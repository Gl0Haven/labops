"""Notification outbox and sender adapters.

A sender is a callable sender(text) -> (status, error), where status is one of
"sent", "failed", "unknown", or "token_expired". Set markdown_style = "wechat"
to enable WeChat Markdown conversion. Use register_sender(name, factory).
"""

import json
import os
import re
import shutil
import ssl
import sqlite3
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from pathlib import Path

from . import desktop, gitio
from .canon import canonical_json
from .config import now
from .db import tx
from .errors import Blocked, ValidationError
from .gpuq import queue_tick
from .runs import scan_runs

TEST_MESSAGE = "[LabOps] 测试消息：通知通道可用"
_NOTIFY_KIND_RE = re.compile(r"[a-z0-9_:-]{1,80}")
_TASK_SHORT_RE = re.compile(r"^T\d{8}-(.+)$")
_TEST_SUMMARY_RE = re.compile(r"FAILED\s*\(failures=\d+[^)]*\)|Ran\s+\d+\s+tests?", re.IGNORECASE)
_QUEUE_ID_RE = re.compile(r"\bgq_\d{8}T\d{6}_[0-9a-fA-F]{8}\b")
_QUEUE_ENTRYPOINT_RE = re.compile(
    r"(?i)(?:-ScriptPath\s+(?:\"([^\"]+\.ps1)\"|([^\s]+\.ps1))|"
    r"background_start\s+启动\s+(.+?\.ps1)(?=[，,]|$))"
)
_ALERT_MAX_CHARS = 140
DIRECT_KINDS = frozenset({"desktop_reply", "agent_report", "agent_relay"})
RELAY_SUMMARY_MAX_CHARS = 400
REPLY_MAX_CHARS = 1500
NOT_SEEN_REPORT_MINUTES = 15
DISPATCH_TIMEOUT_MINUTES = 360
AGENT_REPORT_MAX_CHARS = 1500
AGENT_REPORT_PER_HOUR = 30


def _task_for_round(conn: sqlite3.Connection, round_id: str):
    return conn.execute("SELECT task_id,n FROM rounds WHERE round_id=?", (round_id,)).fetchone()


def _task_for_operation(conn: sqlite3.Connection, op_id: str):
    operation = conn.execute("SELECT repo_id,on_applied_json,detail_json FROM operations WHERE op_id=?", (op_id,)).fetchone()
    if operation is None:
        return None, None
    for field in ("on_applied_json", "detail_json"):
        try:
            detail = json.loads(operation[field] or "{}")
        except (TypeError, json.JSONDecodeError):
            detail = {}
        if not isinstance(detail, dict):
            continue
        task_id = detail.get("task_id")
        if isinstance(task_id, str):
            return task_id, None
        integration_id = detail.get("integration_id")
        if isinstance(integration_id, str):
            integration = conn.execute("SELECT task_id FROM integrations WHERE integration_id=?", (integration_id,)).fetchone()
            if integration:
                return integration["task_id"], None
    return None, None


def _disabled_kinds(conn: sqlite3.Connection) -> set[str]:
    row = conn.execute("SELECT value FROM meta WHERE key='notify_kinds_disabled'").fetchone()
    return {item.strip() for item in (row[0] if row else "").split(",") if item.strip()}


def _set_kind_disabled(conn: sqlite3.Connection, kind: str, disabled: bool) -> dict:
    if not isinstance(kind, str) or not _NOTIFY_KIND_RE.fullmatch(kind):
        raise ValidationError("invalid_notify_kind", detail={"kind": kind})
    with tx(conn):
        kinds = _disabled_kinds(conn)
        before = kind in kinds
        if disabled:
            kinds.add(kind)
        else:
            kinds.discard(kind)
        encoded = ",".join(sorted(kinds))
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('notify_kinds_disabled',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (encoded,)
        )
    return {"kind": kind, "muted": disabled, "changed": before != disabled,
            "disabled": sorted(kinds)}


def mute_kind(conn: sqlite3.Connection, kind: str) -> dict:
    return _set_kind_disabled(conn, kind, True)


def unmute_kind(conn: sqlite3.Connection, kind: str) -> dict:
    return _set_kind_disabled(conn, kind, False)


def set_alert_detail(conn: sqlite3.Connection, repo_id: str, enabled: bool) -> dict:
    if not isinstance(enabled, bool):
        raise ValidationError("invalid_alert_detail", detail={"enabled": enabled})
    current = conn.execute("SELECT alert_detail FROM repos WHERE repo_id=?", (repo_id,)).fetchone()
    if current is None:
        raise ValidationError("repo_not_found", detail={"repo_id": repo_id})
    target = int(enabled)
    changed = current["alert_detail"] != target
    if changed:
        with tx(conn):
            conn.execute("UPDATE repos SET alert_detail=? WHERE repo_id=?", (target, repo_id))
            conn.execute(
                "INSERT INTO events(ts,entity_type,entity_id,from_state,to_state,actor,detail_json) "
                "VALUES(?,?,?,?,?,?,?)",
                (now(), "repo", repo_id, None, None, "user",
                 canonical_json({"alert_detail": enabled}).decode("utf-8")),
            )
    return {"repo_id": repo_id, "alert_detail": enabled, "changed": changed}


def _single_line(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\r", " ").replace("\n", " ")).strip()


def _truncate(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    if limit == 1:
        return "…"
    return value[:limit - 1] + "…"


def _to_wechat_markdown(text: str) -> str:
    rendered: list[str] = []
    for line in str(text).splitlines():
        if not line.strip():
            if rendered and rendered[-1] != "":
                rendered.append("")
            continue
        if line.startswith("[LabOps"):
            if rendered and rendered[-1] != "":
                rendered.append("")
            rendered.append(line)
        elif line.startswith("- "):
            rendered.append(line)
        else:
            rendered.append(f"- {line}")
    while rendered and rendered[-1] == "":
        rendered.pop()
    return "\n".join(rendered)


def _task_short(task_id: str | None) -> str:
    if not task_id:
        return "旧流程"
    match = _TASK_SHORT_RE.fullmatch(task_id)
    slug = match.group(1) if match else task_id
    return slug.removeprefix("labops-")


def _format_alert(repo_id: str, task_id: str | None, number, role_event: str,
                  detail: str | None, next_step: str | None, *, alert_detail: bool,
                  full_task_id: bool = False) -> str:
    label = task_id if full_task_id and task_id else _task_short(task_id)
    round_part = f" r{number}" if number is not None else ""
    first = f"[LabOps · {repo_id}] {label}{round_part} · {role_event}"
    if not alert_detail:
        return first

    detail_line = _single_line(detail)
    next_line = f"下一步：{_single_line(next_step)}" if next_step else ""
    if not detail_line and not next_line:
        return first
    if not detail_line:
        return "\n".join((first, _truncate(next_line, _ALERT_MAX_CHARS - len(first) - 1)))
    if not next_line:
        return "\n".join((first, _truncate(detail_line, _ALERT_MAX_CHARS - len(first) - 1)))

    detail_budget = _ALERT_MAX_CHARS - len(first) - len(next_line) - 2
    if detail_budget > 0:
        return "\n".join((first, _truncate(detail_line, detail_budget), next_line))
    next_budget = _ALERT_MAX_CHARS - len(first) - 1
    return "\n".join((first, _truncate(next_line, next_budget)))


def _task_alert_context(conn: sqlite3.Connection, task_id: str):
    return conn.execute(
        "SELECT t.task_id,t.repo_id,t.goal,p.alert_detail,p.confidential,p.path AS repo_path "
        "FROM tasks t JOIN repos p ON p.repo_id=t.repo_id WHERE t.task_id=?", (task_id,),
    ).fetchone()


def _round_alert_context(conn: sqlite3.Connection, round_id: str):
    return conn.execute(
        "SELECT r.task_id,r.n,r.actor,r.base_commit,r.head_commit,t.repo_id,p.alert_detail,p.path AS repo_path "
        "FROM rounds r JOIN tasks t ON t.task_id=r.task_id JOIN repos p ON p.repo_id=t.repo_id "
        "WHERE r.round_id=?", (round_id,),
    ).fetchone()


def _latest_evidence(conn: sqlite3.Connection, round_id: str):
    row = conn.execute(
        "SELECT status,results_json FROM evidence WHERE round_id=? "
        "ORDER BY created_at DESC,evidence_id DESC LIMIT 1", (round_id,),
    ).fetchone()
    if row is None:
        return None, []
    try:
        results = json.loads(row["results_json"] or "[]")
    except (TypeError, json.JSONDecodeError):
        results = []
    return row["status"], results if isinstance(results, list) else []


def _verification_log_summary(result: dict) -> str | None:
    log_path = result.get("log_path")
    if not isinstance(log_path, str) or not log_path:
        return None
    try:
        lines = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()[-30:]
    except OSError:
        return None
    for line in reversed(lines):
        match = _TEST_SUMMARY_RE.search(line)
        if match:
            return _single_line(match.group(0))
    return None


def _failed_command_summary(results) -> str:
    candidates = [item for item in results if isinstance(item, dict)]
    failed = next((item for item in candidates if item.get("timed_out") or
                   (item.get("exit_code") is not None and item.get("exit_code") != 0)), None)
    if failed is None:
        failed = next((item for item in candidates if not item.get("skipped")), {})
    name = _single_line(failed.get("name") or "验收命令")
    code = failed.get("exit_code")
    summary = f"{name} exit {code if code is not None else '?'}"
    if failed.get("timed_out"):
        summary += "，超时"
    log_summary = _verification_log_summary(failed)
    if log_summary:
        summary += f"；{log_summary}"
    return summary


def _event_notification(conn: sqlite3.Connection, event: sqlite3.Row):
    entity_type, entity_id, state = event["entity_type"], event["entity_id"], event["to_state"]
    detail = {}
    try:
        detail = json.loads(event["detail_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        pass
    if not isinstance(detail, dict):
        detail = {}

    def emit(task_info, kind, role_event, detail_text=None, next_step=None, *, number=None, full_task_id=False):
        if task_info is None:
            return None
        task_id = task_info["task_id"]
        text = _format_alert(task_info["repo_id"], task_id, number, role_event, detail_text, next_step,
                             alert_detail=bool(task_info["alert_detail"]), full_task_id=full_task_id)
        if (task_info["alert_detail"] and next_step
                and ("转给 Codex" in next_step or "转给 Opus" in next_step)):
            goal = re.sub(r"\s*\(see\b[^)]*\)\s*$", "", _single_line(task_info["goal"]), flags=re.IGNORECASE).strip()
            text += f"\n任务：{goal[:40]}"
        return {"task_id": task_id, "kind": kind, "text": text}

    def actor_name(actor: str) -> str:
        return {"codex": "Codex", "opus": "Opus", "gemini": "Gemini", "user": "用户", "labops": "LabOps"}.get(
            actor, actor[:1].upper() + actor[1:] if actor else "LabOps")

    if entity_type == "bg_run" and state in {
        "run_started", "run_completed", "run_failed", "run_stopped", "run_stalled", "run_orphaned",
        "run_exited",
    }:
        run = conn.execute(
            "SELECT b.repo_id,b.run_dir,b.progress_hint,b.exit_code,b.source,b.title,p.alert_detail "
            "FROM bg_runs b JOIN repos p ON p.repo_id=b.repo_id WHERE b.run_key=?", (entity_id,),
        ).fetchone()
        if run is None:
            return None
        status_label = {
            "run_started": "已开始", "run_completed": "已完成", "run_failed": "失败",
            "run_stopped": "已停止", "run_stalled": "疑似卡住", "run_orphaned": "进程已不在",
            "run_exited": "已结束（退出码未知）",
        }[state]
        run_name = (run["title"] if run["source"] == "adopted" and run["title"] else
                    Path(str(run["run_dir"]).replace("\\", "/")).name or entity_id[:12])
        first = f"[LabOps · {run['repo_id']}] 训练 {run_name} · {status_label}"
        if not run["alert_detail"]:
            return {"task_id": None, "kind": state, "text": first}
        if state == "run_exited":
            last_progress = _single_line(detail.get("progress_hint") or run["progress_hint"]) or "暂无训练进度行"
            return {"task_id": None, "kind": state,
                    "text": f"{first}\n最后进度：{last_progress}"}
        if state == "run_orphaned":
            second = "进程已不在，但状态仍是 running"
        elif state == "run_failed":
            error_line = _single_line(detail.get("error_line"))
            exit_code = detail.get("exit_code", run["exit_code"])
            second = f"exit code {exit_code if exit_code is not None else '?'}"
            if error_line:
                second += f" · {_truncate(error_line, 100)}"
        else:
            second = _single_line(detail.get("progress_hint") or run["progress_hint"]) or "暂无训练进度行"
        return {"task_id": None, "kind": state,
                "text": f"{first}\n{second}\n下一步：让 Codex 用 background_get 查看"}

    if entity_type == "gpu_queue" and isinstance(detail.get("notification_kind"), str):
        kind = detail["notification_kind"]
        if kind not in {"queue_ask_busy", "queue_ask_idle", "queue_idle_manual",
                        "queue_launched", "queue_launch_failed"}:
            return None
        queue = conn.execute(
            "SELECT q.queue_id,q.repo_id,q.title,q.entrypoint,q.working_dir,q.run_prefix,p.alert_detail "
            "FROM gpu_queue q JOIN repos p ON p.repo_id=q.repo_id WHERE q.queue_id=?", (entity_id,),
        ).fetchone()
        if queue is None:
            return None
        repo_id, title, queue_id = queue["repo_id"], queue["title"], queue["queue_id"]
        if kind == "queue_ask_busy":
            first = f"[LabOps · {repo_id}] {title} 排队中"
            active_runs = detail.get("gpu_runs") or []
            if active_runs:
                run = active_runs[0]
                age = run.get("age_minutes")
                age_text = f"{age} 分钟前更新" if isinstance(age, int) else "更新时刻未知"
                run_text = (f"当前在跑：{run.get('run_dir_name') or '未知 run'} · "
                            f"{run.get('progress_hint') or '暂无进度'} · {age_text}")
            else:
                run_text = "当前 GPU 占用状态待复核"
            lines = [first, run_text,
                     f"回复秘书：A 等跑完我来调度 / B 跑完自动开跑（{queue_id}）"]
        elif kind == "queue_ask_idle":
            lines = [f"[LabOps · {repo_id}] GPU 空闲，现在跑 {title} 吗？",
                     f"回复秘书：现在跑 / 先不跑（{queue_id}）"]
        elif kind == "queue_idle_manual":
            if detail.get("handoff"):
                lines = [
                    f"[LabOps · {repo_id}] {title}（{queue_id}）",
                    "GPU 已空闲 / 按你的选择应启动",
                    f"转给 Codex：用 background_start 启动 {queue['entrypoint']}，run 目录前缀 {queue['run_prefix']}",
                    f"任务：{title}",
                ]
            else:
                previous = detail.get("previous_run") if isinstance(detail.get("previous_run"), dict) else {}
                headline = f"GPU 空了：{previous.get('run') or '前置任务'} {previous.get('result') or ''}".strip()
                lines = [f"[LabOps · {repo_id}] {headline}", f"可以跑：{title}",
                         f"队列：{queue_id}", f"手动命令：{detail.get('manual_command') or '命令不可用'}"]
        elif kind == "queue_launched":
            lines = [f"[LabOps · {repo_id}] 已启动：{title} → {detail.get('run_dir') or 'run 目录待识别'}"]
        else:
            lines = [f"[LabOps · {repo_id}] 启动失败：{detail.get('reason') or '原因未记录'}",
                     f"队列：{queue_id}",
                     f"手动命令：{detail.get('manual_command') or '命令不可用'}"]
        fingerprint_summary = detail.get("fingerprint_summary")
        if isinstance(fingerprint_summary, str) and fingerprint_summary:
            lines.append(fingerprint_summary)
        if not queue["alert_detail"]:
            return {"task_id": None, "kind": kind, "text": lines[0]}
        return {"task_id": None, "kind": kind, "text": "\n".join(lines)}

    if entity_type == "round" and state in {"CLAIMED", "VERIFIED", "REJECTED", "BLOCKED",
                                             "integrity_conflict", "unattributed_commit"}:
        rnd = _round_alert_context(conn, entity_id)
        if rnd is None:
            return None
        task_id, number, actor = rnd["task_id"], rnd["n"], rnd["actor"]
        task_info = _task_alert_context(conn, task_id)
        if state == "CLAIMED":
            try:
                if not isinstance(rnd["base_commit"], str) or not isinstance(rnd["head_commit"], str):
                    raise ValidationError("snapshot_refs_missing")
                files = len(gitio.diff_numstat(Path(rnd["repo_path"]), rnd["base_commit"], rnd["head_commit"]))
                diff_summary = f"改动 {files} 个文件"
            except (OSError, TypeError, ValidationError):
                diff_summary = "改动统计不可用"
            detail_text = f"commit {(rnd['head_commit'] or '')[:8] or '未知'}，{diff_summary}"
            return emit(task_info, "submitted", f"{actor_name(actor)} 已提交", detail_text,
                        f"LabOps 验证（labops verify --round {entity_id}）", number=number)

        if state == "VERIFIED":
            _status, results = _latest_evidence(conn, entity_id)
            executed = [item for item in results if isinstance(item, dict) and not item.get("skipped")]
            command_names = [_single_line(item.get("name")) for item in executed if item.get("name")]
            duration_ms = sum(item.get("duration_ms", 0) for item in executed
                              if isinstance(item.get("duration_ms", 0), (int, float)))
            names = ", ".join(command_names) or "验收命令"
            detail_text = f"{names} 全部 exit 0，用时 {duration_ms / 1000:.1f}s"
            next_step = "转给 Opus 审查" if actor == "codex" else "转给 Codex 或你审查"
            return emit(task_info, "verified", "LabOps 验证通过", detail_text, next_step, number=number)

        if state == "REJECTED":
            _status, results = _latest_evidence(conn, entity_id)
            return emit(task_info, "verify_failed", "LabOps 验证未通过", _failed_command_summary(results),
                        f"转给 {actor_name(actor)}：调用 next_task 返工", number=number)

        if state == "BLOCKED":
            reason = _single_line(detail.get("reason")) or "原因未记录"
            return emit(task_info, "verify_blocked", "LabOps 验证受阻", f"原因 {reason}",
                        "看原因后决定（常见：repo 未信任 / 需 GPU）", number=number)

        detail_kind = "integrity_conflict" if state == "integrity_conflict" else "unattributed_commit"
        if detail.get("ref"):
            summary = _single_line(detail["ref"])
        elif detail.get("expected") or detail.get("actual") or detail.get("tip"):
            expected = str(detail.get("expected") or "")[:8] or "?"
            actual = str(detail.get("actual") or detail.get("tip") or "")[:8] or "?"
            summary = f"期望 {expected} / 实际 {actual}"
        else:
            summary = _single_line(detail.get("reason") or "引用信息不可用")
        return emit(task_info, "integrity", "LabOps 对账异常", f"{detail_kind}：{summary}",
                    "找 Opus 看", number=number)

    if entity_type == "review" and state in {"REWORK_REQUIRED", "BLOCKED"}:
        review = conn.execute(
            "SELECT v.task_id,v.reviewer,v.verdict,v.findings_json,r.n,r.actor "
            "FROM reviews v JOIN rounds r ON r.round_id=v.round_id WHERE v.review_id=?", (entity_id,),
        ).fetchone()
        if review is None:
            return None
        task_info = _task_alert_context(conn, review["task_id"])
        try:
            findings = json.loads(review["findings_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            findings = []
        significant = [item for item in findings if isinstance(item, dict)
                       and item.get("severity") in {"blocker", "major"}] if isinstance(findings, list) else []
        detail_text = "无 blocker/major finding"
        if significant:
            finding = significant[0]
            finding_id = _single_line(finding.get("id") or "finding")
            finding_text = _truncate(_single_line(finding.get("text")), 50)
            detail_text = f"{finding_id}：{finding_text}"
            if len(significant) > 1:
                detail_text += f"；等 {len(significant)} 条"
        reviewer = actor_name(review["reviewer"])
        verdict = review["verdict"] or state
        return emit(task_info, "review_blocking", f"{reviewer} 审查：{verdict}", detail_text,
                    f"转给 {actor_name(review['actor'])}：调用 next_task", number=review["n"])

    if entity_type == "review" and state in {"PASS", "PASS_WITH_NOTES"}:
        review = conn.execute(
            "SELECT v.task_id,v.reviewer,v.verdict,v.independence,v.findings_json,r.n "
            "FROM reviews v JOIN rounds r ON r.round_id=v.round_id WHERE v.review_id=?", (entity_id,),
        ).fetchone()
        if review is None:
            return None
        task_info = _task_alert_context(conn, review["task_id"])
        try:
            findings = json.loads(review["findings_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            findings = []
        note_count = len(findings) if isinstance(findings, list) else 0
        detail_text = f"{note_count} 条 notes" if note_count else None
        self_review = review["independence"] == "SELF_REVIEWED_FIX"
        role_event = "自审通过" if self_review else f"{actor_name(review['reviewer'])} 审查通过"
        next_step = (f"需你接受自审：labops approve self-review --task-id {review['task_id']}"
                     if self_review else "等你批准合并")
        return emit(task_info, "review_passed", role_event, detail_text, next_step, number=review["n"])

    if entity_type == "integration" and state in {"AWAITING_HUMAN_APPROVAL", "MERGED"}:
        integration = conn.execute(
            "SELECT task_id,candidate_commit,candidate_tree,main_before FROM integrations WHERE integration_id=?",
            (entity_id,),
        ).fetchone()
        if integration is None:
            return None
        task_info = _task_alert_context(conn, integration["task_id"])
        latest = conn.execute("SELECT MAX(n) FROM rounds WHERE task_id=?", (integration["task_id"],)).fetchone()[0]
        if state == "AWAITING_HUMAN_APPROVAL":
            return emit(task_info, "awaiting_approval", "LabOps 等你批准合并",
                        f"tree {(integration['candidate_tree'] or '')[:12] or '未知'}",
                        f"labops integrate approve --integration {entity_id}", number=latest,
                        full_task_id=True)
        detail_text = f"{(integration['candidate_commit'] or '')[:8] or '未知'}"
        restart_required = False
        if task_info is not None and integration["main_before"] and integration["candidate_commit"]:
            try:
                from .gate import client_restart_requirement
                restart_required = client_restart_requirement(
                    Path(task_info["repo_path"]), integration["main_before"], integration["candidate_commit"],
                )["client_restart_required"]
            except (OSError, ValidationError, KeyError):
                restart_required = False
        return emit(task_info, "merged", "LabOps 已合入 main", detail_text,
                    "需重启 Codex/Claude" if restart_required else None,
                    number=latest, full_task_id=True)

    if entity_type == "repo" and state == "legacy_drift":
        repo = conn.execute("SELECT repo_id,alert_detail FROM repos WHERE repo_id=?", (entity_id,)).fetchone()
        if repo is None:
            return None
        kind = detail.get("kind")
        if not isinstance(kind, str) or not re.fullmatch(r"[a-z0-9_:-]{1,80}", kind):
            kind = "status_drift"
        baseline = str(detail.get("baseline") or "")[:8]
        head = str(detail.get("head") or "")[:8]
        detail_text = f"{kind}：baseline {baseline or '?'} / HEAD {head or '?'}"
        text = _format_alert(repo["repo_id"], None, None, "旧流程状态漂移", detail_text,
                             "可忽略或更新 agent-status", alert_detail=bool(repo["alert_detail"]))
        return {"task_id": None, "kind": "legacy_drift", "text": text}

    if entity_type == "operation" and state == "CONFLICT":
        task_id, _ = _task_for_operation(conn, entity_id)
        if task_id is None:
            return None
        operation = conn.execute("SELECT kind,ref,detail_json FROM operations WHERE op_id=?", (entity_id,)).fetchone()
        task_info = _task_alert_context(conn, task_id)
        latest = conn.execute("SELECT MAX(n) FROM rounds WHERE task_id=?", (task_id,)).fetchone()[0]
        operation_detail = {}
        try:
            operation_detail = json.loads(operation["detail_json"] or "{}") if operation else {}
        except (TypeError, json.JSONDecodeError):
            operation_detail = {}
        summary = _single_line(operation_detail.get("reason") or (operation["kind"] if operation else "operation_conflict"))
        reference = _single_line(operation["ref"] if operation else None)
        if reference:
            summary += f"：{reference}"
        return emit(task_info, "integrity", "LabOps 对账异常", summary, "找 Opus 看", number=latest)
    return None


def scan(conn: sqlite3.Connection) -> int:
    inserted = 0
    with tx(conn):
        row = conn.execute("SELECT value FROM meta WHERE key='notify_cursor'").fetchone()
        cursor = int(row[0]) if row else 0
        events = conn.execute("SELECT * FROM events WHERE seq>? ORDER BY seq", (cursor,)).fetchall()
        max_seq = cursor
        for event in events:
            max_seq = max(max_seq, event["seq"])
            notification = _event_notification(conn, event)
            if notification is None or notification["kind"] in _disabled_kinds(conn):
                continue
            result = conn.execute(
                "INSERT OR IGNORE INTO notifications(event_key,task_id,kind,text,status,created_at) VALUES(?,?,?,?,'PENDING',?)",
                (f"ev:{event['seq']}", notification["task_id"], notification["kind"], notification["text"], event["ts"]),
            )
            inserted += max(result.rowcount, 0)
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('notify_cursor',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(max_seq),),
        )
    return inserted


DEFAULT_URGENT_KINDS = frozenset({"awaiting_approval", "review_blocking", "verify_failed", "verify_blocked",
                                   "integrity", "run_failed", "run_stalled", "queue_ask_busy",
                                   "queue_ask_idle", "queue_idle_manual", "queue_launch_failed"})
BATCH_KINDS = frozenset({"batch_urgent", "batch_digest"})
DEFAULT_DIGEST_MINUTES = 180
_BATCH_MAX_CHARS = 600


def _urgent_kinds(conn: sqlite3.Connection) -> set[str]:
    row = conn.execute("SELECT value FROM meta WHERE key='notify_urgent_kinds'").fetchone()
    if row is None:
        return set(DEFAULT_URGENT_KINDS)
    return {kind for kind in row[0].split(",") if kind}


def _digest_minutes(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='notify_digest_minutes'").fetchone()
    try:
        value = int(row[0]) if row else DEFAULT_DIGEST_MINUTES
    except (TypeError, ValueError):
        return DEFAULT_DIGEST_MINUTES
    return value if 0 <= value <= 7 * 24 * 60 else DEFAULT_DIGEST_MINUTES


def _llm_rewrite_enabled(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM meta WHERE key='notify_llm_rewrite'").fetchone()
    return bool(row and row[0] == "on")


def notify_config(conn: sqlite3.Connection, *, urgent_kinds: list[str] | None = None,
                  digest_minutes: int | None = None, llm_rewrite: str | None = None,
                  sender: str | None = None) -> dict:
    if sender is not None:
        _sender_factory(sender)
    with tx(conn):
        if sender is not None:
            conn.execute("INSERT INTO meta(key,value) VALUES('notify_sender',?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (sender,))
        if urgent_kinds is not None:
            cleaned = sorted({kind.strip() for kind in urgent_kinds if kind.strip()})
            if any(not _NOTIFY_KIND_RE.fullmatch(kind) or kind in BATCH_KINDS for kind in cleaned):
                raise ValidationError("invalid_notify_kind", detail={"kinds": cleaned})
            conn.execute("INSERT INTO meta(key,value) VALUES('notify_urgent_kinds',?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (",".join(cleaned),))
        if digest_minutes is not None:
            if (isinstance(digest_minutes, bool) or not isinstance(digest_minutes, int)
                    or not 0 <= digest_minutes <= 7 * 24 * 60):
                raise ValidationError("invalid_digest_minutes", detail={"digest_minutes": digest_minutes})
            conn.execute("INSERT INTO meta(key,value) VALUES('notify_digest_minutes',?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(digest_minutes),))
        if llm_rewrite is not None:
            if llm_rewrite not in {"on", "off"}:
                raise ValidationError("invalid_llm_rewrite", detail={"llm_rewrite": llm_rewrite})
            conn.execute("INSERT INTO meta(key,value) VALUES('notify_llm_rewrite',?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (llm_rewrite,))
    return {"urgent_kinds": sorted(_urgent_kinds(conn)), "digest_minutes": _digest_minutes(conn),
            "llm_rewrite": "on" if _llm_rewrite_enabled(conn) else "off"}


def _parse_ts(timestamp: str):
    from datetime import datetime
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def _local_hhmm(timestamp: str) -> str:
    try:
        return _parse_ts(timestamp).astimezone().strftime("%m-%d %H:%M")
    except (AttributeError, ValueError):
        return "?"


def _progress_snapshot(conn: sqlite3.Connection, now_ts: str) -> tuple[list[str], str]:
    import hashlib
    from datetime import datetime

    rows = conn.execute(
        "SELECT run_key,run_dir,progress_hint,progress_at,log_mtime FROM bg_runs "
        "WHERE status='running' ORDER BY run_dir,run_key"
    ).fetchall()
    fingerprint = [{"run_key": row["run_key"], "progress_hint": row["progress_hint"],
                    "log_mtime": row["log_mtime"]} for row in rows]
    progress_hash = hashlib.sha256(
        json.dumps(fingerprint, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    current = datetime.fromisoformat(now_ts.replace("Z", "+00:00"))
    lines = []
    for row in rows:
        base = row["progress_at"] or row["log_mtime"]
        try:
            observed = datetime.fromisoformat(base.replace("Z", "+00:00")) if base else None
            age = max(0, int((current - observed).total_seconds() // 60)) if observed else None
        except (AttributeError, TypeError, ValueError):
            age = None
        run_name = Path(str(row["run_dir"]).replace("\\", "/")).name or row["run_key"][:12]
        hint = _single_line(row["progress_hint"]) or "暂无进度行"
        lines.append(f"{run_name} · {hint} · {age if age is not None else '?'} 分钟前")
    return lines, progress_hash


def _compose_batch(label: str, rows: list, *, progress_lines: list[str] | None = None) -> str:
    header = f"[LabOps · {label} {len(rows)} 条 · 自 {_local_hhmm(rows[0]['created_at'])}]"
    # Urgent batches keep every line of each alert (its next step matters); digests keep first lines.
    blocks = [row["text"] if label == "紧急" else row["text"].splitlines()[0] for row in rows]
    lines = [header]
    used = len(header)
    shown = 0
    for block in blocks:
        if shown and used + len(block) + 1 > _BATCH_MAX_CHARS:
            break
        lines.append(block)
        used += len(block) + 1
        shown += 1
    if shown < len(blocks):
        lines.append(f"…另 {len(blocks) - shown} 条，问秘书看详情")
    if label == "汇总" and progress_lines:
        lines.extend(["", "训练进度", *progress_lines])
    return "\n".join(lines)


def merge_pending(conn: sqlite3.Connection, *, now_ts: str | None = None) -> dict:
    """Fold PENDING alerts so each run sends at most one urgent and one digest message.

    WeChat bot replies are scarce, so non-urgent kinds wait for the digest window and several
    urgent alerts in one run go out as a single message. Sources become MERGED and point at
    the batch row through last_error; the batch row reuses the normal send/pause/ntfy path.
    """
    from datetime import timedelta
    timestamp = now_ts or now()
    urgent = _urgent_kinds(conn)
    result = {"merged_urgent": 0, "merged_digest": 0}
    with tx(conn):
        disabled = _disabled_kinds(conn)
        rows = [row for row in conn.execute(
            "SELECT event_key,task_id,kind,text,created_at FROM notifications WHERE status='PENDING' "
            "ORDER BY created_at,event_key").fetchall()
            if row["kind"] not in (BATCH_KINDS | DIRECT_KINDS) and row["kind"] not in disabled]
        urgent_rows = [row for row in rows if row["kind"] in urgent]
        digest_rows = [row for row in rows if row["kind"] not in urgent]
        progress_lines, progress_hash = _progress_snapshot(conn, timestamp)
        batches = []
        digest_ready = False
        if len(urgent_rows) > 1:
            batches.append(("batch_urgent", "紧急", urgent_rows))
        if digest_rows and _parse_ts(timestamp) - _parse_ts(digest_rows[0]["created_at"]) >= timedelta(
                minutes=_digest_minutes(conn)):
            batches.append(("batch_digest", "汇总", digest_rows))
            digest_ready = True
        for kind, label, members in batches:
            batch_key = f"batch:{kind}:{members[-1]['event_key']}:{len(members)}"
            batch_text = _compose_batch(label, members,
                                        progress_lines=progress_lines if kind == "batch_digest" else None)
            conn.execute("INSERT OR IGNORE INTO notifications(event_key,task_id,kind,text,status,created_at) "
                         "VALUES(?,?,?,?,'PENDING',?)",
                         (batch_key, None, kind, batch_text, timestamp))
            conn.executemany("UPDATE notifications SET status='MERGED',last_error=? WHERE event_key=? AND status='PENDING'",
                             [(batch_key, row["event_key"]) for row in members])
            result["merged_urgent" if kind == "batch_urgent" else "merged_digest"] += len(members)
        if digest_ready:
            conn.execute("INSERT INTO meta(key,value) VALUES('runs_progress_digest_hash',?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (progress_hash,))
            conn.execute("INSERT INTO meta(key,value) VALUES('runs_progress_digest_at',?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (timestamp,))
        elif progress_lines and not digest_rows:
            previous_hash = conn.execute(
                "SELECT value FROM meta WHERE key='runs_progress_digest_hash'"
            ).fetchone()
            previous_at = conn.execute(
                "SELECT value FROM meta WHERE key='runs_progress_digest_at'"
            ).fetchone()
            ready_by_window = (previous_at is None or
                               _parse_ts(timestamp) - _parse_ts(previous_at[0]) >= timedelta(
                                   minutes=_digest_minutes(conn)))
            pending_snapshot = conn.execute(
                "SELECT 1 FROM notifications WHERE kind='batch_digest' AND status='PENDING' LIMIT 1"
            ).fetchone() is not None
            if (ready_by_window and not pending_snapshot
                    and (previous_hash is None or previous_hash[0] != progress_hash)):
                batch_key = f"batch:progress:{progress_hash[:16]}:{timestamp}"
                batch_text = "\n".join(["[LabOps · 进度快照]", "训练进度", *progress_lines])
                conn.execute("INSERT OR IGNORE INTO notifications(event_key,task_id,kind,text,status,created_at) "
                             "VALUES(?,NULL,'batch_digest',?,'PENDING',?)",
                             (batch_key, batch_text, timestamp))
                conn.execute("INSERT INTO meta(key,value) VALUES('runs_progress_digest_hash',?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (progress_hash,))
                conn.execute("INSERT INTO meta(key,value) VALUES('runs_progress_digest_at',?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (timestamp,))
    return result


def _pending_rows(conn: sqlite3.Connection, limit: int, disabled: list[str]) -> list[sqlite3.Row]:
    # Direct reports, urgent alerts and batches bypass the digest wait.
    sendable = sorted((_urgent_kinds(conn) | BATCH_KINDS | DIRECT_KINDS) - set(disabled))
    if not sendable:
        return []
    placeholders = ",".join("?" for _ in sendable)
    filter_sql = f" AND kind IN ({placeholders})"
    parameters = [*sendable, limit]
    return conn.execute(
        "SELECT event_key,text FROM notifications WHERE status='PENDING'" + filter_sql +
        " ORDER BY created_at,event_key LIMIT ?", parameters,
    ).fetchall()


def _record_wechat_pause(conn: sqlite3.Connection, *, paused_at: str | None = None) -> None:
    timestamp = now()
    with tx(conn):
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('wechat_paused_at',?) "
            "ON CONFLICT(key) DO UPDATE SET value=CASE WHEN value='' THEN excluded.value ELSE value END",
            (paused_at or timestamp,),
        )
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('wechat_last_probe_at',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (timestamp,),
        )


def _record_probe(conn: sqlite3.Connection) -> None:
    with tx(conn):
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('wechat_last_probe_at',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now(),),
        )


def _clear_wechat_pause(conn: sqlite3.Connection) -> None:
    with tx(conn):
        conn.execute("DELETE FROM meta WHERE key='wechat_paused_at'")


def _send_ntfy_once(conn: sqlite3.Connection, *, event_key: str, text: str, sender) -> str | None:
    with tx(conn):
        claimed = conn.execute(
            "UPDATE notifications SET ntfy_status='ntfy_failed' WHERE event_key=? AND ntfy_status IS NULL",
            (event_key,),
        )
        if claimed.rowcount != 1:
            return None
    try:
        template_first_line = str(text).splitlines()[0] if str(text).splitlines() else ""
        outcome = sender(template_first_line)
        status = outcome[0] if isinstance(outcome, tuple) and outcome else outcome
    except Exception:
        status = "failed"
    ntfy_status = "ntfy_sent" if status == "sent" else "ntfy_failed"
    if ntfy_status == "ntfy_sent":
        with tx(conn):
            conn.execute("UPDATE notifications SET ntfy_status='ntfy_sent' WHERE event_key=? AND ntfy_status='ntfy_failed'",
                         (event_key,))
    return ntfy_status


def _repo_is_confidential(conn: sqlite3.Connection, repo_id: str | None) -> bool:
    if not repo_id:
        return False
    row = conn.execute("SELECT confidential FROM repos WHERE repo_id=?", (repo_id,)).fetchone()
    return bool(row and row["confidential"])


def _repo_id_from_alert(text: str) -> str | None:
    match = re.match(r"^\[LabOps · ([^\]]+)\]", str(text).splitlines()[0] if str(text).splitlines() else "")
    if not match or match.group(1) in {"汇总", "紧急", "进度快照", "训练进度快照"}:
        return None
    return match.group(1)


def _notification_confidential(conn: sqlite3.Connection, *, event_key: str,
                               task_id: str | None, kind: str, text: str) -> bool:
    if task_id:
        row = conn.execute(
            "SELECT p.confidential FROM tasks t JOIN repos p ON p.repo_id=t.repo_id WHERE t.task_id=?",
            (task_id,),
        ).fetchone()
        if row and row["confidential"]:
            return True
    if kind in BATCH_KINDS:
        sources = conn.execute(
            "SELECT task_id,text FROM notifications WHERE status='MERGED' AND last_error=?",
            (event_key,),
        ).fetchall()
        for source in sources:
            if source["task_id"]:
                row = conn.execute(
                    "SELECT p.confidential FROM tasks t JOIN repos p ON p.repo_id=t.repo_id WHERE t.task_id=?",
                    (source["task_id"],),
                ).fetchone()
                if row and row["confidential"]:
                    return True
            if _repo_is_confidential(conn, _repo_id_from_alert(source["text"])):
                return True
    return _repo_is_confidential(conn, _repo_id_from_alert(text))


def _prepare_send_text(conn: sqlite3.Connection, event_key: str, template: str) -> str:
    row = conn.execute(
        "SELECT kind,task_id,rewritten_text FROM notifications WHERE event_key=?", (event_key,),
    ).fetchone()
    if row is None:
        return template
    if row["kind"] == "agent_relay":
        if row["rewritten_text"] is not None:
            return row["rewritten_text"]
        from .rewrite import summarize
        text, outcome = summarize(template, enabled=_llm_rewrite_enabled(conn), confidential=False,
                                  max_chars=RELAY_SUMMARY_MAX_CHARS)
        with tx(conn):
            conn.execute(
                "UPDATE notifications SET rewritten_text=?,rewrite_outcome=? "
                "WHERE event_key=? AND rewritten_text IS NULL", (text, outcome, event_key),
            )
            stored = conn.execute("SELECT rewritten_text FROM notifications WHERE event_key=?", (event_key,)).fetchone()
        return stored["rewritten_text"] if stored is not None else text
    if row["kind"] in DIRECT_KINDS:
        return template
    kind = row["kind"]
    if not (kind.startswith("batch_") or kind in _urgent_kinds(conn)):
        return template
    if row["rewritten_text"] is not None:
        return row["rewritten_text"]
    confidential = _notification_confidential(
        conn, event_key=event_key, task_id=row["task_id"], kind=kind, text=template,
    )
    from .rewrite import rewrite
    rewritten, outcome = rewrite(template, kind=kind, confidential=confidential,
                                 enabled=_llm_rewrite_enabled(conn))
    if outcome == "llm":
        required = set(_QUEUE_ID_RE.findall(template))
        for match in _QUEUE_ENTRYPOINT_RE.finditer(template):
            required.add(match.group(1) or match.group(2) or match.group(3))
        if any(item not in rewritten for item in required):
            rewritten, outcome = template, "template_check_failed"
    with tx(conn):
        conn.execute(
            "UPDATE notifications SET rewritten_text=?,rewrite_outcome=? "
            "WHERE event_key=? AND rewritten_text IS NULL",
            (rewritten, outcome, event_key),
        )
        stored = conn.execute("SELECT rewritten_text FROM notifications WHERE event_key=?", (event_key,)).fetchone()
    return stored["rewritten_text"] if stored and stored["rewritten_text"] is not None else template


def send_pending(conn: sqlite3.Connection, sender, limit: int = 10, *, ntfy_sender=None,
                 ntfy_topic: str | None = None) -> dict:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValidationError("invalid_notify_limit", detail={"limit": limit})
    topic = ntfy_topic if ntfy_topic is not None else os.environ.get("LABOPS_NTFY_TOPIC")
    if isinstance(topic, str):
        topic = topic.strip() or None
    selected_ntfy = ntfy_sender if ntfy_sender is not None else NtfySender(topic) if topic else None
    with tx(conn):
        conn.execute("UPDATE notifications SET status='PENDING',last_error=NULL WHERE status='FAILED' AND attempts<3")
        disabled = sorted(_disabled_kinds(conn))
        paused = conn.execute("SELECT value FROM meta WHERE key='wechat_paused_at'").fetchone() is not None
        rows = _pending_rows(conn, 1 if paused else limit, disabled)

    counts = {"attempted": 0, "accepted": 0, "failed": 0, "unknown": 0,
              "token_expired": 0, "ntfy_sent": 0, "ntfy_failed": 0}
    row_index = 0
    while row_index < len(rows):
        row = rows[row_index]
        row_index += 1
        with tx(conn):
            current = conn.execute("SELECT status,attempts FROM notifications WHERE event_key=?", (row["event_key"],)).fetchone()
            if current is None or current["status"] != "PENDING":
                continue
            previous_attempts = current["attempts"]
            attempts = current["attempts"] + 1
            conn.execute("UPDATE notifications SET attempts=?,status='UNKNOWN',last_error='send_in_progress' WHERE event_key=?",
                         (attempts, row["event_key"]))
        counts["attempted"] += 1
        send_text = _prepare_send_text(conn, row["event_key"], row["text"])
        rewrite_state = conn.execute(
            "SELECT rewrite_outcome FROM notifications WHERE event_key=?", (row["event_key"],)
        ).fetchone()
        if getattr(sender, "markdown_style", None) == "wechat" and not (rewrite_state and rewrite_state["rewrite_outcome"] == "llm"):
            send_text = _to_wechat_markdown(send_text)
        try:
            outcome = sender(send_text)
            if isinstance(outcome, tuple) and len(outcome) == 2:
                status, error = outcome
            else:
                status, error = outcome, None
        except (TimeoutError, subprocess.TimeoutExpired):
            status, error = "unknown", "timeout"
        except Exception as exc:
            status, error = "unknown", type(exc).__name__
        if status not in {"sent", "failed", "unknown", "token_expired"}:
            status, error = "unknown", "unparseable_sender_result"
        elif status == "failed" and not error:
            error = "sender_failed"

        if status == "token_expired":
            counts["token_expired"] += 1
            _record_wechat_pause(conn)
            with tx(conn):
                conn.execute("UPDATE notifications SET status='PENDING',attempts=?,last_error=?,sent_at=NULL WHERE event_key=?",
                             (previous_attempts, error or "token_expired", row["event_key"]))
            ntfy_status = _send_ntfy_once(conn, event_key=row["event_key"], text=row["text"], sender=selected_ntfy) if selected_ntfy else None
            if ntfy_status is not None:
                counts[ntfy_status] += 1
            paused = True
            break

        if paused and status != "sent":
            if status == "failed":
                counts["failed"] += 1
            else:
                counts["unknown"] += 1
            _record_probe(conn)
            with tx(conn):
                conn.execute("UPDATE notifications SET status='PENDING',attempts=?,last_error=?,sent_at=NULL WHERE event_key=?",
                             (previous_attempts, error or status, row["event_key"]))
            ntfy_status = _send_ntfy_once(conn, event_key=row["event_key"], text=row["text"], sender=selected_ntfy) if selected_ntfy else None
            if ntfy_status is not None:
                counts[ntfy_status] += 1
            break

        with tx(conn):
            sent_at = now() if status == "sent" else None
            if status == "sent":
                conn.execute("UPDATE notifications SET status='ACCEPTED',last_error=NULL,sent_at=? WHERE event_key=?",
                             (sent_at, row["event_key"]))
            else:
                conn.execute("UPDATE notifications SET status=?,last_error=?,sent_at=NULL WHERE event_key=?",
                             (status.upper(), error, row["event_key"]))
        if status == "sent":
            counts["accepted"] += 1
            if paused:
                _record_probe(conn)
                _clear_wechat_pause(conn)
                paused = False
                remaining = max(0, limit - counts["attempted"])
                if remaining:
                    rows.extend(_pending_rows(conn, remaining, disabled))
        else:
            counts[status] += 1
            ntfy_status = _send_ntfy_once(conn, event_key=row["event_key"], text=row["text"], sender=selected_ntfy) if status == "failed" and selected_ntfy else None
            if ntfy_status is not None:
                counts[ntfy_status] += 1
    return counts


def _openclaw_command() -> list[str] | None:
    """Return argv that starts openclaw without going through cmd.exe.

    On Windows npm installs openclaw as a .CMD shim; cmd.exe cuts the command line at the first
    newline inside an argument, so multi-line alerts lost every line after the first (and the
    trailing --json flag). When the shim's package entry point exists, run it with node directly.
    """
    executable = shutil.which("openclaw")
    if not executable:
        return None
    shim = Path(executable)
    if shim.suffix.lower() in {".cmd", ".bat"}:
        entry = shim.parent / "node_modules" / "openclaw" / "openclaw.mjs"
        local_node = shim.parent / "node.exe"
        node = str(local_node) if local_node.is_file() else shutil.which("node")
        if entry.is_file() and node:
            return [node, str(entry)]
    return [executable]


def _notify_timeout() -> float:
    # A real openclaw send takes ~55 s (slow CLI start), so the old 60 s cap turned most sends
    # into unknown/timeout. LABOPS_NOTIFY_TIMEOUT overrides the 180 s default.
    raw = os.environ.get("LABOPS_NOTIFY_TIMEOUT", "").strip()
    try:
        value = float(raw) if raw else 180.0
    except ValueError:
        return 180.0
    return value if 0 < value <= 3600 else 180.0


def _parse_openclaw_json(stdout):
    # After a gateway restart openclaw prints ANSI log lines on stdout before the JSON body, so a
    # whole-buffer parse fails and every send was recorded as unknown.
    if not isinstance(stdout, str):
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    offset = 0
    for line in stdout.splitlines(keepends=True):
        if line.lstrip().startswith("{"):
            start = offset + len(line) - len(line.lstrip())
            try:
                value, _end = decoder.raw_decode(stdout, start)
            except json.JSONDecodeError:
                offset += len(line)
                continue
            if isinstance(value, dict):
                return value
        offset += len(line)
    return None


class OpenClawSender:
    def __init__(self, channel: str | None = None):
        self.channel = channel or os.environ.get("LABOPS_NOTIFY_CHANNEL") or "openclaw-weixin"
        self.markdown_style = "wechat" if self.channel == "openclaw-weixin" else None

    def __call__(self, text: str):
        account = os.environ.get("LABOPS_NOTIFY_ACCOUNT")
        target = os.environ.get("LABOPS_NOTIFY_TARGET")
        if not account or not target:
            return "failed", "missing_notify_configuration"
        command = _openclaw_command()
        if command is None:
            return "failed", "openclaw_not_found"
        argv = [*command, "message", "send", "--json", "--channel", self.channel, "--account", account,
                "--target", target, "-m", text]
        try:
            result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=_notify_timeout(), check=False)
        except FileNotFoundError:
            return "failed", "openclaw_not_found"
        output = f"{result.stdout}\n{result.stderr}"
        lowered = output.lower()
        if "ret=-2" in lowered or "prepare failed" in lowered:
            return "token_expired", "openclaw_token_expired"
        payload = _parse_openclaw_json(result.stdout)
        # SEC-3 (Opus fix): openclaw 2026.10.5 reports deliveryStatus under "payload", not at top level.
        delivery = None
        if isinstance(payload, dict):
            delivery = payload.get("deliveryStatus")
            inner = payload.get("payload")
            if delivery is None and isinstance(inner, dict):
                delivery = inner.get("deliveryStatus")
        if isinstance(delivery, str) and delivery.lower() in {"sent", "delivered"}:
            return "sent", None
        if isinstance(delivery, str) and delivery.lower() in {"failed", "rejected", "error"}:
            return "failed", delivery
        if result.returncode != 0 and "sent" not in output.lower():
            return "failed", f"openclaw_exit_{result.returncode}"
        return "unknown", "unparseable_delivery_status"


_SENDERS = {"openclaw": OpenClawSender}
_SENDER_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def register_sender(name: str, factory) -> None:
    if not isinstance(name, str) or not _SENDER_NAME_RE.fullmatch(name):
        raise ValidationError("invalid_sender_name")
    _SENDERS[name] = factory


def _sender_factory(name: str):
    if name not in _SENDERS:
        raise ValidationError("unknown_notify_sender", detail={"name": name, "available": sorted(_SENDERS)})
    return _SENDERS[name]


def make_sender(conn=None) -> object:
    row = conn.execute("SELECT value FROM meta WHERE key='notify_sender'").fetchone() if conn is not None else None
    name = (row[0] if row else None) or os.environ.get("LABOPS_NOTIFY_SENDER") or "openclaw"
    return _sender_factory(name)()


def _ssl_contexts():
    contexts = []
    try:
        import truststore
        contexts.append(truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    except (ImportError, AttributeError, OSError):
        pass
    try:
        import certifi
        contexts.append(ssl.create_default_context(cafile=certifi.where()))
    except (ImportError, AttributeError, OSError):
        pass
    contexts.append(ssl.create_default_context())
    return contexts


class NtfySender:
    def __init__(self, topic: str, timeout: float = 10):
        if not isinstance(topic, str) or not topic.strip():
            raise ValidationError("invalid_ntfy_topic")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValidationError("invalid_ntfy_timeout")
        self.topic = topic.strip()
        self.timeout = timeout

    def __call__(self, text: str) -> str:
        first_line = text.splitlines()[0] if isinstance(text, str) and text.splitlines() else ""
        url = "https://ntfy.sh/" + urllib.parse.quote(self.topic, safe="")
        request = urllib.request.Request(
            url, data=first_line.encode("utf-8"),
            headers={"Title": "LabOps", "Tags": "warning"}, method="POST",
        )
        contexts = _ssl_contexts()
        for index, context in enumerate(contexts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout, context=context) as response:
                    code = response.getcode()
                    return "sent" if 200 <= code < 300 else "failed"
            except urllib.error.HTTPError as exc:
                return "sent" if 200 <= exc.code < 300 else "failed"
            except urllib.error.URLError as exc:
                if isinstance(exc.reason, ssl.SSLError) and index + 1 < len(contexts):
                    continue
                return "failed"
            except ssl.SSLError:
                if index + 1 < len(contexts):
                    continue
                return "failed"
            except (OSError, TimeoutError, ValueError):
                return "failed"
        return "failed"


_PHASE_LABEL = {
    "completed": "已完成",
    "interrupted": "已中断",
    "session_gone": "会话已关闭",
    "not_seen": "未送达",
    "timeout": "超时未完成",
    "failed": "发送失败",
    "unknown": "发送结果未知",
}


def _reply_text(view: dict, confidential: bool, label: str) -> str:
    who = {"claude": "Claude", "codex": "Codex"}[view["kind"]]
    detail = view["detail"]
    if confidential:
        name = "（机密仓库）"
    else:
        name = (detail.get("name") if view["kind"] == "claude" else None)
        name = name or detail.get("thread_id", "")[:8] or view["target"][:8]
    lines = [f"[LabOps · 派活回报] {who} · {name} · {_PHASE_LABEL[label]}", f"请求：{view['request_id']}"]
    if confidential:
        lines.append("正文不经微信发送；在电脑上用 desktop_status 查看。")
    elif view.get("reply"):
        lines.append(_truncate(view["reply"], REPLY_MAX_CHARS))
    elif view.get("hint"):
        lines.append(f"提示：{view['hint']}")
    return "\n".join(lines)


def scan_desktop_replies(conn, *, now_ts: str | None = None, process_info=None) -> int:
    timestamp = now_ts or now()
    scan_time = _parse_ts(timestamp)
    rows = conn.execute(
        "SELECT request_id,state,created_at FROM desktop_requests "
        "WHERE origin='wechat' AND reported_at IS NULL ORDER BY created_at"
    ).fetchall()
    queued = 0
    for row in rows:
        state = row["state"]
        request_id = row["request_id"]
        age_min = (scan_time - _parse_ts(row["created_at"])).total_seconds() / 60
        if state == "SENDING":
            continue
        try:
            view = desktop.desktop_status(conn, request_id=request_id, process_info=process_info)
        except Exception:
            continue

        phase = view.get("phase")
        if state == "FAILED":
            label = "failed"
        elif state == "UNKNOWN" and phase in (None, "not_seen"):
            if age_min < NOT_SEEN_REPORT_MINUTES:
                continue
            label = "unknown"
        elif phase in ("completed", "interrupted", "session_gone"):
            label = phase
        elif phase == "not_seen" and age_min >= NOT_SEEN_REPORT_MINUTES:
            label = "not_seen"
        elif age_min >= DISPATCH_TIMEOUT_MINUTES:
            label = "timeout"
        else:
            continue

        confidential = desktop.confidential_cwd(conn, view["detail"].get("cwd"))
        text = _reply_text(view, confidential, label)
        with tx(conn):
            current = conn.execute("SELECT reported_at FROM desktop_requests WHERE request_id=?",
                                   (request_id,)).fetchone()
            if current["reported_at"] is not None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO notifications(event_key,task_id,kind,text,status,created_at) "
                "VALUES(?,NULL,'desktop_reply',?,'PENDING',?)",
                (f"desktop:{request_id}", text, timestamp),
            )
            conn.execute("UPDATE desktop_requests SET reported_at=?,reported_phase=? WHERE request_id=?",
                         (timestamp, label, request_id))
            queued += 1
    return queued


def notify_user(conn, *, actor: str, text: str, request_id: str, now_ts: str | None = None) -> dict:
    if actor not in ("codex", "opus", "gemini"):
        raise ValidationError("actor_tool_not_allowed")
    if not desktop.REQUEST_ID_RE.fullmatch(request_id):
        raise ValidationError("invalid_request_id")
    body = text.strip()
    if not body:
        raise ValidationError("report_text_empty")
    if len(body) > AGENT_REPORT_MAX_CHARS:
        raise ValidationError("report_text_too_long", detail={"max": AGENT_REPORT_MAX_CHARS})

    key = f"agent:{actor}:{request_id}"
    timestamp = now_ts or now()
    with tx(conn):
        if conn.execute("SELECT 1 FROM notifications WHERE event_key=?", (key,)).fetchone():
            return {"event_key": key, "queued": False, "replayed": True}
        since = (_parse_ts(timestamp) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        count = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE kind='agent_report' AND event_key LIKE ? AND created_at>=?",
            (f"agent:{actor}:%", since),
        ).fetchone()[0]
        if count >= AGENT_REPORT_PER_HOUR:
            raise ValidationError("report_rate_limited", detail={"limit": AGENT_REPORT_PER_HOUR})
        who = {"codex": "Codex", "opus": "Claude", "gemini": "Gemini"}[actor]
        conn.execute(
            "INSERT INTO notifications(event_key,task_id,kind,text,status,created_at) "
            "VALUES(?,NULL,'agent_report',?,'PENDING',?)",
            (key, f"[LabOps · {who} 回报]\n{body}", timestamp),
        )
    return {"event_key": key, "queued": True, "replayed": False,
            "note": "sent on the next notify run (cron every 5 min)"}


def run(conn: sqlite3.Connection, sender=None, limit: int = 10, *, ntfy_sender=None,
        ntfy_topic: str | None = None) -> dict:
    runs = scan_runs(conn)
    queue = queue_tick(conn, now())
    scanned = scan(conn)
    desktop_reported = scan_desktop_replies(conn)
    merged = merge_pending(conn)
    counts = send_pending(conn, make_sender(conn) if sender is None else sender, limit=limit,
                          ntfy_sender=ntfy_sender, ntfy_topic=ntfy_topic)
    return {"scanned": scanned, "runs": runs, "queue": queue, "desktop_reported": desktop_reported, **merged, **counts}


def test_send(sender=None) -> dict:
    selected_sender = make_sender() if sender is None else sender
    try:
        outcome = selected_sender(TEST_MESSAGE)
        if isinstance(outcome, tuple) and len(outcome) == 2:
            status, error = outcome
        else:
            status, error = outcome, None
    except (TimeoutError, subprocess.TimeoutExpired):
        status, error = "unknown", "timeout"
    except Exception as exc:
        status, error = "unknown", type(exc).__name__
    if status != "sent":
        raise Blocked("notify_test_send_failed", detail={"status": status, "error": error})
    return {"status": status, "text": TEST_MESSAGE}


def list_notifications(conn: sqlite3.Connection, status: str | None = None) -> list[dict]:
    allowed = {"PENDING", "ACCEPTED", "FAILED", "UNKNOWN"}
    if status is not None and status not in allowed:
        raise ValidationError("invalid_notify_status", detail={"status": status})
    if status:
        rows = conn.execute("SELECT * FROM notifications WHERE status=? ORDER BY created_at,event_key", (status,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM notifications ORDER BY created_at,event_key").fetchall()
    return [dict(row) for row in rows]
