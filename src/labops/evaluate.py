import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import home
from .errors import ValidationError
from .search import search


def _metrics(rows: list[dict], ks: list[int]) -> dict:
    count = len(rows)
    result = {f"recall@{k}": 0.0 for k in ks}
    result["mrr"] = 0.0
    if not count:
        return {"queries": 0, **result}
    for row in rows:
        expected = set(tuple(item) for item in row["expected"])
        retrieved = [(item.get("repo_id"), item.get("ref")) for item in row["results"]]
        for k in ks:
            hits = len(expected.intersection(retrieved[:k]))
            result[f"recall@{k}"] += hits / len(expected) if expected else 0.0
        rank = next((index for index, item in enumerate(retrieved, start=1) if item in expected), None)
        if rank is not None:
            result["mrr"] += 1.0 / rank
    return {"queries": count, **{key: round(value / count, 6) for key, value in result.items()}}


def _read_questions(path: Path) -> list[dict]:
    questions = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValidationError("invalid_eval_jsonl", detail={"line": line_number, "error": str(exc)}) from exc
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                or not isinstance(item.get("query"), str) or not isinstance(item.get("expected"), list)
                or not item["expected"] or not isinstance(item.get("tags", []), list)):
            raise ValidationError("invalid_eval_question", detail={"line": line_number})
        expected = []
        for entry in item["expected"]:
            if (not isinstance(entry, dict) or not isinstance(entry.get("repo_id"), str)
                    or not isinstance(entry.get("ref"), str)):
                raise ValidationError("invalid_eval_expected", detail={"line": line_number})
            expected.append({"repo_id": entry["repo_id"], "ref": entry["ref"]})
        tags = item.get("tags", [])
        if any(not isinstance(tag, str) for tag in tags):
            raise ValidationError("invalid_eval_tags", detail={"line": line_number})
        questions.append({"id": item["id"], "query": item["query"], "expected": expected,
                          "tags": tags, "line": line_number})
    if not questions:
        raise ValidationError("empty_eval_question_set", detail={"path": str(path)})
    return questions


def evaluate_retrieval(conn: sqlite3.Connection, *, file: str | Path, ks: list[int] | None = None,
                       out: str | Path | None = None, repo_filter: str | None = None) -> dict:
    selected_ks = [1, 5, 10] if ks is None else ks
    if not selected_ks or any(isinstance(k, bool) or not isinstance(k, int) or k < 1 for k in selected_ks):
        raise ValidationError("invalid_eval_k", detail={"k": selected_ks})
    selected_ks = sorted(set(selected_ks))
    if repo_filter is not None:
        if not isinstance(repo_filter, str) or not repo_filter.strip():
            raise ValidationError("invalid_eval_repo_filter")
        repo_filter = repo_filter.strip()
        if conn.execute("SELECT 1 FROM repos WHERE repo_id=?", (repo_filter,)).fetchone() is None:
            raise ValidationError("eval_repo_not_found", detail={"repo_id": repo_filter})
    source = Path(file).expanduser().resolve()
    questions = _read_questions(source)
    evaluated = []
    for question in questions:
        selected_expected = [item for item in question["expected"]
                             if repo_filter is None or item["repo_id"] == repo_filter]
        if not selected_expected:
            continue
        found = search(conn, question["query"], repo_id=repo_filter, limit=max(selected_ks))
        expected = [(item["repo_id"], item["ref"]) for item in selected_expected]
        evaluated.append({"id": question["id"], "query": question["query"], "expected": expected,
                          "tags": question["tags"], "results": found["results"]})
    if not evaluated:
        raise ValidationError("empty_eval_question_set_after_repo_filter", detail={"repo_id": repo_filter})
    all_metrics = _metrics(evaluated, selected_ks)
    tag_names = sorted({tag for row in evaluated for tag in row["tags"]})
    by_tag = {tag: _metrics([row for row in evaluated if tag in row["tags"]], selected_ks)
              for tag in tag_names}
    report = {"questions": len(evaluated), "k": selected_ks, "repo_filter": repo_filter, "metrics": all_metrics,
              "by_tag": by_tag, "results": evaluated}
    if out is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output_dir = home() / "evals" / "retrieval" / stamp
    else:
        output_dir = Path(out).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "report.json"
    markdown_path = output_dir / "report.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    lines = ["# Retrieval evaluation", "", f"Questions: {len(evaluated)}", "",
             "| Metric | Score |", "| --- | ---: |"]
    for key, value in all_metrics.items():
        if key != "queries":
            lines.append(f"| {key} | {value:.6f} |")
    lines.extend(["", "## By tag", "", "| Tag | Queries | Recall / MRR |", "| --- | ---: | --- |"])
    for tag, metrics in by_tag.items():
        scores = ", ".join(f"{key}={value:.6f}" for key, value in metrics.items() if key != "queries")
        lines.append(f"| {tag} | {metrics['queries']} | {scores} |")
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"report": report, "json_path": str(json_path), "markdown_path": str(markdown_path)}


def check_eval_file(conn: sqlite3.Connection, *, file: str | Path) -> dict:
    source = Path(file).expanduser().resolve()
    questions = _read_questions(source)
    missing = []
    expected_count = 0
    for question in questions:
        for item in question["expected"]:
            expected_count += 1
            exists = conn.execute(
                "SELECT 1 FROM search_docs WHERE repo_id=? AND lower(ref)=lower(?) "
                "AND superseded_by IS NULL LIMIT 1",
                (item["repo_id"], item["ref"]),
            ).fetchone()
            if exists is None:
                missing.append({"line": question["line"], "id": question["id"],
                                "repo_id": item["repo_id"], "ref": item["ref"]})
    return {"file": str(source), "questions": len(questions), "expected": expected_count,
            "missing": missing, "ok": not missing}
