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
from .locking import WorkspaceBusy, local_lock
from .models import ModelId, Name, Output, QuestionPayload
from .processes import managed_process
from .run_diagnosis import diagnose
from .store import MAX_PAYLOAD_BYTES, encode


class WorkerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: Name = "default"
    agent_id: Name
    runtime: str = Field(default="", max_length=64)
    provider: str = Field(default="", max_length=200)
    adapter: Literal["command", "codex"] = "command"
    command: list[str] | None = Field(default=None, min_length=2, max_length=64)
    executable: str = "codex"
    sandbox: Literal["read-only", "workspace-write", "danger-full-access"] = "read-only"
    cwd: str = "."
    timeout_seconds: int = Field(default=600, ge=1, le=86400, strict=True)
    lease_seconds: int = Field(default=120, ge=60, le=3600, strict=True)
    heartbeat_timeout_seconds: float = Field(default=5, ge=0.2, le=30, allow_inf_nan=False)
    session_file: str | None = None

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


def adapter_command(config, selection, workspace=None, runtime=""):
    if config.adapter == "command":
        return command_arguments(config.command, selection)
    from .codex_adapter import validate_selection

    validate_selection(selection)
    command = [
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
    # Only the selected skills stay visible: the runtime's own roots are otherwise
    # discovered too, and `--ignore-user-config` would disable discovery entirely.
    # An empty selection must disable every discovered skill too; otherwise a
    # leftover staging directory (or a user-installed skill) would silently run.
    from .skill_catalogue import skills_config_value, unselected_skills

    skills = ""
    if config.adapter == "codex" or (runtime or "").casefold() == "codex":
        skills = skills_config_value(
            unselected_skills(runtime or "codex", selection.get("skills") or [], workspace)
        )
    if skills:
        command += ["--skills-config", skills]
    return command


class WorkerError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Name
    message: str = Field(min_length=1, max_length=2000)


class WorkerResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outputs: list[Output] = Field(default_factory=list, max_length=16)
    reported_model: ModelId | None = None
    error: WorkerError | None = None
    question: QuestionPayload | None = None

    @model_validator(mode="after")
    def success_or_failure(self):
        if sum((bool(self.outputs), bool(self.error), bool(self.question))) != 1:
            raise ValueError("Return outputs, a question or an error, exactly one")
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
            # A selected value becomes one argv element. Reject option-shaped
            # values so a model parameter cannot smuggle a new CLI flag.
            if not isinstance(arg, str) or not arg or arg.startswith("-"):
                raise ValueError(f"Invalid command parameter value: {key}")
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
    events = [
        directory / "codex-events.jsonl",
        directory / "opencode-events.jsonl",
        directory / "vendor-events.log",
    ]
    if (
        os.fstat(stdout.fileno()).st_size > MAX_PAYLOAD_BYTES
        or os.fstat(stderr.fileno()).st_size > 1_048_576
        or any(path.exists() and path.stat().st_size > 8_388_608 for path in events)
    ):
        raise RuntimeError("Command output exceeded its size limit")


class ActivityReporter:
    def __init__(self, directory, report):
        from .run_activity import ActivityReader

        self.reader = ActivityReader(directory)
        self.report = report
        self.queue = []
        self.pending = None

    async def flush(self):
        for _ in range(10):
            if self.pending is None:
                if not self.queue:
                    before = self.reader.offset
                    self.queue = self.reader.read()
                if not self.queue:
                    if self.reader.offset != before:
                        continue
                    break
                self.pending = self.queue[:8]
            await self.report(self.pending)
            del self.queue[: len(self.pending)]
            self.pending = None

    async def run(self, interval):
        while True:
            await asyncio.sleep(min(3.0, max(1.0, interval)))
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Exact pending batch and sender idempotency key survive retry.
                continue


async def report_activity(directory, report, interval):
    await ActivityReporter(directory, report).run(interval)


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
    extra_env=None,
    report=None,
):
    """Monitor execution independently of asynchronous, cancellable lease renewal."""
    if report is None:

        async def report(_entries):
            return None

    if stop is not None and stop.is_set():
        raise RuntimeError("Runner stopped before execution")
    save_private(directory / "input.json", envelope)
    # Strip everything RelayFence-specific from the inherited environment; the
    # adapter executable override is the one override a child adapter may see.
    allowlist = {"RELAYFENCE_OPENCODE_EXECUTABLE", "RELAYFENCE_VENDOR_TIMEOUT"}
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "RELAYFENCE_TOKEN" and (not key.startswith("RELAYFENCE_") or key in allowlist)
    }
    if extra_env:
        env.update(
            {
                str(key): str(value)
                for key, value in extra_env.items()
                if isinstance(key, str)
                and isinstance(value, str)
                and key
                and (
                    not key.startswith("RELAYFENCE_")
                    or key
                    in {
                        "RELAYFENCE_SESSION_FILE",
                        "RELAYFENCE_SESSION_PROJECT",
                        "RELAYFENCE_SESSION_RUNTIME",
                    }
                )
                and "\x00" not in key
                and "\x00" not in value
            }
        )
    env["RELAYFENCE_RUN_DIRECTORY"] = str(directory)
    # Let the vendor adapter finish its own protocol loop just before the worker's
    # hard deadline, so the worker still owns the final kill.
    env["RELAYFENCE_VENDOR_TIMEOUT"] = str(max(1, int(timeout) - 30))
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
        activity = ActivityReporter(directory, report)
        reporting = asyncio.create_task(activity.run(interval), name="relayfence-activity")
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
                # The lease may have failed during the final poll gap; a lost
                # lease must still win over a cleanly exited adapter.
                if renewal.done():
                    renewal.result()
                if process.returncode:
                    raise RuntimeError(
                        f"Command exited with code {process.returncode}; inspect stderr.log"
                    )
            # Tree cleanup precedes final log checks and workspace-lock release.
            # Include stderr/events written just before adapter exit.
            _check_output(directory, stdout, stderr)
            reporting.cancel()
            await asyncio.gather(reporting, return_exceptions=True)
            try:
                async with asyncio.timeout(min(3, heartbeat_timeout)):
                    await activity.flush()
            except Exception:
                pass
        finally:
            renewal.cancel()
            reporting.cancel()
            await asyncio.gather(renewal, reporting, return_exceptions=True)
    try:
        return WorkerResult.model_validate_json((directory / "stdout.json").read_bytes())
    except ValueError as exc:
        # Validation errors can echo untrusted output/secrets; do not print them.
        raise RuntimeError(
            "Command must emit one WorkerResult JSON object; inspect stdout.json"
        ) from exc


# The agent-authored summary inside WorkerResult.error is vendor text: it may
# quote prompt fragments or injected content, so only fixed sentences cross into
# the shared ledger. The full summary stays in the private run directory.
_AGENT_OUTCOME_MESSAGES = {
    "agent_blocked": (
        "成员判断该任务暂时无法继续，说明与产出已留在工作区和运行目录，请查看任务对话后再调整指令。"
    ),
    "agent_failed": "成员报告任务执行失败，详情已留在工作区和运行目录，请查看任务对话后重试或修改任务。",
}
_AGENT_OUTCOME_FALLBACK = "成员未按要求完成任务，详情已留在工作区和运行目录，请查看任务对话。"


def _failure_reason(exc, directory):
    """Map an execution failure to a ledger error code and an actionable sentence.

    A coordination error already carries a precise code and its own wording. Any
    other failure is classified from this run's artifacts, so a quota problem is not
    flattened into an opaque exit code.
    """
    if isinstance(exc, CoordinationError):
        return exc.code, str(exc)[:1500]
    if isinstance(exc, (RuntimeError, ValueError)):
        code, reason = diagnose(directory)
        detail = str(exc)
        # Keep the original wording when it says more than the generic exit status.
        if detail and "exited with code" not in detail:
            reason = f"{reason} ({detail[:200]})"
        return code, reason
    return diagnose(directory)[0], "Execution interrupted before publication"


def run_task(
    client: RelayFenceClient,
    config_path: Path,
    task_id: str,
    directory: Path,
    stop=None,
    env=None,
    config=None,
):
    config_path = config_path.resolve()
    if config is None:
        config = load_config(config_path)
    cwd = (config_path.parent / config.cwd).resolve()
    if not cwd.is_dir():
        raise ValueError("Worker cwd must be an existing directory")
    # A task lease protects ledger publication, not filesystem writes by other
    # tasks. Keep a shared checkout exclusive until isolated worktrees exist.
    # Different working directories can still execute concurrently.
    with local_lock(cwd / ".relayfence" / "worker.lock"):
        return _run_task(client, config, cwd, task_id, directory, stop, env)


def _run_task(client, config, cwd, task_id, directory, stop, extra_env=None):
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
        "skills": [],
        "user_decisions": "When blocked on a user choice or authorization, stop and return outcome=needs_confirmation with question {question, context, options}. Do not invent consent. Read user answers from task inputs on the next attempt.",
    }
    # Inject the member's skills into the workspace the adapter actually runs in.
    # The claim snapshot decides the selection, so a role edit mid-run cannot apply.
    selected_skills = execution["requested"].get("skills") or []
    runtime = execution["agent"].get("runtime") or ""
    # Each provider/worker slot of a software shares the same project binding.
    # The workspace lock above serializes turns and protects this private file.
    if config.session_file:
        extra_env = {
            **(extra_env or {}),
            "RELAYFENCE_SESSION_FILE": str(Path(config.session_file).resolve()),
            "RELAYFENCE_SESSION_PROJECT": project,
            "RELAYFENCE_SESSION_RUNTIME": runtime,
        }
    from .skill_catalogue import inject_skills, remove_injected

    skill_lock = None
    try:
        if selected_skills:
            # Runtimes load skills from a shared project directory. Hold the lock
            # for the whole run so another slot cannot replace the staged files
            # while this process is reading them. Skill-bearing runs therefore
            # serialize per workspace; runs without skills still proceed in
            # parallel once the lock is briefly available.
            skill_lock = local_lock(cwd / ".relayfence" / "skills.lock")
            skill_lock.__enter__()
            try:
                envelope["skills"] = inject_skills(cwd, runtime, selected_skills)
            except BaseException:
                skill_lock.__exit__(*sys.exc_info())
                skill_lock = None
                raise
        else:
            with local_lock(cwd / ".relayfence" / "skills.lock"):
                envelope["skills"] = inject_skills(cwd, runtime, selected_skills)
    except (ValueError, OSError) as exc:
        client.call("task_release", **mutation(**lease))
        raise ValueError("Skill injection failed: " + str(exc)) from exc
    except WorkspaceBusy:
        # Another slot is staging the same workspace. Release the lease so the
        # dispatcher can claim the task again next tick instead of silently
        # holding it until expiry.
        client.call("task_release", **mutation(**lease))
        raise
    save_private(directory / "skills.json", envelope["skills"])

    async def execute(command):
        # Control traffic has its own pool and overall deadline. Cancelling it must
        # never leave a background thread renewing a lease after execution ends.
        async with client.async_peer(timeout=config.heartbeat_timeout_seconds) as lease_client:

            async def heartbeat():
                await lease_client.call(
                    "task_heartbeat", **mutation(**lease, lease_seconds=config.lease_seconds)
                )

            activity_request = None

            async def report(entries):
                nonlocal activity_request
                if activity_request is None:
                    activity_request = mutation(**lease, entries=entries)
                await lease_client.call("task_activity", **activity_request)
                activity_request = None

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
                extra_env,
                report,
            )

    completion_saved = False
    try:
        command = adapter_command(
            config, execution["requested"], cwd, execution["agent"].get("runtime") or ""
        )
        result = asyncio.run(execute(command))
        if stop is not None and stop.is_set():
            raise RuntimeError("Runner stopped before publication")
        if result.question:
            request = mutation(**lease, question=result.question.model_dump())
            save_private(directory / "question-request.json", request)
            completion_saved = True
            response = client.call("task_question", **request)
            save_private(directory / "question-response.json", response)
            return response
        if result.error:
            # The error message body is vendor-authored text; publish only the
            # fixed sentence for this outcome code.
            raise CoordinationError(
                result.error.code,
                _AGENT_OUTCOME_MESSAGES.get(result.error.code, _AGENT_OUTCOME_FALLBACK),
            )
        request = mutation(**lease, **result.model_dump(mode="json", exclude={"error", "question"}))
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
                code, reason = _failure_reason(exc, directory)
                failure = mutation(
                    **lease,
                    error_code=code,
                    message=(
                        reason
                        + " · Run: "
                        + directory.name
                        + ". Inspect local logs before retrying."
                    )[:1500],
                )
                save_private(directory / "failure-request.json", failure)
                response = client.call("task_fail", **failure)
                save_private(directory / "failure-response.json", response)
            except (CoordinationError, OSError):
                pass
        raise
    finally:
        if skill_lock is not None:
            try:
                try:
                    remove_injected(cwd, runtime)
                except OSError:
                    pass
            finally:
                skill_lock.__exit__(None, None, None)
