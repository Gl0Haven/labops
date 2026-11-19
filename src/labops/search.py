import re
import sqlite3

from .errors import ValidationError


_DECISION_RE = re.compile(r"^D-\d{8}-\d+$", re.IGNORECASE)
_TASK_RE = re.compile(r"^T\d{8}-[a-z0-9-]+$", re.IGNORECASE)
_REVIEW_RE = re.compile(r"^rv_[a-z0-9_-]+$", re.IGNORECASE)
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_CJK_RANGES = (
    (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF),
    (0x20000, 0x2A6DF), (0x2A700, 0x2B73F), (0x2B740, 0x2B81F),
    (0x2B820, 0x2CEAF), (0x2CEB0, 0x2EBEF), (0x30000, 0x3134F),
)
_MAYBE_SUPERSEDED_RE = re.compile(r"取代|更正|supersed", re.IGNORECASE)
_EXACT_SCORE = 1_000_000_000.0


def _is_cjk(char: str) -> bool:
    point = ord(char)
    return any(start <= point <= end for start, end in _CJK_RANGES)


def bigram_text(value: str) -> str:
    if not isinstance(value, str):
        raise ValidationError("invalid_search_query")
    tokens = []
    cjk = []
    ascii_word = []

    def flush_cjk():
        if not cjk:
            return
        word = "".join(cjk)
        if len(word) == 1:
            tokens.append(word)
        else:
            tokens.extend(word[index:index + 2] for index in range(len(word) - 1))
        cjk.clear()

    def flush_ascii():
        if ascii_word:
            tokens.append("".join(ascii_word).lower())
            ascii_word.clear()

    for char in value:
        if _is_cjk(char):
            flush_ascii()
            cjk.append(char)
        elif char.isascii() and (char.isalnum() or char == "_"):
            flush_cjk()
            ascii_word.append(char)
        else:
            flush_cjk()
            flush_ascii()
    flush_cjk()
    flush_ascii()
    return " ".join(tokens)


def _query_bigram_tokens(value: str) -> list[str]:
    unique = []
    seen = set()
    for token in bigram_text(value).split():
        if token.isascii() and (len(token) == 1 or not any(char.isalnum() for char in token)):
            continue
        if token not in seen:
            seen.add(token)
            unique.append(token)
        if len(unique) == 64:
            break
    return unique


def _quoted_fts(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _exact_rows(conn: sqlite3.Connection, query: str) -> list[sqlite3.Row]:
    if _DECISION_RE.fullmatch(query) or _TASK_RE.fullmatch(query):
        return conn.execute(
            "SELECT * FROM search_docs WHERE lower(ref)=lower(?) ORDER BY repo_id,kind,path,line,doc_id",
            (query,),
        ).fetchall()
    if _REVIEW_RE.fullmatch(query):
        return conn.execute(
            "SELECT * FROM search_docs WHERE lower(ref)=lower(?) OR lower(ref) LIKE lower(?) "
            "ORDER BY repo_id,kind,path,line,doc_id",
            (query, query + "#%"),
        ).fetchall()
    if _SHA_RE.fullmatch(query):
        pattern = query.lower() + "%"
        contains = "%" + query.lower() + "%"
        return conn.execute(
            "SELECT * FROM search_docs WHERE lower(ref) LIKE ? OR lower(body) LIKE ? "
            "ORDER BY repo_id,kind,path,line,doc_id",
            (pattern, contains),
        ).fetchall()
    return []


def _fts_doc_ids(conn: sqlite3.Connection, table: str, match: str) -> list[str]:
    if not match:
        return []
    rows = conn.execute(
        f"SELECT doc_id FROM {table} WHERE {table} MATCH ? ORDER BY bm25({table}) LIMIT 50", (match,)
    ).fetchall()
    return [row[0] for row in rows]


def _add_rrf(scores: dict[str, float], doc_ids: list[str], exact_ids: set[str]) -> None:
    for rank, doc_id in enumerate(doc_ids, start=1):
        if doc_id not in exact_ids:
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (60 + rank)


def _row_allowed(row, *, repo_id: str | None, kinds: set[str] | None, include_superseded: bool) -> bool:
    if repo_id is not None and row["repo_id"] != repo_id:
        return False
    if kinds is not None and row["kind"] not in kinds:
        return False
    if not include_superseded and row["superseded_by"] is not None:
        return False
    return True


def _snippet(text: str, query: str, limit: int = 80) -> str:
    if len(text) <= limit:
        return text
    lowered = text.casefold()
    position = lowered.find(query.casefold()) if query else -1
    start = max(0, position - (limit // 2)) if position >= 0 else 0
    end = min(len(text), start + limit)
    if end - start < limit:
        start = max(0, end - limit)
    result = text[start:end]
    if start:
        result = "…" + result[1:]
    if end < len(text):
        result = result[:-1] + "…"
    return result


def search(conn: sqlite3.Connection, query: str, *, repo_id: str | None = None,
           kinds: list[str] | set[str] | None = None, limit: int = 10,
           include_superseded: bool = False, actor: str | None = None) -> dict:
    if not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise ValidationError("invalid_search_query")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValidationError("invalid_search_limit", detail={"limit": limit})
    if not isinstance(include_superseded, bool):
        raise ValidationError("invalid_include_superseded")
    normalized = query.strip()
    selected_kinds = set(kinds) if kinds is not None else None
    if selected_kinds is not None and (not selected_kinds
                                       or any(not isinstance(kind, str) for kind in selected_kinds)
                                       or selected_kinds - {"decision", "review", "status", "task", "finding", "commit",
                                                            "plan", "review_section", "research_section"}):
        raise ValidationError("invalid_search_kinds", detail={"kinds": sorted(selected_kinds)})

    scores: dict[str, float] = {}
    exact_ids = set()
    for row in _exact_rows(conn, normalized):
        if _row_allowed(row, repo_id=repo_id, kinds=selected_kinds, include_superseded=include_superseded):
            scores[row["doc_id"]] = _EXACT_SCORE
            exact_ids.add(row["doc_id"])

    tri_phrase_ids = _fts_doc_ids(conn, "search_tri", _quoted_fts(normalized)) if len(normalized) >= 3 else []
    fragments = list(dict.fromkeys(
        fragment for fragment in re.split(r"[\s\W]+", normalized) if len(fragment) >= 3
    ))
    tri_fragment_match = " OR ".join(_quoted_fts(fragment) for fragment in fragments)
    tri_fragment_ids = _fts_doc_ids(conn, "search_tri", tri_fragment_match)
    bi_tokens = _query_bigram_tokens(normalized)
    bi_match = " OR ".join(_quoted_fts(token) for token in bi_tokens)
    bi_ids = _fts_doc_ids(conn, "search_bi", bi_match)
    _add_rrf(scores, tri_phrase_ids, exact_ids)
    _add_rrf(scores, tri_fragment_ids, exact_ids)
    _add_rrf(scores, bi_ids, exact_ids)

    if not scores:
        return {"query": normalized, "count": 0, "results": []}
    placeholders = ",".join("?" for _ in scores)
    rows = conn.execute(f"SELECT * FROM search_docs WHERE doc_id IN ({placeholders})", tuple(scores)).fetchall()
    candidates = [row for row in rows if _row_allowed(row, repo_id=repo_id, kinds=selected_kinds,
                                                      include_superseded=include_superseded)]
    candidates.sort(key=lambda row: (-scores[row["doc_id"]], row["repo_id"], row["kind"], row["ref"],
                                     row["path"] or "", row["line"] or 0, row["doc_id"]))
    results = []
    for row in candidates[:limit]:
        title, body = row["title"], row["body"]
        text = "\n".join(value for value in (title, body) if value)
        item = {"repo_id": row["repo_id"], "kind": row["kind"], "ref": row["ref"],
                "path": row["path"], "line": row["line"], "title": title,
                "snippet": _snippet(text, normalized), "score": scores[row["doc_id"]],
                "content_hash": row["content_hash"],
                "valid": row["superseded_by"] is None,
                "maybe_superseded": bool(row["kind"] == "decision" and
                                           _MAYBE_SUPERSEDED_RE.search(f"{title or ''}\n{body}")),
                "ts": row["ts"]}
        if actor == "secretary" and row["confidential"]:
            allowed = ("repo_id", "kind", "ref", "title") if row["kind"] in {
                "review_section", "research_section"} else ("repo_id", "kind", "ref", "ts")
            item = {key: item[key] for key in allowed}
        results.append(item)
    return {"query": normalized, "count": len(results), "results": results}
