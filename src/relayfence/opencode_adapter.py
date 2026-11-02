"""Built-in adapter and catalogue helpers for an installed, authenticated OpenCode CLI.

The exact model reference discovered from ``opencode models`` is passed through to
``opencode run`` without substitution. Provider credentials, project configuration
and permission settings remain under OpenCode's control: RelayFence neither reads
nor rewrites them. A requested model reference is not independent evidence of the
runtime's actual identity.

Process ownership, stdout limits and lease renewal belong to the RelayFence worker.
This module only runs one foreground ``opencode run`` for one claimed task.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from math import isfinite
from pathlib import Path

from .codex_adapter import AgentIncomplete, CodexResult, question_output, result_schema
from .project_sessions import current_session, stream_process
from .store import MAX_PAYLOAD_BYTES, encode

SOFTWARE = "opencode"
LABEL = "OpenCode"
EVENT_LIMIT = 8_388_608
CATALOGUE_LIMIT = 2_048

# OpenCode references are "provider/model". The model part may contain further
# slashes (for example openrouter/anthropic/...); variants (#) are not selectable
# through RelayFence's documented model identifier.
_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+\-]*/[A-Za-z0-9][A-Za-z0-9_.:/@+\-]*$")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

_PERMISSION_AGENTS = {
    "read-only": "plan",
    "workspace-write": "build",
    "danger-full-access": "build",
}


def find_executable():
    """Return a non-batch OpenCode executable, or None when none is installed.

    An explicit override wins. Windows npm batch shims are not accepted because a
    shell would re-parse the prompt/path arguments; set the override to the real
    ``opencode.exe`` (or the launcher installed under ``~/.opencode/bin``).
    """
    override = os.environ.get("RELAYFENCE_OPENCODE_EXECUTABLE")
    if override:
        return os.path.expanduser(override)
    names = ("opencode.exe", "opencode") if os.name == "nt" else ("opencode",)
    candidates = []
    found = shutil.which("opencode")
    if found:
        candidates.append(found)
        if Path(found).suffix.lower() in {".cmd", ".bat"}:
            # npm also installs the native binary beside its batch shim.
            candidates.append(str(Path(found).with_suffix(".exe")))
    home = Path.home()
    candidates.extend(home / ".opencode" / "bin" / name for name in names)
    candidates.extend(home / ".local" / "bin" / name for name in names)
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.extend(Path(local) / "Programs" / "opencode" / name for name in names)
            candidates.extend(Path(local) / "opencode" / "bin" / name for name in names)
            # OpenCode Desktop bundles a headless CLI beside its Electron app.
            resources = Path(local) / "Programs" / "@opencodedesktop" / "resources"
            candidates.extend(resources / name for name in ("opencode-cli.exe", "opencode.exe"))
        roaming = os.environ.get("APPDATA")
        if roaming:
            # Older/alternate desktop builds keep a versioned CLI copy here.
            root = Path(roaming) / "ai.opencode.desktop" / "cli"
            try:
                versioned = [
                    child / name
                    for child in root.iterdir()
                    for name in ("opencode-cli.exe", "opencode.exe")
                    if (child / name).is_file()
                ]
            except OSError:
                versioned = []
            candidates.extend(
                sorted(versioned, key=lambda path: path.stat().st_mtime_ns, reverse=True)
            )
    else:
        candidates.extend(Path("/usr/local/bin") / name for name in names)
        candidates.extend(Path("/opt/homebrew/bin") / name for name in names)
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file() and path.suffix.lower() not in {".cmd", ".bat"}:
            return str(path)
    return None


def parse_models(text):
    """Parse ``opencode models`` output into unique, bounded provider/model references.

    Only complete references are accepted. Diagnostics, blank lines and anything
    that is not a ``provider/model`` pair are ignored rather than guessed at.
    """
    references, seen = [], set()
    for raw in text.splitlines():
        line = _ANSI.sub("", raw).strip()
        if not line or not _REFERENCE.match(line):
            continue
        if line in seen:
            continue
        seen.add(line)
        references.append(line)
        if len(references) > CATALOGUE_LIMIT:
            raise ValueError("OpenCode model catalogue exceeds its limit")
    return references


def _rate(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and isfinite(value)
        and value >= 0
    )


def _cost_metadata(cost):
    """Return ``(free, pricing)`` for an OpenCode cost record, or ``None``.

    OpenCode reports either one object or a list of context-tier objects. A model
    is free only when every reported token rate is zero; missing or malformed
    prices stay unknown instead of being guessed as free.
    """
    if isinstance(cost, dict):
        entries = [cost]
    elif isinstance(cost, list) and cost:
        entries = cost
    else:
        return None
    values = []
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        input_rate, output_rate = entry.get("input"), entry.get("output")
        cache = entry.get("cache")
        if not isinstance(cache, dict):
            cache = {}
        cache_read = cache.get("read", 0)
        cache_write = cache.get("write", 0)
        if not all(_rate(value) for value in (input_rate, output_rate, cache_read, cache_write)):
            return None
        values.append((input_rate, output_rate, cache_read, cache_write))
    if not values:
        return None
    free = all(rate == 0 for value in values for rate in value)
    pricing = {"input": values[0][0], "output": values[0][1]}
    if values[0][2]:
        pricing["cache_read"] = values[0][2]
    if values[0][3]:
        pricing["cache_write"] = values[0][3]
    return free, pricing


def parse_model_metadata(text):
    """Read model labels, free/paid status and base prices from the local API.

    ``opencode api GET /api/model`` is a local, read-only catalogue. Its shape is
    experimental, so malformed input returns an empty map and the caller falls
    back to the documented ``opencode models`` reference list without prices.
    """
    text = _ANSI.sub("", text).strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}
    data = value.get("data") if isinstance(value, dict) else value
    if data is None and isinstance(value, dict):
        data = value.get("models")
    if not isinstance(data, list):
        return {}
    result = {}
    for item in data:
        if not isinstance(item, dict) or item.get("enabled") is False:
            continue
        provider, model = item.get("providerID"), item.get("modelID") or item.get("id")
        if not isinstance(provider, str) or not isinstance(model, str):
            continue
        reference = provider + "/" + model
        if not _REFERENCE.match(reference):
            continue
        label = item.get("name")
        info = {"label": label if isinstance(label, str) and label else model}
        cost = _cost_metadata(item.get("cost"))
        if cost is not None:
            info["free"], info["pricing"] = cost
        result[reference] = info
        if len(result) >= CATALOGUE_LIMIT:
            break
    return result


def validate_selection(model, parameters):
    """Require one explicit provider/model reference and its matching provider."""
    if not isinstance(model, str) or not _REFERENCE.match(model):
        raise ValueError("OpenCode needs an explicit provider/model reference")
    provider = parameters.get("provider")
    if (
        set(parameters) - {"provider"}
        or not isinstance(provider, str)
        or provider != model.split("/", 1)[0]
    ):
        raise ValueError("OpenCode requires the matching explicit provider and no other parameters")


def worker_command(executable):
    """The private command template consumed by a RelayFence command worker."""
    return [
        sys.executable,
        "-m",
        "relayfence.opencode_adapter",
        "--executable",
        str(executable),
        "--permission-mode",
        "danger-full-access",
        "--model",
        "{model}",
        "--parameters",
        "{parameters_json}",
    ]


def _write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)


def _strip_fence(text):
    lines = text.strip().splitlines()
    if len(lines) >= 3 and lines[0].strip().startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return text.strip()


def _result_value(answer):
    """Extract exactly one structured result from the final assistant text."""
    answer = _strip_fence(answer[:MAX_PAYLOAD_BYTES])
    if not answer:
        raise RuntimeError("OpenCode returned no final text")
    try:
        return CodexResult.model_validate_json(answer).model_dump(exclude_none=True)
    except ValueError:
        pass
    candidates = {}
    decoder = json.JSONDecoder()
    for offset, char in enumerate(answer):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(answer[offset:])
            parsed = CodexResult.model_validate(candidate).model_dump(exclude_none=True)
        except ValueError:
            continue
        candidates[json.dumps(parsed, sort_keys=True, ensure_ascii=False)] = parsed
    if len(candidates) != 1:
        raise RuntimeError("OpenCode did not return one unambiguous structured result")
    return next(iter(candidates.values()))


def result_from_events(text):
    """Read ``opencode run --format json`` text parts and extract the final answer.

    The same part may be emitted more than once. Later complete snapshots replace
    earlier ones; genuinely incremental text is appended. Tool parts are ignored.
    """
    parts, order = {}, []

    def keep(part, fallback):
        if not isinstance(part, dict) or part.get("type") != "text":
            return
        value = part.get("text")
        if not isinstance(value, str):
            return
        ident = part.get("id") or fallback
        if ident not in parts:
            order.append(ident)
            parts[ident] = value
            return
        previous = parts[ident]
        if value.startswith(previous):
            parts[ident] = value
        elif not previous.startswith(value):
            parts[ident] = previous + value

    index = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        index += 1
        fallback = "part-" + str(index)
        keep(event.get("part"), fallback)
        properties = event.get("properties")
        if isinstance(properties, dict):
            keep(properties.get("part"), fallback)
        if event.get("type") == "text" and isinstance(event.get("text"), str):
            keep({"id": fallback, "type": "text", "text": event["text"]}, fallback)
    return _result_value("\n".join(parts[ident] for ident in order))


def execute(executable, permission_mode, model, parameters, envelope, directory):
    validate_selection(model, parameters)
    if Path(executable).suffix.lower() in {".cmd", ".bat"}:
        raise ValueError("Use the OpenCode executable, not a Windows batch shim")
    if permission_mode not in _PERMISSION_AGENTS:
        raise ValueError("Invalid permission mode")
    prompt_file = directory / "opencode-prompt.txt"
    binding = current_session("opencode", envelope)
    _write_private(
        prompt_file,
        "Complete only the assigned task in this workspace. Other workers may edit concurrently. "
        "Do not commit, install packages, change authentication or security settings, or inspect "
        "RelayFence credentials. The runner owns your lease; do not claim, renew or complete tasks. "
        "Use only the supplied workspace. Report changed files and only checks that were actually "
        "run. Set outcome to completed only when the requested work is finished; otherwise use "
        "blocked or failed and explain why. Reply with ONE JSON object and nothing else, matching "
        "this exact schema:\n"
        + encode(result_schema())
        + "\nTask and shared inputs:\n"
        + encode(envelope)
        + "\n",
    )
    command = [
        executable,
        "run",
        # A private server gives this run the same lifetime and process tree as
        # the worker instead of attaching to a long-lived shared service.
        "--standalone",
        "--format",
        "json",
        "--model",
        model,
        "--agent",
        _PERMISSION_AGENTS[permission_mode],
        # The prompt can exceed a Windows command line; attach it as a file and
        # keep argv bounded.
        "--file",
        str(prompt_file),
        "Complete the attached RelayFence handoff and reply with the required JSON object only.",
    ]
    if binding and binding.session_id:
        command += ["--session", binding.session_id]
    env = {k: v for k, v in os.environ.items() if k != "RELAYFENCE_TOKEN"}
    from .mcp_catalogue import opencode_environment

    env = opencode_environment(envelope.get("selection", {}).get("mcp_servers"), env, Path.cwd())
    events_path = directory / "opencode-events.jsonl"
    with events_path.open("xb") as events:
        if os.name != "nt":
            os.chmod(events_path, 0o600)
        if binding:
            result = stream_process(command, b"", events, binding, "opencode", env)
            binding.finish(result.returncode == 0)
        else:
            result = subprocess.run(
                command,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=events,
                stderr=sys.stderr,
                check=False,
            )
    if result.returncode:
        raise RuntimeError("OpenCode execution failed; inspect the private run logs")
    if events_path.stat().st_size > EVENT_LIMIT:
        raise RuntimeError("OpenCode output exceeded its size limit")
    value = result_from_events(events_path.read_text(encoding="utf-8", errors="replace"))
    question = question_output(value)
    if question:
        return question
    if value["outcome"] != "completed":
        raise AgentIncomplete(value["outcome"], value["summary"])
    key = "tasks/" + envelope["task"]["task_id"] + "/result"
    if len(key) > 128:
        import hashlib

        key = (
            "tasks/" + hashlib.sha256(envelope["task"]["task_id"].encode()).hexdigest() + "/result"
        )
    version = next((o["version"] for o in envelope["task"]["outputs"] if o["key"] == key), 0)
    return {"outputs": [{"key": key, "expected_version": version, "value": value}]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", required=True)
    parser.add_argument(
        "--permission-mode",
        choices=["read-only", "workspace-write", "danger-full-access"],
        required=True,
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--parameters", required=True)
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(MAX_PAYLOAD_BYTES + 1)
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise ValueError("Input too large")
        result = execute(
            args.executable,
            args.permission_mode,
            args.model,
            json.loads(args.parameters),
            json.loads(raw),
            Path(os.environ["RELAYFENCE_RUN_DIRECTORY"]),
        )
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        return 0
    except AgentIncomplete as exc:
        print(
            json.dumps(
                {"outputs": [], "error": {"code": exc.code, "message": str(exc)}},
                ensure_ascii=True,
                allow_nan=False,
            )
        )
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError):
        print(
            "OpenCode adapter failed; inspect the private run logs and configuration",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
