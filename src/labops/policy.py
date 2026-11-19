import ast
import re
from pathlib import Path

from . import gitio
from .errors import ValidationError


def glob_regex(pattern: str) -> re.Pattern[str]:
    pieces = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            pieces.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            pieces.append(".*")
            i += 2
        elif pattern[i] == "*":
            pieces.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            pieces.append("[^/]")
            i += 1
        else:
            pieces.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(pieces) + "$")


def glob_match(pattern: str, path: str) -> bool:
    return bool(glob_regex(pattern).fullmatch(path))


def validate_policy(policy: dict) -> dict:
    if not isinstance(policy, dict) or policy.get("schema") != 1 or not isinstance(policy.get("frozen"), list):
        raise ValidationError("invalid_policy_schema")
    for i, item in enumerate(policy["frozen"]):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"]:
            raise ValidationError("invalid_policy_entry", detail={"index": i, "field": "path"})
        symbols = item.get("symbols")
        if symbols is not None and (not isinstance(symbols, list) or any(not isinstance(s, str) for s in symbols)):
            raise ValidationError("invalid_policy_entry", detail={"index": i, "field": "symbols"})
        if not isinstance(item.get("ref"), str) or not isinstance(item.get("note"), str):
            raise ValidationError("invalid_policy_entry", detail={"index": i, "field": "ref/note"})
    return policy


def out_of_scope(repo: Path, allowed: list[str], a: str, b: str) -> list[str]:
    if not allowed:
        return []
    patterns = [glob_regex(pattern) for pattern in allowed]
    return sorted(path for _, path in gitio.diff_name_status(repo, a, b)
                  if not any(pattern.fullmatch(path) for pattern in patterns))


def _declaration_intervals(source: str):
    tree = ast.parse(source)
    intervals: list[tuple[int, int, str]] = []
    decorators: list[tuple[int, int, str]] = []

    def walk(body, prefix: str = "", *, class_scope: bool = True):
        for node in body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if not class_scope:
                continue
            qualname = f"{prefix}.{node.name}" if prefix else node.name
            intervals.append((node.lineno, node.end_lineno or node.lineno, qualname))
            for decorator in getattr(node, "decorator_list", ()):
                decorators.append((decorator.lineno, decorator.end_lineno or decorator.lineno,
                                   f"@decorator:{qualname}"))
            if isinstance(node, ast.ClassDef):
                walk(node.body, qualname, class_scope=True)

    walk(tree.body)
    return intervals, decorators


def _line_symbols(line: int, intervals: list[tuple[int, int, str]]) -> set[str]:
    return {name for start, end, name in intervals if start <= line <= end}


def _line_decorators(line: int, decorators: list[tuple[int, int, str]]) -> set[str]:
    return {name for start, end, name in decorators if start <= line <= end}


def _ancestors(qualname: str) -> set[str]:
    parts = qualname.split(".")
    return {".".join(parts[:i]) for i in range(1, len(parts) + 1)}


def frozen_hits(repo: Path, policy: dict, a: str, b: str) -> list[dict]:
    validate_policy(policy)
    hits = []
    for status, path in gitio.diff_name_status(repo, a, b):
        entries = [item for item in policy["frozen"] if glob_match(item["path"], path)]
        for entry in entries:
            symbols = entry.get("symbols")
            common = {"path": path, "ref": entry.get("ref"), "note": entry.get("note")}
            if symbols is None:
                hits.append({**common, "reason": "frozen_path"})
                continue
            if status.startswith("D"):
                hits.append({**common, "reason": "file_deleted"})
                continue
            if not path.endswith(".py"):
                hits.append({**common, "reason": "non_python_symbol_policy"})
                continue
            old_bytes = gitio.show_file(repo, a, path)
            new_bytes = gitio.show_file(repo, b, path)
            try:
                old_source = old_bytes.decode("utf-8") if old_bytes is not None else ""
                new_source = new_bytes.decode("utf-8") if new_bytes is not None else ""
                old_intervals, old_decorators = _declaration_intervals(old_source) if old_bytes is not None else ([], [])
                new_intervals, new_decorators = _declaration_intervals(new_source) if new_bytes is not None else ([], [])
            except (SyntaxError, UnicodeDecodeError):
                hits.append({**common, "reason": "ast_parse_failed"})
                continue
            reasons: dict[str, set[str]] = {}
            old_text = old_source.splitlines()
            new_text = new_source.splitlines()
            for os_, ol, ns, nl in gitio.diff_hunks(repo, a, b, path):
                old_lines = range(os_, os_ + ol) if ol > 0 else (os_,)
                new_lines = range(ns, ns + nl) if nl > 0 else ()
                for line in old_lines:
                    dec = _line_decorators(line, old_decorators)
                    mapped = _line_symbols(line, old_intervals)
                    _classify_line(reasons, dec, mapped, symbols, trivial=_is_trivial(old_text, line))
                for line in new_lines:
                    dec = _line_decorators(line, new_decorators)
                    mapped = _line_symbols(line, new_intervals)
                    _classify_line(reasons, dec, mapped, symbols, trivial=_is_trivial(new_text, line))
            if reasons:
                reason = "decorator" if "decorator" in reasons else "module_level" if "module_level" in reasons else "frozen_symbol"
                hits.append({**common, "reason": reason, "symbols": sorted(reasons.get("frozen_symbol", set()) | reasons.get("decorator", set()))})
    return hits


def _is_trivial(lines: list[str], line: int) -> bool:
    """Blank or comment-only line (or a hunk anchor outside the file, e.g. line 0)."""
    if line < 1 or line > len(lines):
        return True
    text = lines[line - 1].strip()
    return text == "" or text.startswith("#")


def _classify_line(reasons: dict[str, set[str]], decorators: set[str], mapped: set[str], frozen: list[str],
                   *, trivial: bool = False) -> None:
    if decorators:
        reasons.setdefault("decorator", set()).update(decorators)
        return
    if not mapped:
        # Opus review fix: blank and comment-only module-level lines are not "top-level code"; counting
        # them made adding any new function to a symbol-frozen file a human block. Constants, imports,
        # statements and decorators still degrade to a block as v3.1 §3.3 requires.
        if trivial:
            return
        reasons.setdefault("module_level", set()).add("<module>")
        return
    frozen_set = set(frozen)
    matched = {name for name in mapped if _ancestors(name) & frozen_set}
    if matched:
        reasons.setdefault("frozen_symbol", set()).update(matched)


def changed_symbols(repo: Path, a: str, b: str) -> list[dict]:
    changed = []
    for _, path in gitio.diff_name_status(repo, a, b):
        if not path.endswith(".py"):
            continue
        old, new = gitio.show_file(repo, a, path), gitio.show_file(repo, b, path)
        try:
            intervals_old, _ = _declaration_intervals(old.decode("utf-8")) if old is not None else ([], [])
            intervals_new, _ = _declaration_intervals(new.decode("utf-8")) if new is not None else ([], [])
        except (SyntaxError, UnicodeDecodeError):
            changed.append({"path": path, "qualname": "<unparsed>"})
            continue
        names = {name for _, _, name in intervals_old + intervals_new}
        hunks = gitio.diff_hunks(repo, a, b, path)
        touched = set()
        for os_, ol, ns, nl in hunks:
            for line in (range(os_, os_ + ol) if ol else (os_,)):
                touched.update(_line_symbols(line, intervals_old))
            for line in range(ns, ns + nl):
                touched.update(_line_symbols(line, intervals_new))
        changed.extend({"path": path, "qualname": name} for name in sorted(touched & names))
    return sorted(changed, key=lambda item: (item["path"], item["qualname"]))
