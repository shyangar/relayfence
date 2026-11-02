"""Classify why a local agent run failed, from the artifacts it already wrote.

A non-zero adapter exit only reports "code 1". The actionable cause (rate limiting,
an exhausted plan, a rejected credential) is recorded in the run's own event log or
stderr, so it is read back here and turned into a ledger error code and a short
sentence a person can act on.

Only fixed, pre-written sentences cross into the ledger. Raw vendor text is never
copied: it can contain prompt fragments, account identifiers or the provider's own
wording, none of which belong in a shared record.
"""

from __future__ import annotations

import json
import re

QUOTA_CODE = "quota_exhausted"
FAILED_CODE = "execution_failed"
AUTH_CODE = "provider_auth_failed"
AUTH_MESSAGE = "模型供应商认证失败（401 或凭证被拒绝）；请在智能体管理中重新连接该供应商，或交接给已连接的成员。重试原凭证不会自动恢复。"

# Bounded so a huge or malformed log cannot stall a worker between heartbeats.
READ_LIMIT = 524_288

QUOTA_MESSAGE = (
    "模型额度不足或触发限流（{detail}）；该次数会在一段时间后恢复，或改用其他账号/成员。"
)
QUOTA_HINT = "已识别为额度类失败，可安全交接给其他成员。"

# Provider-neutral fingerprints. Each was observed in real relayfence runs:
# OpenCode free tier returns provider.quota + FreeUsageLimitError, and the ZCode
# account plan returns providerCode 1005 with "exceed quota limit".
_QUOTA_PATTERNS = (
    "freeusagelimiterror",
    "exceed quota",
    "quota limit",
    "insufficient_quota",
    "rate limit exceeded",
    "usage limit",
    "resource_exhausted",
)

_EVENT_LABELS = {
    "provider.quota": "模型服务限流",
    "provider.rate_limit": "模型服务限流",
    "provider.auth": "模型凭证被拒绝",
    "FreeUsageLimitError": "免费额度已用尽",
}

_SENSITIVE = re.compile(
    r"(?i)\b(token|secret|password|api[_-]?key|authorization|bearer)\b\s*[:=]\s*\S+"
)


def _scrub(text):
    """Remove credential-shaped fragments and bound the result."""
    return " ".join(_SENSITIVE.sub(r"\1=[redacted]", text).split())[:200]


def _read(directory, name):
    path = directory / name
    try:
        if not path.is_file():
            return ""
        with path.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - READ_LIMIT))
            return stream.read(READ_LIMIT).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _event_error_type(value):
    """Return the error type of one JSONL event, or None."""
    try:
        event = json.loads(value)
    except (ValueError, TypeError):
        return None
    if not isinstance(event, dict) or event.get("type") != "error":
        return None
    error = event.get("error")
    if not isinstance(error, dict):
        return None
    return str(error.get("type") or "")


def _structured_error(line):
    try:
        event = json.loads(line)
    except ValueError:
        return {}
    if not isinstance(event, dict):
        return {}
    if event.get("type") == "error" and isinstance(event.get("error"), dict):
        return event["error"]
    # DSH headless reports terminal errors inside a turn-end status event.
    reason = event.get("reason")
    if (
        event.get("type") == "status"
        and event.get("phase") == "turn_end"
        and isinstance(reason, dict)
        and reason.get("kind") == "error"
        and isinstance(reason.get("error"), dict)
    ):
        return reason["error"]
    return {}


def _describe(kind, detail):
    # Never copy an unknown vendor type or detail into the shared ledger: it may
    # contain a path, prompt fragment or account identifier. Only fixed labels
    # cross the boundary; everything else becomes one generic sentence.
    label = _EVENT_LABELS.get(kind) or _EVENT_LABELS.get(detail) or "未识别的错误类型"
    return _scrub(str(label))


def diagnose(directory):
    """Return (error_code, message) for a failed run.

    Returns the generic failure when nothing provider-specific is provable: a
    wrong diagnosis would send the task to the wrong place, so silence wins.
    """
    events = _read(directory, "opencode-events.jsonl") + _read(directory, "vendor-events.log")
    observed = None
    for line in events.splitlines():
        value = line.strip()
        if not value or '"error"' not in value:
            continue
        error = _structured_error(value)
        kind = str(error.get("type") or error.get("code") or "")
        if str(error.get("status")) == "401" or kind.casefold() in {
            "auth",
            "provider.auth",
            "authentication_error",
            "invalid_api_key",
            "unauthenticated",
        }:
            return AUTH_CODE, AUTH_MESSAGE
        if str(error.get("status")) == "429":
            return QUOTA_CODE, QUOTA_MESSAGE.format(detail=QUOTA_HINT)
        if not kind:
            continue
        observed = observed or kind
        if "quota" in kind.lower() or "rate" in kind.lower():
            return QUOTA_CODE, QUOTA_MESSAGE.format(detail=QUOTA_HINT)

    # stderr carries the ZCode business error, which is not a structured event.
    stderr = _read(directory, "stderr.log").casefold()
    if any(
        pattern in stderr
        for pattern in (
            "authentication fails",
            "invalid api key",
            "invalid_api_key",
            "authentication_error",
        )
    ):
        return AUTH_CODE, AUTH_MESSAGE
    if (
        'providercode": 1005' in stderr
        or 'providercode":1005' in stderr
        or any(pattern in stderr for pattern in _QUOTA_PATTERNS)
    ):
        return QUOTA_CODE, QUOTA_MESSAGE.format(detail=QUOTA_HINT)

    if observed:
        return FAILED_CODE, f"执行失败（{_describe(observed, '')}）；未自动重试，请检查后手动重试。"
    return FAILED_CODE, "执行失败；未自动重试，请检查运行日志后手动重试。"
