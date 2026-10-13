"""Explicit local command adapter. The HTTP service never executes stored commands."""

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .client import RelayFenceClient
from .errors import CoordinationError
from .locking import local_lock
from .models import ModelId, Name, Output
from .processes import managed_process
from .store import MAX_PAYLOAD_BYTES, encode


class WorkerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: Name = "default"
    agent_id: Name
    adapter: Literal["command", "codex"] = "command"
    command: list[str] | None = Field(default=None, min_length=2, max_length=64)
    executable: str = "codex"
    sandbox: Literal["read-only", "workspace-write"] = "read-only"
    cwd: str = "."
    timeout_seconds: int = Field(default=600, ge=1, le=86400, strict=True)
    lease_seconds: int = Field(default=120, ge=60, le=3600, strict=True)
    heartbeat_timeout_seconds: float = Field(default=5, ge=0.2, le=30, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_adapter(self):
        if (self.adapter == "command") != (self.command is not None):
            raise ValueError(
                "Command adapters require a command; built-in adapters do not accept one"
            )
        if not self.executable or "\x00" in self.executable:
            raise ValueError("Invalid adapter executable")
        return self


def load_config(path):
    try:
        return WorkerConfig.model_validate_json(path.read_bytes())
    except ValueError as exc:
        raise ValueError("Invalid local worker configuration") from exc


def adapter_command(config, selection):
    if config.adapter == "command":
        return command_arguments(config.command, selection)
    from .codex_adapter import validate_selection

    validate_selection(selection)
    return [
        sys.executable,
        "-m",
        "relayfence.codex_adapter",
        "--executable",
        config.executable,
        "--sandbox",
        config.sandbox,
        "--model",
        selection["model"],
        "--parameters",
        encode(selection["parameters"]),
    ]


class WorkerError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Name
    message: str = Field(min_length=1, max_length=2000)


class WorkerResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outputs: list[Output] = Field(default_factory=list, max_length=16)
    reported_model: ModelId | None = None
    error: WorkerError | None = None

    @model_validator(mode="after")
    def success_or_failure(self):
        if bool(self.outputs) == bool(self.error):
            raise ValueError("Return outputs or an error, never both")
        return self


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


def replace_private(path, value):
    """Publish a complete snapshot; readers see the old or the new JSON document."""
    pending = path.with_name("." + path.name + "-" + uuid.uuid4().hex + ".tmp")
    try:
        save_private(pending, value)
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def _check_output(directory, stdout, stderr):
    events = directory / "codex-events.jsonl"
    if (
        os.fstat(stdout.fileno()).st_size > MAX_PAYLOAD_BYTES
        or os.fstat(stderr.fileno()).st_size > 1_048_576
        or (events.exists() and events.stat().st_size > 8_388_608)
    ):
        raise RuntimeError("Command output exceeded its size limit")


async def execute_command(
    command,
    cwd,
    envelope,
    directory,
    timeout,
    heartbeat,
    interval,
    stop=None,
    heartbeat_timeout=5,
):
    """Monitor execution independently of asynchronous, cancellable lease renewal."""
    if stop is not None and stop.is_set():
        raise RuntimeError("Runner stopped before execution")
    save_private(directory / "input.json", envelope)
    env = {k: v for k, v in os.environ.items() if k != "RELAYFENCE_TOKEN"}
    env["RELAYFENCE_RUN_DIRECTORY"] = str(directory)
    with (
        (directory / "stdout.json").open("xb") as stdout,
        (directory / "stderr.log").open("xb") as stderr,
    ):
        if os.name != "nt":
            os.chmod(directory / "stdout.json", 0o600)
            os.chmod(directory / "stderr.log", 0o600)
        deadline = time.monotonic() + timeout

        async def renew():
            while True:
                await asyncio.sleep(interval)
                try:
                    async with asyncio.timeout(heartbeat_timeout):
                        await heartbeat()
                except TimeoutError as exc:
                    raise CoordinationError(
                        "heartbeat_timeout", "Lease heartbeat timed out; execution stopped"
                    ) from exc

        renewal = asyncio.create_task(renew(), name="relayfence-lease")
        try:
            with managed_process(
                command,
                input_path=directory / "input.json",
                cwd=cwd,
                env=env,
                stdout=stdout,
                stderr=stderr,
            ) as process:
                while True:
                    if stop is not None and stop.is_set():
                        raise RuntimeError("Runner stopped before completion")
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Command timed out; inspect the private run directory")
                    if renewal.done():
                        renewal.result()  # Loss of lease must win over an adapter's late result.
                    _check_output(directory, stdout, stderr)
                    if process.poll() is not None:
                        break
                    await asyncio.sleep(0.05)
                if process.returncode:
                    raise RuntimeError(
                        f"Command exited with code {process.returncode}; inspect stderr.log"
                    )
            # Tree cleanup precedes final log checks and workspace-lock release.
            # Include stderr/events written just before adapter exit.
            _check_output(directory, stdout, stderr)
        finally:
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)
    try:
        return WorkerResult.model_validate_json((directory / "stdout.json").read_bytes())
    except ValueError as exc:
        # Validation errors can echo untrusted output/secrets; do not print them.
        raise RuntimeError(
            "Command must emit one WorkerResult JSON object; inspect stdout.json"
        ) from exc


def run_task(client: RelayFenceClient, config_path: Path, task_id: str, directory: Path, stop=None):
    config_path = config_path.resolve()
    config = load_config(config_path)
    cwd = (config_path.parent / config.cwd).resolve()
    if not cwd.is_dir():
        raise ValueError("Worker cwd must be an existing directory")
    with local_lock(cwd / ".relayfence" / "worker.lock"):
        return _run_task(client, config, cwd, task_id, directory, stop)


def _run_task(client, config, cwd, task_id, directory, stop):
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
    envelope = {
        "task": claim["task"],
        "inputs": claim["inputs"],
        "selection": execution["requested"],
    }

    async def execute(command):
        # Control traffic has its own pool and overall deadline. Cancelling it must
        # never leave a background thread renewing a lease after execution ends.
        async with client.async_peer(timeout=config.heartbeat_timeout_seconds) as lease_client:

            async def heartbeat():
                await lease_client.call(
                    "task_heartbeat", **mutation(**lease, lease_seconds=config.lease_seconds)
                )

            return await execute_command(
                command,
                cwd,
                envelope,
                directory,
                config.timeout_seconds,
                heartbeat,
                min(3, config.lease_seconds / 3),
                stop,
                config.heartbeat_timeout_seconds,
            )

    completion_saved = False
    try:
        command = adapter_command(config, execution["requested"])
        result = asyncio.run(execute(command))
        if stop is not None and stop.is_set():
            raise RuntimeError("Runner stopped before publication")
        if result.error:
            raise CoordinationError(result.error.code, result.error.message)
        request = mutation(**lease, **result.model_dump(mode="json", exclude={"error"}))
        save_private(directory / "complete-request.json", request)
        completion_saved = True
        response = client.call("task_complete", **request)
        save_private(directory / "complete-response.json", response)
        return response
    except BaseException as exc:
        # Once completion was sent, its outcome may be unknown. Preserve its exact
        # request for replay; never release or execute the command a second time.
        if not completion_saved:
            try:
                failure = mutation(
                    **lease,
                    error_code=exc.code
                    if isinstance(exc, CoordinationError)
                    else "execution_failed",
                    message=(
                        (
                            str(exc)
                            if isinstance(exc, (RuntimeError, ValueError, CoordinationError))
                            else "Execution interrupted before publication"
                        )[:1500]
                        + " · Run: "
                        + directory.name
                        + ". Inspect local logs before retrying."
                    ),
                )
                save_private(directory / "failure-request.json", failure)
                response = client.call("task_fail", **failure)
                save_private(directory / "failure-response.json", response)
            except (CoordinationError, OSError):
                pass
        raise
