"""Turn a vendor event stream into short, readable activity entries.

Each adapter writes its own dialect, so this module holds one small reader per
dialect and a bounded, provider-neutral output shape. It reads only the tail of a
file so a long run cannot make a status poll expensive.

Nothing here interprets or trusts the vendor: text is clipped, control characters
are stripped, and only a fixed set of fields is copied. The dispatcher is the only
writer, so the ledger never has to parse a vendor format.
"""

from __future__ import annotations

import json
import re

from .run_diagnosis import READ_LIMIT

# How much of one field to keep. Chat bubbles are short on purpose; a huge tool
# output is truncated rather than allowed to dominate the ledger.
MAX_TITLE = 160
MAX_TEXT = 2400

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Adapters write the file named after their own event format.
EVENT_FILES = ("opencode-events.jsonl", "codex-events.jsonl", "vendor-events.log")


def _clean(value, limit):
    if not isinstance(value, str):
        return ""
    text = _CONTROL.sub("", value).strip()
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _read_json_lines(path):
    try:
        if not path.is_file():
            return []
        with path.open("rb") as stream:
            data = stream.read(READ_LIMIT).decode("utf-8", errors="replace")
    except OSError:
        return []
    events = []
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            # A line can be mid-write; skipping it keeps the rest readable.
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _opencode(events):
    """OpenCode writes {type, part}; parts carry text, tools and steps."""
    entries = []
    for event in events:
        part = event.get("part")
        if not isinstance(part, dict):
            continue
        kind = part.get("type") or event.get("type")
        if kind == "text":
            text = _clean(part.get("text"), MAX_TEXT)
            if text:
                entries.append({"kind": "output", "title": "", "text": text, "status": "completed"})
        elif kind == "reasoning":
            text = _clean(part.get("text"), MAX_TEXT)
            if text:
                entries.append(
                    {"kind": "note", "title": "思考", "text": text, "status": "completed"}
                )
        elif kind == "tool":
            state = part.get("state")
            state = state if isinstance(state, dict) else {}
            tool = _clean(part.get("tool") or "tool", 40)
            payload = state.get("input")
            summary = ""
            if isinstance(payload, dict):
                # Tool inputs name their own target; a shell names its command.
                summary = _clean(
                    payload.get("command")
                    or payload.get("description")
                    or payload.get("filePath")
                    or payload.get("path")
                    or payload.get("pattern")
                    or "",
                    MAX_TITLE,
                )
            elif isinstance(payload, str):
                summary = _clean(payload, MAX_TITLE)
            suffix = ""
            if summary and summary != tool:
                suffix = " · " + summary
            entries.append(
                {
                    "kind": "command",
                    "title": tool + suffix,
                    "text": summary,
                    "status": "failed" if state.get("status") == "error" else "completed",
                }
            )
            output = _clean(state.get("output"), MAX_TEXT)
            if output:
                entries.append(
                    {"kind": "output", "title": "", "text": output, "status": "completed"}
                )
        elif kind == "error":
            error = part.get("error")
            text = _clean(
                error if isinstance(error, str) else json.dumps(error, ensure_ascii=False), MAX_TEXT
            )
            entries.append(
                {"kind": "error", "title": "错误", "text": text or "执行错误", "status": "failed"}
            )
    return entries


def _codex(events):
    """Codex emits JSONL items; message content is the assistant's own words."""
    entries = []
    for event in events:
        kind = event.get("type")
        if kind in {"item.completed", "item.updated"}:
            item = event.get("item")
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "agent_message":
                text = _clean(item.get("text"), MAX_TEXT)
                if text:
                    entries.append(
                        {"kind": "output", "title": "", "text": text, "status": "completed"}
                    )
            elif item_type == "reasoning":
                text = _clean(item.get("text"), MAX_TEXT)
                if text:
                    entries.append(
                        {"kind": "note", "title": "思考", "text": text, "status": "completed"}
                    )
            elif item_type == "command_execution":
                command = _clean(item.get("command"), MAX_TITLE)
                entries.append(
                    {
                        "kind": "command",
                        "title": command or "command",
                        "text": command,
                        "status": "failed"
                        if item.get("exit_code") not in (None, 0)
                        else "completed",
                    }
                )
        elif kind == "error":
            text = _clean(event.get("message"), MAX_TEXT)
            entries.append(
                {"kind": "error", "title": "错误", "text": text or "执行错误", "status": "failed"}
            )
    return entries


def _vendor(events):
    """Vendor adapters write assistant deltas and a structured result."""
    entries = []
    for event in events:
        kind = event.get("type")
        if kind in {"assistant", "message", "assistant_message", "text"}:
            text = _clean(event.get("text") or event.get("content"), MAX_TEXT)
            if text:
                entries.append({"kind": "output", "title": "", "text": text, "status": "completed"})
        elif kind in {"tool", "tool_call", "tool_use"}:
            name = _clean(event.get("name") or event.get("tool") or "tool", 40)
            command = _clean(event.get("command") or event.get("input"), MAX_TITLE)
            entries.append(
                {
                    "kind": "command",
                    "title": f"{name} · {command}" if command else name,
                    "text": command,
                    "status": "completed",
                }
            )
        elif kind == "error":
            text = _clean(event.get("message") or event.get("error"), MAX_TEXT)
            entries.append(
                {"kind": "error", "title": "错误", "text": text or "执行错误", "status": "failed"}
            )
    return entries


def entries_from_events(name, events):
    """Return readable entries for one event dialect, or [] when unknown."""
    parser = {
        "opencode-events.jsonl": _opencode,
        "codex-events.jsonl": _codex,
        "vendor-events.log": _vendor,
    }.get(name)
    if parser is None:
        return []
    result = []
    for event in events:
        rows = parser([event])
        detail = event.get("item") or event.get("part") or {}
        detail = detail if isinstance(detail, dict) else {}
        for field, keys in [
            ("source_agent", ("source_agent", "agent_id", "thread_id")),
            ("parent_agent", ("parent_agent", "parent_agent_id", "parent_thread_id")),
        ]:
            value = next(
                (
                    source.get(key)
                    for source in (event, detail)
                    for key in keys
                    if isinstance(source.get(key), str) and source[key].strip()
                ),
                None,
            )
            if value:
                for row in rows:
                    row[field] = _clean(value, 128)
        result.extend(rows)
    return result


def read_activity(directory):
    """Return activity entries found in ``directory`` using the first matching file."""
    for name in EVENT_FILES:
        path = directory / name
        if not path.is_file():
            continue
        entries = entries_from_events(name, _read_json_lines(path))
        if entries:
            return entries
    return []


def new_entries(entries, seen):
    """Drop entries already reported.

    Event streams are append-only, so remembering how many were sent is enough and
    avoids resending the whole history on every poll.
    """
    fresh = entries[seen:]
    return fresh, len(entries)


class ActivityReader:
    """Read new bytes, including long streams beyond the initial read budget."""

    def __init__(self, directory):
        self.directory = directory
        self.path = None
        self.offset = 0
        self.fragment = b""

    def read(self):
        from pathlib import Path

        if self.path is None:
            self.path = next(
                (
                    Path(self.directory) / name
                    for name in EVENT_FILES
                    if (Path(self.directory) / name).is_file()
                ),
                None,
            )
        if self.path is None:
            return []
        try:
            if self.path.stat().st_size < self.offset:
                self.offset = 0
                self.fragment = b""
            with self.path.open("rb") as stream:
                stream.seek(self.offset)
                data = stream.read(READ_LIMIT)
                self.offset += len(data)
        except OSError:
            return []
        pieces = (self.fragment + data).split(b"\n")
        self.fragment = pieces.pop()
        events = []
        for line in pieces:
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(event, dict):
                events.append(event)
        # A final valid JSON object need not end with a newline. Invalid tails
        # remain buffered while the vendor is still writing them.
        if self.fragment:
            try:
                event = json.loads(self.fragment)
            except (ValueError, UnicodeError):
                if len(self.fragment) > READ_LIMIT * 2:
                    self.fragment = b""
            else:
                self.fragment = b""
                if isinstance(event, dict):
                    events.append(event)
        return entries_from_events(self.path.name, events)
