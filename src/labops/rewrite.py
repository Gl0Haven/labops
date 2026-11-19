"""Conservative DeepSeek rewrite with template-preserving validation."""

from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request


_TASK_RE = re.compile(r"T\d{8}-[\w-]+")
_ROUND_RE = re.compile(r"\br\d+\b")
_INTEGRATION_RE = re.compile(r"\big_\w+\b")
_SHA_RE = re.compile(r"(?i)(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])|(?<![0-9a-f])[0-9a-f]{7,12}(?![0-9a-f])")
_NUMBER_WITH_UNIT_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:ms|s|秒|分钟|小时|天|条|个|次|%|kib|mib|gib)(?=$|[^a-z])", re.IGNORECASE
)
_NEXT_STEP_RE = re.compile(r"下一步：([^\r\n]+)")
_TASK_GOAL_RE = re.compile(r"任务：([^\r\n]+)")
_RUN_DIR_RE = re.compile(r"训练\s+(.+?)\s+·")
_DEFAULT_BASE_URL = "https://api.deepseek.com"
_DEFAULT_MODEL = "deepseek-flash"
_MAX_CHARS = 800


def _preserved_items(text: str) -> list[str]:
    items: list[str] = []
    for pattern in (_TASK_RE, _ROUND_RE, _INTEGRATION_RE, _SHA_RE, _NUMBER_WITH_UNIT_RE,
                    _NEXT_STEP_RE, _TASK_GOAL_RE, _RUN_DIR_RE):
        for match in pattern.finditer(text):
            value = match.group(1) if pattern in (_NEXT_STEP_RE, _TASK_GOAL_RE, _RUN_DIR_RE) else match.group(0)
            if value and value not in items:
                items.append(value)
    return items


def _valid(candidate: str, template: str) -> bool:
    if not isinstance(candidate, str) or not candidate.strip():
        return False
    limit = min(_MAX_CHARS, int(len(template) * 1.2))
    if len(candidate) > limit:
        return False
    return all(item in candidate for item in _preserved_items(template))


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


def _model_name() -> str:
    model = os.environ.get("LABOPS_DS_MODEL", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL
    if model.casefold().startswith("deepseek/"):
        model = model[len("deepseek/"):]
    return model


def _request_payload(text: str, system: str | None = None) -> dict:
    default_system = (
        "你负责改写给研究者本人看的微信推送。使用中文和紧凑分段，每条事件单独成段；"
        "不增删事实；模板中的任务 ID、round、integration ID、SHA、下一步命令、带单位数字、"
        "run 目录名必须逐字保留；总长不超过原文的 1.2 倍且不超过 800 字；只输出正文。"
    )
    return {
        "model": _model_name(),
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": default_system if system is None else system},
            {"role": "user", "content": text},
        ],
    }


def _request_completion(text: str, system: str | None = None) -> str:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("missing_api_key")
    base_url = os.environ.get("LABOPS_DS_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")
    payload = json.dumps(_request_payload(text, system=system), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/chat/completions", data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    last_ssl_error = None
    contexts = _ssl_contexts()
    for index, context in enumerate(contexts):
        try:
            with urllib.request.urlopen(request, timeout=20, context=context) as response:
                decoded = json.loads(response.read().decode("utf-8"))
            return decoded["choices"][0]["message"]["content"]
        except ssl.SSLError as exc:
            last_ssl_error = exc
            if index == len(contexts) - 1:
                raise
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLError):
                last_ssl_error = exc.reason
                if index < len(contexts) - 1:
                    continue
            raise
    if last_ssl_error is not None:
        raise last_ssl_error
    raise RuntimeError("deepseek_request_failed")


def rewrite(text: str, *, kind: str, confidential: bool,
            enabled: bool | None = None, api_key_available: bool | None = None) -> tuple[str, str]:
    """Return rewritten text and outcome; caller supplies the active DB toggle.

    ``enabled`` is optional to keep this helper safe for direct use: absent means
    disabled. The notification path reads the toggle from its active connection
    and passes it explicitly, avoiding a second connection to the user's DB.
    """
    template = text if isinstance(text, str) else str(text)
    if not enabled:
        return template, "template_disabled"
    if confidential:
        return template, "template_confidential"
    has_key = bool(os.environ.get("DEEPSEEK_API_KEY")) if api_key_available is None else api_key_available
    if not has_key:
        return template, "template_disabled"
    try:
        candidate = _request_completion(template)
    except Exception:
        return template, "template_error"
    if not _valid(candidate, template):
        return template, "template_check_failed"
    return candidate, "llm"


_SUMMARY_SYSTEM = (
    "你把两个 AI 编程助手之间的一条消息概括给研究者本人看。中文，不超过 {n} 字；"
    "原文出现的任务 ID、request id、Q 编号和文件路径必须逐字保留，原文没有的不要提及；"
    "需要用户裁定时，最后一行写“需你裁定：……”，否则不写这一行；只输出正文。"
)
_Q_RE = re.compile(r"\bQ\d+\b")
_PATH_RE = re.compile(r"[\w./\\-]+\.(?:py|md|json|ps1|toml)")


def summarize(text: str, *, enabled: bool, confidential: bool, max_chars: int = 400) -> tuple[str, str]:
    header, _, body = text.partition("\n")
    truncated = body if len(body) <= max_chars else (body[:max_chars - 1] + "…" if max_chars > 0 else "")
    fallback = header + "\n" + truncated
    if not enabled:
        return fallback, "template_disabled"
    if confidential:
        return fallback, "template_confidential"
    if not os.environ.get("DEEPSEEK_API_KEY"):
        return fallback, "template_disabled"
    try:
        candidate = _request_completion(body, system=_SUMMARY_SYSTEM.format(n=max_chars))
    except Exception:
        return fallback, "template_error"

    request = re.match(r"请求：([A-Za-z0-9_-]+)", body)
    required = _TASK_RE.findall(body) + _Q_RE.findall(body) + _PATH_RE.findall(body)
    if request:
        required.append(request[1])
    if (not isinstance(candidate, str) or not candidate.strip() or len(candidate) > max_chars + 50
            or any(item not in candidate for item in required)):
        return fallback, "template_check_failed"
    return header + "\n" + candidate.strip(), "llm"
