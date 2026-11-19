import argparse
import json
import sys
from pathlib import Path

from . import desktop, gitio
from . import spool as spool_module
from .backup import list_backups, run_backup, verify_restore
from .approvals import approve_frozen, approve_self_review, require_human
from .config import initialize, now
from .context import task_context
from .db import connect, migrate, tx
from .errors import LabOpsError, UsageError, ValidationError
from .gate import approve_integration, gate_check, propose_integration
from .handoff import build_handoff
from .history import query_history
from .index import index_stats, rebuild_index
from .next_task import next_task
from .onboard import onboard
from .notify import (list_notifications, mute_kind, notify_config, run as notify_run,
                     set_alert_detail, test_send as notify_test_send, unmute_kind)
from .legacy import audit_legacy, import_legacy
from .progress import progress_summary
from .review import open_review, submit_review
from .search import search as search_history_cli
from .evaluate import check_eval_file, evaluate_retrieval
from .gpuq import queue_cancel, queue_choose, queue_config, queue_list, queue_register
from .reconcile import reconcile
from .runs import adopt_process, list_runs, preview_progress_rules
from .tasks import (abandon_task, add_repo, create_task, create_worktree, open_round,
                    list_repos, prune_worktrees, set_confidential, set_policy, set_repo_options,
                    set_handoff_rules, set_task_brief, snapshot, submit_changes, sync_main, task_row)
from .verify import verify_round
from .worker import run_once as worker_run_once


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def _json_file(path: str):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError("invalid_json_file", detail={"path": path, "error": str(exc)}) from exc


def _text_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValidationError("invalid_utf8_text_file", detail={"path": path}) from exc


def _row_dict(row):
    return dict(row) if row is not None else None


def _db():
    conn = connect()
    migrate(conn)
    return conn


def _task_events(conn, task_id: str) -> list[dict]:
    task = task_row(conn, task_id)
    round_ids = [row[0] for row in conn.execute("SELECT round_id FROM rounds WHERE task_id=?", (task_id,))]
    review_ids = [row[0] for row in conn.execute("SELECT review_id FROM reviews WHERE task_id=?", (task_id,))]
    integration_ids = [row[0] for row in conn.execute("SELECT integration_id FROM integrations WHERE task_id=?", (task_id,))]
    ids = [task_id, *round_ids, *review_ids, *integration_ids]
    placeholders = ",".join("?" for _ in ids)
    return [dict(row) for row in conn.execute(f"SELECT * FROM events WHERE entity_id IN ({placeholders}) ORDER BY seq", ids)]


def _refs_audit(conn, task_id: str) -> dict:
    task = task_row(conn, task_id)
    repo = conn.execute("SELECT * FROM repos WHERE repo_id=?", (task["repo_id"],)).fetchone()
    repo_path = Path(repo["path"])
    rounds = conn.execute("SELECT * FROM rounds WHERE task_id=? ORDER BY n", (task_id,)).fetchall()
    differences = []
    entries = []
    for rnd in rounds:
        if not rnd["head_commit"]:
            continue
        ref = f"refs/labops/{task_id}/r{rnd['n']}/{rnd['actor']}/submitted"
        actual = gitio.read_ref(repo_path, ref)
        trailers = gitio.commit_trailers(repo_path, rnd["head_commit"])
        expected_trailers = {"LabOps-Task": task_id, "LabOps-Round": str(rnd["n"]), "LabOps-Actor": rnd["actor"]}
        entry = {"round_id": rnd["round_id"], "head_commit": rnd["head_commit"], "head_tree": rnd["head_tree"],
                 "ref": ref, "ref_value": actual, "trailers": trailers}
        entries.append(entry)
        if actual != rnd["head_commit"]:
            differences.append({"round_id": rnd["round_id"], "reason": "submitted_ref_mismatch", "expected": rnd["head_commit"], "actual": actual})
        missing = {key: value for key, value in expected_trailers.items() if trailers.get(key) != value}
        if missing:
            differences.append({"round_id": rnd["round_id"], "reason": "commit_trailer_mismatch", "missing_or_wrong": missing})
    return {"task_id": task_id, "ok": not differences, "rounds": entries, "differences": differences}


def _status(conn, task_id: str | None) -> dict:
    if task_id:
        task = task_row(conn, task_id)
        return {"task": _row_dict(task),
                "rounds": [dict(r) for r in conn.execute("SELECT * FROM rounds WHERE task_id=? ORDER BY n", (task_id,))],
                "reviews": [dict(r) for r in conn.execute("SELECT * FROM reviews WHERE task_id=? ORDER BY created_at", (task_id,))],
                "integrations": [dict(r) for r in conn.execute("SELECT * FROM integrations WHERE task_id=? ORDER BY created_at", (task_id,))]}
    return {"tasks": [dict(r) for r in conn.execute("SELECT task_id,repo_id,goal,state,base_commit,created_at,updated_at FROM tasks ORDER BY created_at DESC")],
            "repos": [dict(r) for r in conn.execute("SELECT repo_id,path,main_ref,trusted_for_exec,policy_hash FROM repos ORDER BY repo_id")]}


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(prog="labops")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=JsonArgumentParser)
    p = sub.add_parser("init"); p.add_argument("--test-instance", action="store_true")
    p = sub.add_parser("repo"); s = p.add_subparsers(dest="repo_command", required=True, parser_class=JsonArgumentParser)
    q = s.add_parser("add"); q.add_argument("--repo-id", required=True); q.add_argument("--path", required=True)
    q.add_argument("--main-ref", default="refs/heads/main"); q.add_argument("--worktree-root"); q.add_argument("--trusted-for-exec", action="store_true")
    q.add_argument("--confidential", action="store_true")
    q = s.add_parser("set"); q.add_argument("--repo-id", required=True); q.add_argument("--confidential", choices=("on", "off"))
    q.add_argument("--alert-detail", choices=("on", "off"))
    q.add_argument("--python"); q.add_argument("--backup-root"); q.add_argument("--backup-keep", type=int); q.add_argument("--legacy-bridge")
    q.add_argument("--run-watch", action="append")
    q.add_argument("--progress-rule", action="append")
    q = s.add_parser("progress-preview"); q.add_argument("--repo-id", required=True)
    s.add_parser("list")
    p = sub.add_parser("queue"); s = p.add_subparsers(dest="queue_command", required=True, parser_class=JsonArgumentParser)
    s.add_parser("list")
    q = s.add_parser("config"); q.add_argument("--entry-root", action="append")
    q.add_argument("--clear-entry-roots", action="store_true"); q.add_argument("--launcher")
    q.add_argument("--clear-launcher", action="store_true")
    q = s.add_parser("register"); q.add_argument("--repo-id", required=True); q.add_argument("--title", required=True)
    q.add_argument("--entrypoint", required=True); q.add_argument("--context", required=True)
    q.add_argument("--context-commit"); q.add_argument("--draft-id"); q.add_argument("--run-prefix")
    q.add_argument("--config", action="append")
    q = s.add_parser("choose"); q.add_argument("--queue-id", required=True)
    q.add_argument("--choice", choices=("manual", "auto", "now", "cancel"), required=True)
    q = s.add_parser("cancel"); q.add_argument("--queue-id", required=True); q.add_argument("--reason", required=True)
    p = sub.add_parser("backup"); s = p.add_subparsers(dest="backup_command", required=True, parser_class=JsonArgumentParser)
    q = s.add_parser("run"); group = q.add_mutually_exclusive_group(required=True); group.add_argument("--repo-id"); group.add_argument("--all", action="store_true")
    q = s.add_parser("verify-restore"); q.add_argument("--repo-id", required=True); q.add_argument("--backup-id")
    q = s.add_parser("list"); q.add_argument("--repo-id", required=True)
    p = sub.add_parser("legacy"); s = p.add_subparsers(dest="legacy_command", required=True, parser_class=JsonArgumentParser)
    q = s.add_parser("import"); q.add_argument("--repo-id", required=True); q.add_argument("--verbose", action="store_true")
    q = s.add_parser("audit"); group = q.add_mutually_exclusive_group(required=True); group.add_argument("--repo-id"); group.add_argument("--all", action="store_true")
    q.add_argument("--verbose", action="store_true")
    p = sub.add_parser("onboard"); p.add_argument("--repo-id", required=True); p.add_argument("--path", required=True)
    p.add_argument("--main-ref", default="auto"); p.add_argument("--confidential", action="store_true")
    p.add_argument("--legacy-bridge"); p.add_argument("--backup-root"); p.add_argument("--backup-keep", type=int)
    p.add_argument("--index", action="store_true")
    p = sub.add_parser("index"); s = p.add_subparsers(dest="index_command", required=True, parser_class=JsonArgumentParser)
    q = s.add_parser("rebuild"); group = q.add_mutually_exclusive_group(required=True); group.add_argument("--repo-id"); group.add_argument("--all", action="store_true")
    s.add_parser("stats")
    p = sub.add_parser("search"); p.add_argument("query"); p.add_argument("--repo-id"); p.add_argument("--kind", action="append")
    p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("eval"); s = p.add_subparsers(dest="eval_command", required=True, parser_class=JsonArgumentParser)
    q = s.add_parser("retrieval"); q.add_argument("--file", required=True); q.add_argument("--k", default="1,5,10"); q.add_argument("--out"); q.add_argument("--repo-filter")
    q = s.add_parser("check-file"); q.add_argument("file")
    p = sub.add_parser("report"); s = p.add_subparsers(dest="report_command", required=True, parser_class=JsonArgumentParser)
    q = s.add_parser("stats"); q.add_argument("--since"); q.add_argument("--out")
    p = sub.add_parser("policy"); s = p.add_subparsers(dest="policy_command", required=True, parser_class=JsonArgumentParser)
    q = s.add_parser("set"); q.add_argument("--repo-id", required=True); q.add_argument("--file", required=True)
    p = sub.add_parser("task"); s = p.add_subparsers(dest="task_command", required=True, parser_class=JsonArgumentParser)
    q = s.add_parser("create"); q.add_argument("--repo-id", required=True); q.add_argument("--task-id", required=True)
    q.add_argument("--goal", required=True); q.add_argument("--profile", required=True); q.add_argument("--base")
    q.add_argument("--brief-file")
    q = s.add_parser("brief"); q.add_argument("--task-id", required=True); q.add_argument("--set-file", required=True)
    q = s.add_parser("abandon"); q.add_argument("--task-id", required=True)
    p = sub.add_parser("round"); s = p.add_subparsers(dest="round_command", required=True, parser_class=JsonArgumentParser)
    q = s.add_parser("open"); q.add_argument("--task-id", required=True); q.add_argument("--actor", choices=("codex", "opus"), required=True)
    q.add_argument("--role", choices=("implement", "fix"), required=True)
    q = s.add_parser("sync"); q.add_argument("--round", required=True); q.add_argument("--actor", choices=("codex", "opus"), required=True)
    p = sub.add_parser("wt"); s = p.add_subparsers(dest="wt_command", required=True, parser_class=JsonArgumentParser)
    q = s.add_parser("create"); q.add_argument("--round", required=True)
    q = s.add_parser("prune"); q.add_argument("--task-id", required=True); q.add_argument("--apply", action="store_true")
    p = sub.add_parser("snapshot"); p.add_argument("--round", required=True); p.add_argument("--actor", required=True)
    p.add_argument("--message", required=True); p.add_argument("--files", nargs="+", required=True)
    p = sub.add_parser("submit"); p.add_argument("--round", required=True); p.add_argument("--actor", required=True)
    p.add_argument("--commit", required=True); p.add_argument("--request-id", required=True); p.add_argument("--notes")
    p = sub.add_parser("verify"); p.add_argument("--round", required=True); p.add_argument("--retry", action="store_true")
    p = sub.add_parser("review"); s = p.add_subparsers(dest="review_command", required=True, parser_class=JsonArgumentParser)
    q = s.add_parser("open"); q.add_argument("--round", required=True); q.add_argument("--reviewer", choices=("codex", "opus", "user"), required=True)
    q = s.add_parser("submit"); q.add_argument("--review", required=True); q.add_argument("--actor", choices=("codex", "opus", "user"), required=True)
    q.add_argument("--verdict", choices=("PASS", "PASS_WITH_NOTES", "REWORK_REQUIRED", "BLOCKED", "INCONCLUSIVE"), required=True)
    q.add_argument("--request-id", required=True); q.add_argument("--findings")
    p = sub.add_parser("approve"); s = p.add_subparsers(dest="approve_command", required=True, parser_class=JsonArgumentParser)
    q = s.add_parser("self-review"); q.add_argument("--task-id", required=True)
    q = s.add_parser("frozen"); q.add_argument("--task-id", required=True); q.add_argument("--decision", required=True)
    p = sub.add_parser("gate"); s = p.add_subparsers(dest="gate_command", required=True, parser_class=JsonArgumentParser)
    q = s.add_parser("check"); q.add_argument("--task-id", required=True)
    p = sub.add_parser("integrate"); s = p.add_subparsers(dest="integrate_command", required=True, parser_class=JsonArgumentParser)
    q = s.add_parser("propose"); q.add_argument("--task-id", required=True)
    q = s.add_parser("approve"); q.add_argument("--integration", required=True)
    p = sub.add_parser("handoff"); s = p.add_subparsers(dest="handoff_command", required=True, parser_class=JsonArgumentParser)
    q = s.add_parser("build"); q.add_argument("--round", required=True)
    q = s.add_parser("rules"); q.add_argument("--set-file", required=True)
    p = sub.add_parser("reconcile"); p.add_argument("--repo-id")
    p = sub.add_parser("spool"); s = p.add_subparsers(dest="spool_command", required=True, parser_class=JsonArgumentParser)
    s.add_parser("drain")
    p = sub.add_parser("worker"); p.add_argument("--once", action="store_true", required=True)
    p = sub.add_parser("context"); group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--task-id"); group.add_argument("--round")
    p.add_argument("--budget", type=int, default=4000)
    p = sub.add_parser("history"); group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--task-id"); group.add_argument("--sha")
    p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("refs-audit"); p.add_argument("--task-id", required=True)
    p = sub.add_parser("status"); p.add_argument("--task-id")
    p = sub.add_parser("events"); p.add_argument("--task-id", required=True)
    p = sub.add_parser("progress"); p.add_argument("--since-hours", type=int, default=24)
    p = sub.add_parser("runs"); s = p.add_subparsers(dest="runs_command", required=True, parser_class=JsonArgumentParser)
    q = s.add_parser("list"); q.add_argument("--repo-id")
    q = s.add_parser("adopt"); q.add_argument("--repo-id", required=True); q.add_argument("--pid", type=int, required=True)
    q.add_argument("--title", required=True); q.add_argument("--log-path"); q.add_argument("--watch", action="append")
    p = sub.add_parser("next"); p.add_argument("--actor", choices=("codex", "opus"), required=True)
    p = sub.add_parser("notify"); s = p.add_subparsers(dest="notify_command", required=True, parser_class=JsonArgumentParser)
    q = s.add_parser("run"); q.add_argument("--limit", type=int, default=10)
    q = s.add_parser("list"); q.add_argument("--status", choices=("PENDING", "ACCEPTED", "FAILED", "UNKNOWN"))
    q = s.add_parser("config"); q.add_argument("--urgent-kinds"); q.add_argument("--digest-minutes", type=int)
    q.add_argument("--llm-rewrite", choices=("on", "off")); q.add_argument("--sender")
    q = s.add_parser("mute"); q.add_argument("kind")
    q = s.add_parser("unmute"); q.add_argument("kind")
    s.add_parser("test-send")
    p = sub.add_parser("desktop")
    s = p.add_subparsers(dest="desktop_command", required=True, parser_class=JsonArgumentParser)
    q = s.add_parser("list")
    q.add_argument("--kind", required=True)
    q.add_argument("--limit", type=int, default=20)
    q = s.add_parser("send")
    q.add_argument("--kind", required=True)
    q.add_argument("--target", required=True)
    q.add_argument("--request-id", required=True)
    q.add_argument("--repo-id")
    group = q.add_mutually_exclusive_group(required=True)
    group.add_argument("--prompt")
    group.add_argument("--prompt-file")
    q = s.add_parser("status")
    q.add_argument("--request-id", required=True)
    return parser


def dispatch(args, *, notify_sender=None) -> dict:
    if args.command == "init":
        initialize(test_instance=args.test_instance)
        conn = _db()
        conn.close()
        return {"initialized": True, "test_instance": args.test_instance}
    conn = _db()
    try:
        if args.command == "desktop":
            if args.desktop_command == "list":
                return desktop.desktop_list(conn, kind=args.kind, limit=args.limit)
            if args.desktop_command == "send":
                prompt = args.prompt if args.prompt is not None else _text_file(args.prompt_file)
                return desktop.desktop_send(conn, kind=args.kind, target=args.target, prompt=prompt,
                                            request_id=args.request_id, repo_id=args.repo_id, actor="opus")
            return desktop.desktop_status(conn, request_id=args.request_id)
        if args.command == "repo" and args.repo_command == "add":
            if args.trusted_for_exec:
                grant = require_human(f"Trust repo {args.repo_id} for verification? Type the repo ID:", args.repo_id)
            else:
                grant = None
            result = add_repo(conn, repo_id=args.repo_id, path=Path(args.path), main_ref=args.main_ref,
                              worktree_root=Path(args.worktree_root) if args.worktree_root else None,
                              trusted_for_exec=args.trusted_for_exec, confidential=args.confidential)
            if grant:
                result["granted_via"] = grant
            return result
        if args.command == "repo" and args.repo_command == "set":
            result = {"repo_id": args.repo_id}
            changed = False
            alert_changed = False
            if args.confidential is not None:
                confidential = args.confidential == "on"
                if not confidential:
                    require_human(f"Mark repo {args.repo_id} non-confidential? Type the repo ID:", args.repo_id)
                result.update(set_confidential(conn, args.repo_id, confidential))
                changed = True
            if args.python is not None:
                require_human(f"Change the verification interpreter for {args.repo_id}? Type the repo ID:", args.repo_id)
            if any(value is not None for value in (args.python, args.backup_root, args.backup_keep,
                                                    args.legacy_bridge, args.run_watch, args.progress_rule)):
                result.update(set_repo_options(conn, args.repo_id, python=args.python, backup_root=args.backup_root,
                                               backup_keep=args.backup_keep, legacy_bridge=args.legacy_bridge,
                                               run_watch=args.run_watch, progress_rule=args.progress_rule))
                changed = True
            if args.alert_detail is not None:
                alert_result = set_alert_detail(conn, args.repo_id, args.alert_detail == "on")
                result["alert_detail"] = alert_result["alert_detail"]
                alert_changed = alert_result["changed"]
                changed = True
            if not changed:
                return set_repo_options(conn, args.repo_id)
            if args.alert_detail is not None:
                result["changed"] = bool(result.get("changed", False) or alert_changed)
            return result
        if args.command == "repo" and args.repo_command == "list":
            return {"repos": list_repos(conn)}
        if args.command == "repo" and args.repo_command == "progress-preview":
            return preview_progress_rules(conn, args.repo_id)
        if args.command == "queue" and args.queue_command == "config":
            return queue_config(conn, entry_roots=args.entry_root, clear_entry_roots=args.clear_entry_roots,
                                launcher=args.launcher, clear_launcher=args.clear_launcher)
        if args.command == "queue" and args.queue_command == "list":
            return queue_list(conn)
        if args.command == "queue" and args.queue_command == "register":
            return queue_register(conn, repo_id=args.repo_id, title=args.title, entrypoint=args.entrypoint,
                                  context=args.context, context_commit=args.context_commit,
                                  draft_id=args.draft_id, run_prefix=args.run_prefix, created_by="cli",
                                  config_paths=args.config)
        if args.command == "queue" and args.queue_command == "choose":
            return queue_choose(conn, queue_id=args.queue_id, choice=args.choice, actor="cli")
        if args.command == "queue" and args.queue_command == "cancel":
            return queue_cancel(conn, queue_id=args.queue_id, reason=args.reason, actor="cli")
        if args.command == "backup" and args.backup_command == "run":
            if args.all:
                rows = conn.execute("SELECT repo_id FROM repos WHERE backup_root IS NOT NULL ORDER BY repo_id").fetchall()
                return {"backups": [run_backup(conn, row["repo_id"]) for row in rows]}
            return run_backup(conn, args.repo_id)
        if args.command == "backup" and args.backup_command == "verify-restore":
            try:
                result = verify_restore(conn, args.repo_id, backup_id=args.backup_id)
            except LabOpsError as exc:
                record = {"repo_id": args.repo_id, "backup_id": args.backup_id, "ok": False,
                          "error": exc.code, "checked_at": now()}
                with tx(conn):
                    conn.execute("INSERT INTO meta(key,value) VALUES('last_verify_restore',?) "
                                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                 (json.dumps(record, ensure_ascii=False, sort_keys=True),))
                raise
            record = {"repo_id": args.repo_id, "backup_id": result["backup_id"],
                      "ok": result["ok"], "checked_at": now()}
            with tx(conn):
                conn.execute("INSERT INTO meta(key,value) VALUES('last_verify_restore',?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (json.dumps(record, ensure_ascii=False, sort_keys=True),))
            return result
        if args.command == "backup" and args.backup_command == "list":
            return {"backups": list_backups(conn, args.repo_id)}
        if args.command == "legacy" and args.legacy_command == "import":
            result = import_legacy(conn, args.repo_id)
            if args.verbose:
                return result
            return {"imported": result["imported"], "by_kind": result["by_kind"]}
        if args.command == "legacy" and args.legacy_command == "audit":
            if args.all:
                rows = conn.execute("SELECT repo_id FROM repos WHERE legacy_bridge IS NOT NULL ORDER BY repo_id").fetchall()
                audits = [audit_legacy(conn, row["repo_id"]) for row in rows]
                if args.verbose:
                    return {"audits": audits}
                totals = {}
                for item in audits:
                    for kind, count in item["by_kind"].items():
                        totals[kind] = totals.get(kind, 0) + count
                return {"imported": sum(item["imported"] for item in audits), "by_kind": totals}
            result = audit_legacy(conn, args.repo_id)
            if args.verbose:
                return result
            return {"imported": result["imported"], "by_kind": result["by_kind"]}
        if args.command == "onboard":
            return onboard(conn, repo_id=args.repo_id, path=Path(args.path), main_ref=args.main_ref,
                           confidential=args.confidential, legacy_bridge=args.legacy_bridge,
                           backup_root=args.backup_root, backup_keep=args.backup_keep, index=args.index)
        if args.command == "index" and args.index_command == "rebuild":
            if args.all:
                return rebuild_index(conn)
            return rebuild_index(conn, args.repo_id)
        if args.command == "index" and args.index_command == "stats":
            return index_stats(conn)
        if args.command == "search":
            return search_history_cli(conn, args.query, repo_id=args.repo_id, kinds=args.kind, limit=args.limit)
        if args.command == "eval" and args.eval_command == "retrieval":
            try:
                ks = [int(value.strip()) for value in args.k.split(",") if value.strip()]
            except ValueError as exc:
                raise UsageError("invalid_eval_k") from exc
            return evaluate_retrieval(conn, file=args.file, ks=ks, out=args.out, repo_filter=args.repo_filter)
        if args.command == "eval" and args.eval_command == "check-file":
            return check_eval_file(conn, file=args.file)
        if args.command == "report" and args.report_command == "stats":
            from .report import stats_report, write_markdown
            report = stats_report(conn, since=args.since)
            result = {"report": report}
            if args.out is not None:
                result["markdown_path"] = write_markdown(report, args.out)
            return result
        if args.command == "policy" and args.policy_command == "set":
            return set_policy(conn, args.repo_id, _json_file(args.file))
        if args.command == "task" and args.task_command == "create":
            return create_task(conn, repo_id=args.repo_id, task_id=args.task_id, goal=args.goal,
                               profile=_json_file(args.profile), base=args.base,
                               brief=_text_file(args.brief_file) if args.brief_file is not None else None)
        if args.command == "task" and args.task_command == "brief":
            return set_task_brief(conn, args.task_id, _text_file(args.set_file))
        if args.command == "task" and args.task_command == "abandon":
            return abandon_task(conn, args.task_id)
        if args.command == "round" and args.round_command == "open":
            return open_round(conn, task_id=args.task_id, actor=args.actor, role=args.role)
        if args.command == "round" and args.round_command == "sync":
            return sync_main(conn, round_id=args.round, actor=args.actor)
        if args.command == "wt" and args.wt_command == "create":
            return create_worktree(conn, args.round)
        if args.command == "wt" and args.wt_command == "prune":
            return prune_worktrees(conn, args.task_id, apply=args.apply)
        if args.command == "snapshot":
            return snapshot(conn, round_id=args.round, files=args.files, message=args.message, actor=args.actor)
        if args.command == "submit":
            notes = _json_file(args.notes) if args.notes else None
            return submit_changes(conn, round_id=args.round, actor=args.actor, commit=args.commit,
                                  client_request_id=args.request_id, notes=notes)
        if args.command == "verify":
            return verify_round(conn, args.round, retry=args.retry)
        if args.command == "review" and args.review_command == "open":
            return open_review(conn, args.round, args.reviewer)
        if args.command == "review" and args.review_command == "submit":
            return submit_review(conn, review_id=args.review, actor=args.actor, verdict=args.verdict,
                                 findings=_json_file(args.findings) if args.findings else [], client_request_id=args.request_id)
        if args.command == "approve" and args.approve_command == "self-review":
            return approve_self_review(conn, args.task_id)
        if args.command == "approve" and args.approve_command == "frozen":
            return approve_frozen(conn, args.task_id, args.decision)
        if args.command == "gate" and args.gate_command == "check":
            return gate_check(conn, args.task_id)
        if args.command == "integrate" and args.integrate_command == "propose":
            return propose_integration(conn, args.task_id)
        if args.command == "integrate" and args.integrate_command == "approve":
            return approve_integration(conn, args.integration)
        if args.command == "handoff" and args.handoff_command == "build":
            json_path, md_path = build_handoff(conn, args.round)
            return {"json_path": str(json_path), "md_path": str(md_path)}
        if args.command == "handoff" and args.handoff_command == "rules":
            return set_handoff_rules(conn, _text_file(args.set_file))
        if args.command == "reconcile":
            return reconcile(conn, args.repo_id)
        if args.command == "spool" and args.spool_command == "drain":
            return spool_module.drain(conn)
        if args.command == "worker":
            return worker_run_once(conn)
        if args.command == "context":
            return task_context(conn, task_id=args.task_id, round_id=args.round, budget_tokens=args.budget)
        if args.command == "history":
            return query_history(conn, task_id=args.task_id, sha=args.sha, limit=args.limit)
        if args.command == "refs-audit":
            return _refs_audit(conn, args.task_id)
        if args.command == "status":
            return _status(conn, args.task_id)
        if args.command == "events":
            return {"task_id": args.task_id, "events": _task_events(conn, args.task_id)}
        if args.command == "progress":
            return progress_summary(conn, since_hours=args.since_hours)
        if args.command == "runs" and args.runs_command == "list":
            return {"runs": list_runs(conn, repo_id=args.repo_id)}
        if args.command == "runs" and args.runs_command == "adopt":
            return adopt_process(conn, repo_id=args.repo_id, pid=args.pid, title=args.title,
                                 log_path=args.log_path, watch_globs=args.watch, actor="cli")
        if args.command == "next":
            return next_task(conn, args.actor)
        if args.command == "notify" and args.notify_command == "run":
            return notify_run(conn, sender=notify_sender, limit=args.limit)
        if args.command == "notify" and args.notify_command == "list":
            return {"notifications": list_notifications(conn, args.status)}
        if args.command == "notify" and args.notify_command == "config":
            return notify_config(conn, urgent_kinds=args.urgent_kinds.split(",") if args.urgent_kinds is not None else None,
                                 digest_minutes=args.digest_minutes, llm_rewrite=args.llm_rewrite, sender=args.sender)
        if args.command == "notify" and args.notify_command == "mute":
            return mute_kind(conn, args.kind)
        if args.command == "notify" and args.notify_command == "unmute":
            return unmute_kind(conn, args.kind)
        if args.command == "notify" and args.notify_command == "test-send":
            return notify_test_send(notify_sender)
        raise UsageError("unknown_command")
    finally:
        conn.close()


def main(argv=None) -> int:
    try:
        args = build_parser().parse_args(argv)
        result = dispatch(args)
        payload = {"ok": True, **result}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except LabOpsError as exc:
        payload = {"ok": False, "error": exc.code, "message": str(exc), "detail": exc.detail}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return exc.exit
    except KeyboardInterrupt:
        payload = {"ok": False, "error": "interrupted", "message": "interrupted", "detail": {}}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 130


if __name__ == "__main__":
    sys.exit(main())
