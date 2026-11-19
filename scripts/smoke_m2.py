import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SRC))

from labops import cli, mcp_server
from labops.canon import canonical_json
from labops.config import now
from labops.db import connect
from labops.evaluate import evaluate_retrieval
from labops.gate import client_restart_requirement
from labops.gitio import tree_of
from labops.ids import new_id
from labops.index import rebuild_index
from labops.search import search
from labops.tasks import create_task, open_round
from tests.helpers import commit_file, git, make_repo


def _master_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "-c", "init.defaultBranch=master", "init"],
                   capture_output=True, check=True)
    git(path, "config", "user.name", "labops-m2-smoke")
    git(path, "config", "user.email", "labops-m2-smoke@example.invalid")
    git(path, "config", "core.autocrlf", "false")
    (path / "README.txt").write_text("temporary LabOps M2 toy repository\n", encoding="utf-8")
    git(path, "add", "--all")
    git(path, "commit", "-m", "initial master")
    return git(path, "rev-parse", "HEAD").stdout.decode().strip()


def _bridge_state(root: Path) -> dict:
    return {path.relative_to(root).as_posix(): (path.stat().st_mtime_ns, path.read_bytes())
            for path in root.rglob("*") if path.is_file()}


def _add_finding_pair(conn, *, repo_id: str, task_id: str, base: str) -> tuple[str, str]:
    repo_path = Path(conn.execute("SELECT path FROM repos WHERE repo_id=?", (repo_id,)).fetchone()[0])
    create_task(conn, repo_id=repo_id, task_id=task_id, goal="temporary supersession test task",
                profile={"schema": 1}, base=base)
    round_id = open_round(conn, task_id=task_id, actor="codex", role="implement")["round_id"]
    old_id, pass_id = new_id("rv"), new_id("rv")
    tree = tree_of(repo_path, base)
    columns = ("review_id,task_id,round_id,reviewer,state,verdict,independence,base_commit,reviewed_commit,"
               "reviewed_tree,scope_id,acceptance_profile_hash,findings_json,created_at,updated_at")
    old_findings = [{"id": "F-OLD", "severity": "major", "path": "toy.py", "text": "oldvector cache policy"}]
    conn.execute(
        f"INSERT INTO reviews({columns}) VALUES(?,?,?,'opus','REWORK_REQUIRED','REWORK_REQUIRED','INDEPENDENT',?,?,?,?,?,?,?,?)",
        (old_id, task_id, round_id, base, base, tree, "scope", "profile",
         canonical_json(old_findings).decode(), "2026-10-20T01:00:00Z", "2026-10-20T01:00:00Z"),
    )
    conn.execute(
        f"INSERT INTO reviews({columns}) VALUES(?,?,?,'opus','PASS','PASS','INDEPENDENT',?,?,?,?,?,?,?,?)",
        (pass_id, task_id, round_id, base, base, tree, "scope", "profile", "[]",
         "2026-10-20T02:00:00Z", "2026-10-20T02:00:00Z"),
    )
    return old_id, pass_id


def main() -> int:
    checks = {
        "ok": True,
        "two_char_hit": False,
        "abbr_hit": False,
        "exact_id_hit": False,
        "superseded_filtered": False,
        "secretary_redacted": False,
        "legacy_dup_kept": False,
        "onboard_master_ok": False,
        "restart_flag_correct": False,
        "eval_recall_at_5": False,
    }
    with tempfile.TemporaryDirectory(prefix="labops-smoke-m2-") as temporary:
        root = Path(temporary)
        os.environ["LABOPS_HOME"] = str(root / "home")
        os.environ["LABOPS_ACTOR"] = "codex"
        os.environ["LABOPS_CODE_ROOT"] = str(SRC)
        repo = root / "master-toy"
        base = _master_repo(repo)
        bridge = repo / ".ai-bridge"
        (bridge / "review").mkdir(parents=True)
        status = (f"owner: opus\nstate: review\ntask: T20261020-m2-smoke\nround: 1\n"
                  f"updated: {now()}\nbaseline: {base}\nspec: private/m2-spec.md\n\n")
        (bridge / "agent-status.md").write_text(status, encoding="utf-8")
        (bridge / "decisions.md").write_text(
            "## D-20261020-1 positional encoding\n位置编码 KV keeps tokens aligned for multi-hop retrieval.\n"
            "## D-20261020-1 duplicate encoding record\n位置编码 KV has a second traceable decision line.\n"
            "## D-20261020-2 cache design\n缓存状态 is stored for the next decode step.\n"
            "## D-20261020-3 KV abbreviation\n位置编码 KV means key-value cache lookup.\n"
            "## D-20261020-4 TTL abbreviation\nTTL controls how long cache entries stay valid.\n"
            "## D-20261020-5 error fallback\n错误回退 keeps retrieval useful on partial data.\n"
            "## D-20261020-6 retrieval evaluation\n评测工具 reports recall and reciprocal rank.\n"
            "## D-20261020-7 index update\n索引更新 skips documents with unchanged hashes.\n"
            "## D-20261020-8 security boundary\n保密仓库 results are redacted for secretary.\n"
            "## D-20261020-9 superseding cache\n取代旧缓存 strategy and cache cleanup.\n",
            encoding="utf-8")
        (bridge / "current-plan.md").write_text(
            "# Toy M2 plan\n\n## onboardmarker\nTemporary index planning text.\n", encoding="utf-8")
        review = bridge / "review" / "T20261020-m2-smoke.md"
        review.write_text("# Toy review title\nprivate toy-only body\n", encoding="utf-8")
        bridge_before = _bridge_state(bridge)

        onboard_args = ["onboard", "--repo-id", "toy", "--path", str(repo), "--main-ref", "auto",
                        "--confidential", "--legacy-bridge", ".ai-bridge",
                        "--backup-root", str(root / "backups"), "--index"]
        onboard_result = cli.dispatch(cli.build_parser().parse_args(onboard_args))
        conn = connect(Path(os.environ["LABOPS_HOME"]) / "labops.db")
        try:
            row = conn.execute("SELECT main_ref,trusted_for_exec,python,confidential FROM repos WHERE repo_id='toy'").fetchone()
            checks["onboard_master_ok"] = (onboard_result["main_ref"] == "refs/heads/master"
                                            and tuple(row) == ("refs/heads/master", 0, None, 1)
                                            and onboard_result["steps"]["backup"]["verify_restore"]["ok"])
            duplicates = conn.execute(
                "SELECT COUNT(*) FROM legacy_records WHERE repo_id='toy' AND kind='decision' AND ref='D-20261020-1'"
            ).fetchone()[0]
            checks["legacy_dup_kept"] = duplicates == 2 and _bridge_state(bridge) == bridge_before

            task_id = "T20261020-m2-smoke"
            old_review, pass_review = _add_finding_pair(conn, repo_id="toy", task_id=task_id, base=base)
            indexed = rebuild_index(conn, "toy")
            two_char = search(conn, "编码", repo_id="toy")
            abbreviation = search(conn, "KV", repo_id="toy")
            exact = search(conn, "D-20261020-3", repo_id="toy")
            checks["two_char_hit"] = any(item["ref"] == "D-20261020-1" for item in two_char["results"])
            checks["abbr_hit"] = any(item["ref"] == "D-20261020-3" for item in abbreviation["results"])
            checks["exact_id_hit"] = any(item["ref"] == "D-20261020-3" for item in exact["results"])
            old_results = search(conn, "oldvector cache", repo_id="toy")
            include_old = search(conn, "oldvector cache", repo_id="toy", include_superseded=True)
            checks["superseded_filtered"] = (not any(item["ref"].startswith(old_review + "#") for item in old_results["results"])
                                              and any(item["ref"] == old_review + "#F-OLD" and not item["valid"]
                                                      for item in include_old["results"]))
            secretary = search(conn, "编码", repo_id="toy", actor="secretary")
            checks["secretary_redacted"] = bool(secretary["results"]) and all(
                set(item) == {"repo_id", "kind", "ref", "ts"} for item in secretary["results"])

            # Exercise the added MCP tool through the actual worker subprocess.
            mcp_result = mcp_server.search_history(query=task_id, repo_id="toy")
            mcp_search_ok = any(item["kind"] == "task" and item["ref"] == task_id
                                for item in mcp_result["results"])

            eval_result = evaluate_retrieval(conn, file=ROOT / "examples" / "eval_toy.jsonl",
                                             ks=[1, 5, 10], out=root / "eval")
            checks["eval_recall_at_5"] = eval_result["report"]["metrics"]["recall@5"] >= 0.8
        finally:
            conn.close()

        restart_repo = root / "restart-toy"
        restart_base = make_repo(restart_repo, files={
            "src/labops/mcp_server.py": "def old_tool(value):\n    return value\n",
            "src/labops/mcp_worker.py": "TOOLS = {'old_tool': ('value',)}\n",
        })
        restart_candidate = commit_file(restart_repo, "src/labops/mcp_server.py",
                                        "def new_tool(value):\n    return value\n", "add MCP search tool")
        restart = client_restart_requirement(restart_repo, restart_base, restart_candidate)
        checks["restart_flag_correct"] = restart == {"client_restart_required": True,
                                                      "reason": "mcp_server_changed"} and mcp_search_ok
        checks["ok"] = all(value for key, value in checks.items() if key != "ok")
    print(json.dumps(checks, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if checks["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
