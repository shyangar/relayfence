"""The dispatcher turns vendor event streams into short activity entries.

Parsing lives here rather than on the server because only the dispatcher can see
the run. These tests pin the shapes observed in real runs and the safety limits.
"""

from __future__ import annotations

import json

from relayfence.run_activity import (
    MAX_TEXT,
    MAX_TITLE,
    entries_from_events,
    new_entries,
    read_activity,
)


def _write(directory, name, events):
    path = directory / name
    path.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8"
    )
    return path


def test_opencode_text_and_tool_become_entries(tmp_path):
    _write(
        tmp_path,
        "opencode-events.jsonl",
        [
            {"type": "text", "part": {"type": "text", "text": "我先看一下文件"}},
            {
                "type": "tool_use",
                "part": {
                    "type": "tool",
                    "tool": "shell",
                    "state": {
                        "status": "completed",
                        "input": {"command": "Get-ChildItem"},
                        "output": "app.js",
                    },
                },
            },
        ],
    )

    entries = read_activity(tmp_path)

    assert [e["kind"] for e in entries] == ["output", "command", "output"]
    assert entries[0]["text"] == "我先看一下文件"
    assert entries[1]["title"] == "shell · Get-ChildItem"
    assert entries[2]["text"] == "app.js"


def test_a_read_tool_reports_its_path(tmp_path):
    """A bare ``read`` label says nothing; the path is what makes it readable."""
    _write(
        tmp_path,
        "opencode-events.jsonl",
        [
            {
                "type": "tool_use",
                "part": {
                    "type": "tool",
                    "tool": "read",
                    "state": {"status": "completed", "input": {"filePath": "D:/x/README.md"}},
                },
            }
        ],
    )

    entries = read_activity(tmp_path)

    assert entries[0]["title"] == "read · D:/x/README.md"


def test_a_failed_tool_is_marked_failed(tmp_path):
    _write(
        tmp_path,
        "opencode-events.jsonl",
        [
            {
                "type": "tool_use",
                "part": {
                    "type": "tool",
                    "tool": "shell",
                    "state": {"status": "error", "input": {"command": "boom"}},
                },
            }
        ],
    )

    entries = read_activity(tmp_path)

    assert entries[0]["status"] == "failed"


def test_opencode_errors_are_surfaced(tmp_path):
    _write(
        tmp_path,
        "opencode-events.jsonl",
        [{"type": "error", "part": {"type": "error", "error": "FreeUsageLimitError"}}],
    )

    entries = read_activity(tmp_path)

    assert entries[0]["kind"] == "error"
    assert entries[0]["status"] == "failed"
    assert "FreeUsageLimitError" in entries[0]["text"]


def test_codex_agent_message_becomes_output(tmp_path):
    _write(
        tmp_path,
        "codex-events.jsonl",
        [{"type": "item.completed", "item": {"type": "agent_message", "text": "完成了"}}],
    )

    entries = read_activity(tmp_path)

    assert entries == [{"kind": "output", "title": "", "text": "完成了", "status": "completed"}]


def test_codex_command_exit_code_sets_status(tmp_path):
    _write(
        tmp_path,
        "codex-events.jsonl",
        [
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "npm test", "exit_code": 1},
            }
        ],
    )

    entries = read_activity(tmp_path)

    assert entries[0]["kind"] == "command"
    assert entries[0]["status"] == "failed"


def test_vendor_stream_is_read(tmp_path):
    _write(
        tmp_path,
        "vendor-events.log",
        [
            {"type": "assistant", "text": "正在检查"},
            {"type": "tool", "name": "shell", "command": "ls"},
        ],
    )

    entries = read_activity(tmp_path)

    assert [e["kind"] for e in entries] == ["output", "command"]
    assert entries[1]["title"] == "shell · ls"


def test_a_missing_directory_yields_nothing(tmp_path):
    assert read_activity(tmp_path) == []


def test_a_partial_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "opencode-events.jsonl"
    path.write_text(
        json.dumps({"type": "text", "part": {"type": "text", "text": "好的"}})
        + "\n"
        + '{"type": "text", "part": {"ty',
        encoding="utf-8",
    )

    entries = read_activity(tmp_path)

    assert [e["text"] for e in entries] == ["好的"]


def test_long_text_is_clipped(tmp_path):
    _write(
        tmp_path,
        "opencode-events.jsonl",
        [{"type": "text", "part": {"type": "text", "text": "x" * 9000}}],
    )

    entries = read_activity(tmp_path)

    assert len(entries[0]["text"]) <= MAX_TEXT
    assert entries[0]["text"].endswith("…")


def test_long_command_title_is_clipped(tmp_path):
    _write(
        tmp_path,
        "opencode-events.jsonl",
        [
            {
                "type": "tool_use",
                "part": {
                    "type": "tool",
                    "tool": "shell",
                    "state": {"input": {"command": "y" * 900}, "status": "completed"},
                },
            }
        ],
    )

    entries = read_activity(tmp_path)

    assert len(entries[0]["title"]) <= MAX_TITLE + 10


def test_control_characters_are_stripped(tmp_path):
    _write(
        tmp_path,
        "opencode-events.jsonl",
        [{"type": "text", "part": {"type": "text", "text": "a\x00b\x1bc"}}],
    )

    entries = read_activity(tmp_path)

    assert entries[0]["text"] == "abc"


def test_unknown_dialect_is_ignored():
    assert entries_from_events("mystery.log", [{"type": "text"}]) == []


def test_new_entries_only_returns_the_tail():
    reported, seen = new_entries([{"a": 1}], 0)
    assert reported == [{"a": 1}] and seen == 1

    reported, seen = new_entries([{"a": 1}, {"b": 2}], seen)
    assert reported == [{"b": 2}] and seen == 2

    reported, seen = new_entries([{"a": 1}, {"b": 2}], seen)
    assert reported == [] and seen == 2


def test_new_entries_tolerates_a_shrinking_stream():
    """A rotated or truncated file must not resend everything as brand new."""
    reported, seen = new_entries([], 5)
    assert reported == [] and seen == 0


def test_vendor_activity_keeps_failures_and_ignores_nontext_payloads(tmp_path):
    _write(
        tmp_path,
        "vendor-events.log",
        [
            {"type": "assistant", "text": "Working"},
            {"type": "tool_call", "name": "shell", "command": "pytest"},
            {"type": "error", "message": "Provider failed"},
            {"type": "message", "content": {"unexpected": "object"}},
        ],
    )
    entries = read_activity(tmp_path)
    assert [e["kind"] for e in entries] == ["output", "command", "error"]
    assert entries[-1]["status"] == "failed"


def test_codex_activity_bounds_reasoning_and_reports_command_failure(tmp_path):
    _write(
        tmp_path,
        "codex-events.jsonl",
        [
            {"type": "item.updated", "item": None},
            {"type": "item.updated", "item": {"type": "reasoning", "text": "Checking files"}},
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "pytest", "exit_code": 1},
            },
            {"type": "error", "message": "Execution failed"},
        ],
    )
    entries = read_activity(tmp_path)
    assert [e["kind"] for e in entries] == ["note", "command", "error"]
    assert entries[1]["status"] == "failed"


def test_event_reader_survives_partial_and_non_object_json(tmp_path):
    path = tmp_path / "codex-events.jsonl"
    path.write_text(
        "\n[1,2]\n{partial\n"
        + json.dumps(
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Valid message"}}
        ),
        encoding="utf-8",
    )
    assert read_activity(tmp_path)[0]["text"] == "Valid message"


def test_opencode_reasoning_and_tool_failure_are_readable(tmp_path):
    _write(
        tmp_path,
        "opencode-events.jsonl",
        [
            {"part": None},
            {"part": {"type": "reasoning", "text": "Check constraints"}},
            {
                "part": {
                    "type": "tool",
                    "tool": "shell",
                    "state": {"input": "pytest", "status": "error"},
                }
            },
            {"part": {"type": "error", "error": "Permission denied"}},
        ],
    )
    entries = read_activity(tmp_path)
    assert [e["kind"] for e in entries] == ["note", "command", "error"]
    assert entries[1]["status"] == "failed" and "Permission denied" in entries[-1]["text"]


def test_incremental_reader_reaches_long_stream_tail_and_preserves_partial_lines(tmp_path):
    from relayfence.run_activity import ActivityReader
    from relayfence.run_diagnosis import READ_LIMIT

    path = tmp_path / "codex-events.jsonl"
    event = json.dumps(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Tail message"}}
    ).encode()
    path.write_bytes(b"\n" * (READ_LIMIT + 20) + event[:30])
    reader = ActivityReader(tmp_path)
    assert reader.read() == []
    assert reader.read() == []
    with path.open("ab") as stream:
        stream.write(event[30:] + b"\n")
    assert reader.read()[0]["text"] == "Tail message"
    assert reader.read() == []


def test_incremental_reader_recovers_from_missing_truncated_and_removed_files(tmp_path):
    from relayfence.run_activity import ActivityReader

    reader = ActivityReader(tmp_path)
    assert reader.read() == []
    path = tmp_path / "codex-events.jsonl"
    event = {"type": "item.completed", "item": {"type": "agent_message", "text": "first"}}
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    assert reader.read()[0]["text"] == "first"
    path.write_text("[]\n{broken\n", encoding="utf-8")
    assert reader.read() == []
    assert reader.offset == path.stat().st_size
    path.unlink()
    assert reader.read() == []
    event["item"]["text"] = "restored"
    path.write_text(json.dumps(event), encoding="utf-8")
    # A recreated stream is treated as truncated after the file shrinks.
    path.write_text("", encoding="utf-8")
    assert reader.read() == []
    path.write_text(json.dumps(event), encoding="utf-8")
    assert reader.read()[0]["text"] == "restored"


def test_incremental_reader_bounds_a_malformed_unterminated_record(tmp_path):
    from relayfence.run_activity import ActivityReader
    from relayfence.run_diagnosis import READ_LIMIT

    path = tmp_path / "codex-events.jsonl"
    path.write_bytes(b"x" * (READ_LIMIT * 3))
    reader = ActivityReader(tmp_path)
    for _ in range(3):
        assert reader.read() == []
    assert reader.fragment == b""


def test_dsh_json_text_events_remain_visible_after_session_resume(tmp_path):
    _write(
        tmp_path,
        "vendor-events.log",
        [
            {"type": "session", "sessionId": "fixed-project-session"},
            {"type": "status", "phase": "turn_start"},
            {"type": "text", "text": "Continue implementation in the same project session"},
            {"type": "final", "text": "final already represented by text events"},
        ],
    )
    rows = read_activity(tmp_path)
    assert len(rows) == 1 and rows[0]["kind"] == "output"
    assert "same project session" in rows[0]["text"]
