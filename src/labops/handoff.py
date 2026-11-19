import json
import sqlite3
from pathlib import Path

from . import gitio
from .canon import canonical_json
from .config import home
from .gate import _task_policy
from .policy import changed_symbols, frozen_hits, out_of_scope
from .tasks import repo_row, round_row, task_row


def _verification(conn: sqlite3.Connection, round_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM evidence WHERE round_id=? AND kind='INDEPENDENTLY_EXECUTED' ORDER BY created_at DESC,evidence_id DESC LIMIT 1",
                       (round_id,)).fetchone()
    if row is None:
        return None
    results = json.loads(row["results_json"] or "[]")
    return {"evidence_id": row["evidence_id"], "kind": row["kind"], "status": row["status"],
            "results": [{"name": item.get("name"), "exit_code": item.get("exit_code"),
                         "duration_ms": item.get("duration_ms"), "timed_out": item.get("timed_out"),
                         "log_sha256": item.get("log_sha256"), "skipped": bool(item.get("skipped", False))}
                        for item in results if isinstance(item, dict) and "name" in item]}


def _changes(repo_path: Path, base: str, head: str) -> dict:
    statuses = gitio.diff_name_status(repo_path, base, head)
    stats = {path: (added, deleted) for added, deleted, path in gitio.diff_numstat(repo_path, base, head)}
    files = []
    all_added = all_deleted = 0
    has_binary = False
    for status, path in sorted(statuses, key=lambda item: item[1]):
        added, deleted = stats.get(path, (0, 0))
        if added is None or deleted is None:
            has_binary = True
        else:
            all_added += added
            all_deleted += deleted
        files.append({"status": status, "path": path, "added": added, "deleted": deleted})
    return {"files": files, "totals": {"files": len(files), "added": None if has_binary else all_added,
                                         "deleted": None if has_binary else all_deleted}}


def _prior_findings(conn: sqlite3.Connection, task_id: str, current_n: int) -> list[dict]:
    prior_round = conn.execute("SELECT round_id FROM rounds WHERE task_id=? AND n<? ORDER BY n DESC LIMIT 1",
                               (task_id, current_n)).fetchone()
    if prior_round is None:
        return []
    result = []
    reviews = conn.execute("SELECT review_id,findings_json FROM reviews WHERE round_id=? ORDER BY created_at,review_id",
                           (prior_round["round_id"],)).fetchall()
    for review in reviews:
        for finding in json.loads(review["findings_json"] or "[]"):
            if finding.get("severity") in {"blocker", "major"}:
                result.append({**finding, "review_id": review["review_id"]})
    return sorted(result, key=lambda item: (item.get("review_id", ""), item.get("id", "")))


def _self_reported(conn: sqlite3.Connection, rnd) -> dict:
    notes = {}
    if rnd["claim_id"]:
        claim = conn.execute("SELECT payload_json FROM claims WHERE claim_id=?", (rnd["claim_id"],)).fetchone()
        if claim:
            notes = json.loads(claim["payload_json"]).get("notes", {}) or {}
    return {"kind": "SELF_REPORTED", "notes": notes, "self_tests": notes.get("self_tests")}


def handoff_data(conn: sqlite3.Connection, round_id: str) -> dict:
    rnd = round_row(conn, round_id)
    task = task_row(conn, rnd["task_id"])
    repo = repo_row(conn, task["repo_id"])
    repo_path = Path(repo["path"])
    profile_row = conn.execute("SELECT content_json FROM acceptance_profiles WHERE profile_hash=?", (task["profile_hash"],)).fetchone()
    if profile_row is None:
        raise ValueError(f"missing profile {task['profile_hash']}")
    profile = json.loads(profile_row[0])
    policy = _task_policy(conn, task, repo)
    submitted_ref = f"refs/labops/{task['task_id']}/r{rnd['n']}/{rnd['actor']}/submitted"
    changes = _changes(repo_path, rnd["base_commit"], rnd["head_commit"]) if rnd["head_commit"] else {"files": [], "totals": {"files": 0, "added": 0, "deleted": 0}}
    return {
        "schema_version": 1,
        "task_id": task["task_id"],
        "goal": task["goal"],
        "round": {"n": rnd["n"], "actor": rnd["actor"], "role": rnd["role"], "state": rnd["state"],
                  "base_commit": rnd["base_commit"], "head_commit": rnd["head_commit"], "head_tree": rnd["head_tree"],
                  "branch": rnd["branch"], "submitted_ref": submitted_ref,
                  "submitted_ref_value": gitio.read_ref(repo_path, submitted_ref)},
        "acceptance": {"profile_hash": task["profile_hash"],
                       "commands": [{"name": command["name"], "argv": command["argv"]} for command in profile.get("commands", [])]},
        "verification": _verification(conn, rnd["round_id"]),
        "changes": changes,
        "symbols": {"changed": changed_symbols(repo_path, rnd["base_commit"], rnd["head_commit"])
                     if rnd["head_commit"] else []},
        "policy": {"frozen_hits": frozen_hits(repo_path, policy, rnd["base_commit"], rnd["head_commit"])
                   if rnd["head_commit"] else [],
                   "out_of_scope": out_of_scope(repo_path, profile.get("allowed_paths", []), rnd["base_commit"], rnd["head_commit"])
                   if rnd["head_commit"] else []},
        "prior_findings": _prior_findings(conn, task["task_id"], rnd["n"]),
        "self_reported": _self_reported(conn, rnd),
        "next": {"suggested_actor": "opus" if rnd["actor"] == "codex" else "user", "suggested_role": "code_review"},
    }


def render_handoff_md(data: dict) -> str:
    target = data["round"]
    lines = [f"# Handoff {data['task_id']} r{target['n']}", "", "## 任务目标", "", data["goal"], "",
             "## 修改内容", "", "| 状态 | 文件 | 新增 | 删除 |", "| --- | --- | ---: | ---: |"]
    for item in data["changes"]["files"]:
        lines.append(f"| {item['status']} | `{item['path']}` | {item['added'] if item['added'] is not None else 'binary'} | {item['deleted'] if item['deleted'] is not None else 'binary'} |")
    totals = data["changes"]["totals"]
    lines.extend(["", f"合计：{totals['files']} 个文件，新增 {totals['added'] if totals['added'] is not None else '含二进制，未汇总'}，删除 {totals['deleted'] if totals['deleted'] is not None else '含二进制，未汇总'}。", "",
                  "## 关键 Diff", ""])
    if data["symbols"]["changed"]:
        lines.extend(f"- `{item['path']}`：`{item['qualname']}`" for item in data["symbols"]["changed"])
    else:
        lines.append("- 无可映射的 Python 符号差异。")
    lines.extend(["", "## 测试证据", ""])
    verification = data["verification"]
    if verification is None:
        lines.append("- 尚无独立执行证据。")
    else:
        lines.append(f"- {verification['kind']}：{verification['status']}（{verification['evidence_id']}）")
        for result in verification["results"]:
            lines.append(f"- `{result['name']}`：exit={result['exit_code']}，timeout={result['timed_out']}，log_sha256={result['log_sha256']}")
    lines.extend(["", "## 已知限制（Agent 自报，未核验）", "", "```json",
                  json.dumps(data["self_reported"], ensure_ascii=False, sort_keys=True, indent=2), "```", "",
                  "## 需重点审查", "", "### 上轮 blocker/major findings", ""])
    if data["prior_findings"]:
        lines.extend(f"- [{item['severity']}] {item['id']}（review `{item['review_id']}`）：{item['text']}" for item in data["prior_findings"])
    else:
        lines.append("- 无。")
    lines.extend(["", "### 冻结区命中", ""])
    lines.extend(f"- `{item['path']}`：{item['reason']}，symbols={item.get('symbols', [])}" for item in data["policy"]["frozen_hits"])
    if not data["policy"]["frozen_hits"]:
        lines.append("- 无。")
    lines.extend(["", "### 允许范围之外", ""])
    lines.extend(f"- `{path}`" for path in data["policy"]["out_of_scope"])
    if not data["policy"]["out_of_scope"]:
        lines.append("- 无。")
    lines.extend(["", "### 验证失败命令", ""])
    failures = [item for item in (verification or {}).get("results", [])
                if not item.get("skipped", False) and (item["exit_code"] is None or item["exit_code"] != 0 or item["timed_out"])]
    for item in failures:
        if item["exit_code"] is None:
            lines.append(f"- `{item['name']}`：blocked（命令未能启动或 cwd 不可用）。")
        elif item["timed_out"]:
            lines.append(f"- `{item['name']}`：timeout，exit={item['exit_code']}。")
        else:
            lines.append(f"- `{item['name']}`：exit={item['exit_code']}。")
    if not failures:
        lines.append("- 无。")
    lines.extend(["", f"建议下一步：actor=`{data['next']['suggested_actor']}`，role=`{data['next']['suggested_role']}`。", ""])
    return "\n".join(lines)


def build_handoff(conn: sqlite3.Connection, round_id: str) -> tuple[Path, Path]:
    data = handoff_data(conn, round_id)
    n = data["round"]["n"]
    task_id = data["task_id"]
    directory = home() / "handoffs" / task_id
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"r{n}.json"
    md_path = directory / f"r{n}.md"
    json_path.write_bytes(canonical_json(data) + b"\n")
    md_path.write_text(render_handoff_md(data), encoding="utf-8", newline="\n")
    return json_path, md_path
