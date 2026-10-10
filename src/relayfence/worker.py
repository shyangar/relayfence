"""Explicit local command adapter. The HTTP service never executes stored commands."""

import os
import signal
import subprocess
import time
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .client import RelayFenceClient
from .errors import CoordinationError
from .models import ModelId, Name, Output
from .store import MAX_PAYLOAD_BYTES, encode


class WorkerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: Name = "default"
    agent_id: Name
    command: list[str] = Field(min_length=2, max_length=64)
    cwd: str = "."
    timeout_seconds: int = Field(default=600, ge=1, le=86400, strict=True)
    lease_seconds: int = Field(default=120, ge=60, le=3600, strict=True)


class WorkerResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outputs: list[Output] = Field(min_length=1, max_length=16)
    reported_model: ModelId | None = None


def command_arguments(command, selection):
    """Only entire arguments are placeholders; no shell, eval or prompt interpolation."""
    if not command or "{" in command[0] or "}" in command[0]:
        raise ValueError("The executable must be fixed in the local configuration")
    if "{model}" not in command[1:]:
        raise ValueError("Local command must include a separate {model} argument")
    result = []
    for arg in command:
        if not arg or "\x00" in arg:
            raise ValueError("Command arguments must be nonempty and contain no NUL")
        if arg == "{model}":
            arg = selection["model"]
        elif arg == "{parameters_json}":
            arg = encode(selection["parameters"])
        elif arg.startswith("{parameter:") and arg.endswith("}"):
            key = arg[len("{parameter:") : -1]
            if key not in selection["parameters"]:
                raise ValueError(f"Required command parameter is not selected: {key}")
            arg = selection["parameters"][key]
        elif "{" in arg or "}" in arg:
            raise ValueError("Unsupported command placeholder")
        result.append(arg)
    if Path(result[0]).suffix.lower() in (".cmd", ".bat"):
        raise ValueError("Use an executable interpreter and script; batch files are not supported")
    return result


def save_private(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(encode(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def stop_process(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=10,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # It exited between poll() and killpg().
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def execute_command(command, cwd, envelope, directory, timeout, heartbeat, interval):
    save_private(directory / "input.json", envelope)
    env = {k: v for k, v in os.environ.items() if k != "RELAYFENCE_TOKEN"}
    with (
        (directory / "input.json").open("rb") as stdin,
        (directory / "stdout.json").open("xb") as stdout,
        (directory / "stderr.log").open("xb") as stderr,
    ):
        if os.name != "nt":
            os.chmod(directory / "stdout.json", 0o600)
            os.chmod(directory / "stderr.log", 0o600)
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            env=env,
            shell=False,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        deadline = time.monotonic() + timeout
        next_heartbeat = time.monotonic() + interval
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Command timed out; inspect the private run directory")
                if (
                    os.fstat(stdout.fileno()).st_size > MAX_PAYLOAD_BYTES
                    or os.fstat(stderr.fileno()).st_size > 1_048_576
                ):
                    raise RuntimeError("Command output exceeded its size limit")
                if time.monotonic() >= next_heartbeat:
                    heartbeat()
                    next_heartbeat = time.monotonic() + interval
                time.sleep(0.05)
            if process.returncode:
                raise RuntimeError(
                    f"Command exited with code {process.returncode}; inspect stderr.log"
                )
        finally:
            stop_process(process)
    if (directory / "stdout.json").stat().st_size > MAX_PAYLOAD_BYTES:
        raise RuntimeError("Command result exceeds 128 KiB")
    try:
        return WorkerResult.model_validate_json((directory / "stdout.json").read_bytes())
    except ValueError as exc:
        # Validation errors can echo untrusted output/secrets; do not print them.
        raise RuntimeError(
            "Command must emit one WorkerResult JSON object; inspect stdout.json"
        ) from exc


def run_task(client: RelayFenceClient, config_path: Path, task_id: str, directory: Path):
    config_path = config_path.resolve()
    try:
        config = WorkerConfig.model_validate_json(config_path.read_bytes())
    except ValueError as exc:
        raise ValueError("Invalid local worker configuration") from exc
    cwd = (config_path.parent / config.cwd).resolve()
    if not cwd.is_dir():
        raise ValueError("Worker cwd must be an existing directory")
    project = config.project
    task = client.call("task_get", project=project, task_id=task_id)["data"]
    if not task["role_id"]:
        raise ValueError("The command worker requires a role-assigned task")
    role = client.call("role_get", project=project, role_id=task["role_id"])["data"]
    if role["agent_id"] != config.agent_id:
        raise ValueError("Role is assigned to a different instance")
    agent = client.call("agent_get", project=project, agent_id=config.agent_id)["data"]
    if agent["mode"] != "command":
        raise ValueError("External agents configure their model in their own session")
    command = command_arguments(config.command, role["selection"])
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)

    def mutation(**fields):
        return {
            "project": project,
            "actor": config.agent_id,
            "request_id": uuid.uuid4().hex,
            **fields,
        }

    claim_request = mutation(
        task_id=task_id, lease_seconds=config.lease_seconds, selection=role["selection"]
    )
    save_private(directory / "claim-request.json", claim_request)
    claim = client.call("task_claim", **claim_request)["data"]
    save_private(directory / "claim-response.json", claim)
    lease = {
        "task_id": task_id,
        "fence": claim["task"]["fence"],
        "lease_token": claim["lease_token"],
    }
    execution = claim["task"]["execution"]
    # Use the atomic claim snapshot, including any role edits between reads and claim.
    if execution["agent"]["mode"] != "command":
        client.call("task_release", **mutation(**lease))
        raise ValueError("Agent mode changed before claim; inspect configuration")
    command = command_arguments(config.command, execution["requested"])
    envelope = {
        "task": claim["task"],
        "inputs": claim["inputs"],
        "selection": execution["requested"],
    }

    def heartbeat():
        client.call("task_heartbeat", **mutation(**lease, lease_seconds=config.lease_seconds))

    completion_saved = False
    try:
        result = execute_command(
            command,
            cwd,
            envelope,
            directory,
            config.timeout_seconds,
            heartbeat,
            min(20, config.lease_seconds / 3),
        )
        request = mutation(**lease, **result.model_dump(mode="json"))
        save_private(directory / "complete-request.json", request)
        completion_saved = True
        response = client.call("task_complete", **request)
        save_private(directory / "complete-response.json", response)
        return response
    finally:
        # Once completion was sent, its outcome may be unknown. Preserve its exact
        # request for replay; never release or execute the command a second time.
        if not completion_saved:
            try:
                client.call("task_release", **mutation(**lease))
            except CoordinationError:
                pass
