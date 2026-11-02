"""Failure classification from a run's own artifacts.

The two real failures seen in this workspace are reproduced here: OpenCode's free
tier writes a structured `provider.quota` event, and a ZCode account plan writes a
`ProviderBusinessError ... providerCode: 1005` block to stderr.
"""

from __future__ import annotations

import json

from relayfence.run_diagnosis import AUTH_CODE, FAILED_CODE, QUOTA_CODE, diagnose
from relayfence.worker import _failure_reason


def _event(value):
    return json.dumps({"type": "error", "error": value}) + "\n"


def test_opencode_free_usage_limit_is_classified_as_quota(tmp_path):
    (tmp_path / "opencode-events.jsonl").write_text(
        _event(
            {
                "type": "provider.quota",
                "message": "Rate limit exceeded. Please try again later.",
                "status": 429,
                "response": {
                    "body": json.dumps(
                        {
                            "type": "error",
                            "error": {
                                "type": "FreeUsageLimitError",
                                "message": "Rate limit exceeded. Please try again later.",
                            },
                        }
                    )
                },
            }
        ),
        encoding="utf-8",
    )

    code, message = diagnose(tmp_path)

    assert code == QUOTA_CODE
    assert "交接" in message


def test_zcode_business_quota_error_in_stderr_is_classified_as_quota(tmp_path):
    (tmp_path / "stderr.log").write_text(
        "ProviderBusinessError: exceed quota limit\n"
        "    at detectProviderBusinessError\n"
        "  code: 'PROVIDER_BUSINESS_ERROR',\n"
        "  providerCode: 1005,\n"
        "  providerId: 'account:zai-start-plan',\n"
        "  providerMessage: 'exceed quota limit',\n",
        encoding="utf-8",
    )

    code, _message = diagnose(tmp_path)

    assert code == QUOTA_CODE


def test_vendor_events_log_is_also_inspected(tmp_path):
    (tmp_path / "vendor-events.log").write_text(
        _event({"type": "provider.quota", "message": "quota"}), encoding="utf-8"
    )

    assert diagnose(tmp_path)[0] == QUOTA_CODE


def test_authentication_error_is_distinct_from_quota(tmp_path):
    (tmp_path / "opencode-events.jsonl").write_text(
        _event({"type": "provider.auth", "message": "bad token"}), encoding="utf-8"
    )
    (tmp_path / "stderr.log").write_text("Traceback (most recent call last):\n", encoding="utf-8")

    code, message = diagnose(tmp_path)

    assert code == AUTH_CODE
    assert "额度" not in message


def test_unknown_error_type_is_not_echoed_into_the_ledger(tmp_path):
    (tmp_path / "opencode-events.jsonl").write_text(
        _event({"type": "SecretProvider123", "message": "raw vendor text"}),
        encoding="utf-8",
    )

    code, message = diagnose(tmp_path)

    assert code == FAILED_CODE
    assert "SecretProvider123" not in message
    assert "raw vendor text" not in message
    assert "未识别的错误类型" in message


def test_missing_or_unreadable_artifacts_do_not_raise(tmp_path):
    assert diagnose(tmp_path) == (
        FAILED_CODE,
        "执行失败；未自动重试，请检查运行日志后手动重试。",
    )


def test_malformed_events_are_ignored(tmp_path):
    (tmp_path / "opencode-events.jsonl").write_text(
        "{not json}\n[]\n" + json.dumps({"type": "error"}) + "\n" + _event("plain-string"),
        encoding="utf-8",
    )

    assert diagnose(tmp_path)[0] == FAILED_CODE


def test_credentials_are_scrubbed_from_the_message(tmp_path):
    """A raw secret in provider text must never reach the shared ledger."""
    (tmp_path / "stderr.log").write_text(
        "ProviderBusinessError: exceed quota limit\napi_key=sk-live-supersecret\n",
        encoding="utf-8",
    )

    _code, message = diagnose(tmp_path)

    assert "sk-live-supersecret" not in message


def test_read_is_bounded(tmp_path):
    (tmp_path / "opencode-events.jsonl").write_text("x" * 2_000_000, encoding="utf-8")

    assert diagnose(tmp_path)[0] == FAILED_CODE


def test_unreadable_artifact_is_treated_as_absent(tmp_path):
    """A locked or permission-denied log must not break failure reporting."""
    import relayfence.run_diagnosis as module

    class Locked:
        def __truediv__(self, name):
            return self

        def is_file(self):
            return True

        def open(self, *args, **kwargs):
            raise OSError("locked")

    assert module._read(Locked(), "stderr.log") == ""
    # A missing directory behaves the same way, so reporting still completes.
    assert diagnose(tmp_path / "absent")[0] == FAILED_CODE


def test_non_string_event_payload_is_ignored():
    from relayfence.run_diagnosis import _event_error_type

    assert _event_error_type(None) is None


def test_quota_failure_marks_the_task_for_handoff(tmp_path):
    """Classifying as quota_exhausted is what makes safe handoff accept the task."""
    (tmp_path / "stderr.log").write_text("ProviderBusinessError: exceed quota limit\n")

    code, message = _failure_reason(RuntimeError("Command exited with code 1"), tmp_path)

    assert code == QUOTA_CODE
    # The opaque exit status is replaced by an actionable sentence.
    assert "exited with code" not in message


def test_specific_wording_is_kept_for_other_errors(tmp_path):
    code, message = _failure_reason(
        RuntimeError("Command timed out; inspect the private run directory"), tmp_path
    )

    assert code == FAILED_CODE
    assert "Command timed out" in message


def test_coordination_errors_keep_their_own_code_and_text(tmp_path):
    from relayfence.errors import CoordinationError

    code, message = _failure_reason(
        CoordinationError("heartbeat_timeout", "Lease heartbeat timed out; execution stopped"),
        tmp_path,
    )

    assert code == "heartbeat_timeout"
    assert "Lease heartbeat timed out" in message


def test_dsh_terminal_auth_error_is_classified_without_copying_vendor_secret(tmp_path):
    (tmp_path / "vendor-events.log").write_text(
        json.dumps(
            {
                "type": "status",
                "phase": "turn_end",
                "reason": {
                    "kind": "error",
                    "error": {
                        "code": "AUTH",
                        "status": 401,
                        "message": "Authentication Fails api_key=must-not-leak",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    code, message = diagnose(tmp_path)
    assert code == AUTH_CODE and "认证失败" in message
    assert "must-not-leak" not in message and "api_key" not in message


def test_dsh_authentication_fails_stderr_is_not_quota(tmp_path):
    (tmp_path / "stderr.log").write_text(
        "dsh: AUTH: Authentication Fails, Your api key: ****1234 is invalid"
    )
    assert diagnose(tmp_path)[0] == AUTH_CODE


def test_terminal_rate_limit_is_classified_from_tail_of_long_stream(tmp_path):
    (tmp_path / "vendor-events.log").write_text(
        "x" * 600000
        + "\n"
        + json.dumps(
            {
                "type": "status",
                "phase": "turn_end",
                "reason": {"kind": "error", "error": {"code": "RATE_LIMIT", "status": 429}},
            }
        )
    )
    assert diagnose(tmp_path)[0] == QUOTA_CODE


def test_business_error_without_quota_evidence_is_not_assumed_exhausted(tmp_path):
    (tmp_path / "stderr.log").write_text("ProviderBusinessError: invalid request schema")
    assert diagnose(tmp_path)[0] == FAILED_CODE
