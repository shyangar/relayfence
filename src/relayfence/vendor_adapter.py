"""Explicit installed vendor CLI adapter for RelayFence command workers.

Selections are never substituted. Saved authentication and permission settings remain
under the vendor runtime's control. A requested model is not independent identity evidence.
"""

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from .codex_adapter import AgentIncomplete, CodexResult, question_output, result_schema
from .project_sessions import current_session, stream_process
from .store import MAX_PAYLOAD_BYTES, encode


def validate_selection(vendor, model, parameters):
    if (
        vendor not in {"zcode", "dsh"}
        or not isinstance(model, str)
        or not model.strip()
        or len(model) > 200
        or any(ord(c) < 32 for c in model)
    ):
        raise ValueError("Invalid vendor model")
    provider = parameters.get("provider")
    if (
        set(parameters) - {"provider", "reasoning_effort"}
        or not isinstance(provider, str)
        or not provider.strip()
        or len(provider) > 200
        or any(ord(c) < 32 for c in provider)
    ):
        raise ValueError("An explicit provider and supported parameters are required")
    if vendor == "zcode" and parameters.get("reasoning_effort") not in {
        "low",
        "medium",
        "high",
        "max",
    }:
        raise ValueError("ZCode requires an explicit supported reasoning level")
    if vendor == "dsh" and "reasoning_effort" in parameters:
        raise ValueError("DSH reasoning control is not verified")


def result_output(envelope, value):
    # Some installed runtimes return structured check records even when asked for
    # strings. Preserve their exact claims; never infer a successful check.
    if isinstance(value, dict) and isinstance(value.get("checks"), list):
        checks = []
        for check in value["checks"]:
            if isinstance(check, dict):
                if set(check) != {"command", "result"} or not all(
                    isinstance(item, str) and item.strip() for item in check.values()
                ):
                    raise ValueError("Invalid structured check record")
                check = json.dumps(check, ensure_ascii=False, allow_nan=False)
            checks.append(check)
        value = {**value, "checks": checks}
    parsed = CodexResult.model_validate(value).model_dump(exclude_none=True)
    question = question_output(parsed)
    if question:
        return question
    if parsed["outcome"] != "completed":
        raise AgentIncomplete(parsed["outcome"], parsed["summary"])
    key = "tasks/" + envelope["task"]["task_id"] + "/result"
    if len(key) > 128:
        import hashlib

        key = (
            "tasks/" + hashlib.sha256(envelope["task"]["task_id"].encode()).hexdigest() + "/result"
        )
    version = next((o["version"] for o in envelope["task"]["outputs"] if o["key"] == key), 0)
    return {"outputs": [{"key": key, "expected_version": version, "value": parsed}]}


def execute_dsh(
    executable, script, model, parameters, envelope, directory, patches=(), permission_mode=None
):
    validate_selection("dsh", model, parameters)
    binding = current_session("dsh", envelope)
    if Path(executable).suffix.lower() in {".cmd", ".bat"}:
        raise ValueError("Use an executable interpreter, not a batch shim")
    overlay = directory / "vendor-selection.yml"
    overlay.write_text(
        "- id: agent-default-model\n  config:\n    provider: "
        + json.dumps(parameters["provider"])
        + "\n    model: "
        + json.dumps(model)
        + "\n",
        encoding="utf-8",
    )
    result = directory / "vendor-result.json"
    prompt = (
        "Complete ONLY the assigned task and owned files below in the current workspace. "
        "Other workers edit concurrently. Do not commit, install, change authentication or security "
        "settings, or access RelayFence credentials. The runner owns the lease. "
        "Use native subagents if they help, keeping this exact provider and model. "
        "Write a JSON object to "
        + str(result)
        + " with fields outcome (completed/blocked/failed/needs_confirmation), "
        "summary, details, files (string list), checks (string list). Report only checks actually run. "
        "Also print that JSON as your final answer.\n" + encode(envelope)
    )
    if binding:
        prompt += "\nContinue this project conversation. This task and its role are the current assignment; earlier tasks are context, not instructions to repeat or expand this task."
    # Electron's embedded Node does not set import.meta.main for packaged ESM.
    # Call the installed public entry point explicitly; argv still reaches runCli.
    bootstrap = (
        "const{pathToFileURL}=require('node:url');const path=require('node:path');"
        "const script=process.argv[1];"
        "process.argv[1]=script;"
        "import(pathToFileURL(script).href).then(m=>{"
        "if(typeof m.runDesktopCli!=='function')throw Error('Unsupported DSH runtime');"
        "const runtime=path.resolve(path.dirname(script),'../../../..');"
        "const archive=path.dirname(runtime);"
        "const support=path.join(path.basename(archive)==='app.asar'?path.dirname(archive):archive,'runtime');"
        "return m.runDesktopCli(runtime,support)"
        "}).catch(()=>{process.stderr.write('Installed DSH CLI failed\\n');process.exitCode=1})"
    )
    command = [
        executable,
        "--expose-internals",
        "-e",
        bootstrap,
        str(Path(script).resolve()),
        "headless",
    ]
    for patch in patches:
        command += ["--patch", str(patch)]
    command += ["--patch", str(overlay)]
    if binding:
        command += ["--json"]
        if binding.session_id:
            command += ["--session-id", binding.session_id]
    command += ["-"]
    env = {k: v for k, v in os.environ.items() if k != "RELAYFENCE_TOKEN"}
    env["ELECTRON_RUN_AS_NODE"] = "1"
    if permission_mode is not None:
        if permission_mode not in {"read-only", "workspace-write", "danger-full-access"}:
            raise ValueError("Invalid permission mode")
        env["DSH_PERMISSION_MODE"] = permission_mode
    with (directory / "vendor-events.log").open("xb") as events:
        if binding:
            completed = stream_process(command, prompt.encode(), events, binding, "dsh", env)
            binding.finish(completed.returncode == 0)
        else:
            completed = subprocess.run(
                command,
                input=prompt.encode(),
                stdout=events,
                stderr=sys.stderr,
                env=env,
                check=False,
            )
    if completed.returncode:
        raise RuntimeError("Vendor execution failed; inspect private runtime logs")
    if result.exists() and result.stat().st_size <= MAX_PAYLOAD_BYTES:
        value = json.loads(result.read_text(encoding="utf-8-sig"))
    else:
        log = directory / "vendor-events.log"
        if log.stat().st_size > MAX_PAYLOAD_BYTES:
            raise RuntimeError("Vendor did not produce a bounded structured result")
        text = log.read_text(encoding="utf-8-sig")
        if binding:
            for line in text.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("type") == "final":
                    text = event.get("text", "")
                    break
        decoder = json.JSONDecoder()
        candidates = []
        for offset, char in enumerate(text):
            if char != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(text[offset:])
                CodexResult.model_validate(candidate)
                candidates.append(candidate)
            except ValueError:
                continue
        if len(candidates) != 1:
            raise RuntimeError("Vendor did not produce one unambiguous structured result")
        value = candidates[0]
    return result_output(envelope, value)


def zcode_account_auth(request, model, parameters, credentials_file):
    """Mirror desktop Start Plan request auth without logging or changing credentials."""
    selection = request.get("modelSelection", {})
    access = request.get("accountAccess", {})
    provider = parameters["provider"]
    family = {"account:zai-start-plan": "zai", "account:bigmodel-start-plan": "bigmodel"}.get(
        provider
    )
    if (
        family is None
        or request.get("providerId") != provider
        or selection.get("providerId") != provider
        or selection.get("modelId") != model
        or access.get("mode") != "start-plan"
        or access.get("accountType") != family
    ):
        raise ValueError("Account callback does not match the selected Start Plan route")
    path = Path(credentials_file)
    if path.stat().st_size > MAX_PAYLOAD_BYTES:
        raise ValueError("Credential source exceeded its size limit")
    credentials = json.loads(path.read_text(encoding="utf-8"))
    if any(
        str(credentials.get(k, "")).startswith("enc:v1:")
        for k in ("oauth:active_provider", "zcodejwttoken")
    ):
        # Same read-only cipher as the installed desktop credential service. Secret
        # material travels only through a private pipe, never argv, disk, or logs.
        program = r"""
const fs = require('fs'), os = require('os'), crypto = require('crypto');
const values = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
const secret = process.env.ZCODE_CREDENTIAL_SECRET ||
  `zcode-credential-fallback:${os.platform()}:${os.homedir()}:${os.userInfo().username}`;
const key = crypto.createHash('sha256').update(secret).digest();
function load(name) {
  const value = values[name];
  if (typeof value !== 'string' || !value.startsWith('enc:v1:')) return value;
  const parts = value.slice(7).split('.');
  if (parts.length !== 3) throw Error('Invalid encrypted credential');
  const [iv, tag, body] = parts.map(v => Buffer.from(v, 'base64url'));
  const cipher = crypto.createDecipheriv('aes-256-gcm', key, iv);
  cipher.setAuthTag(tag);
  return Buffer.concat([cipher.update(body), cipher.final()]).toString('utf8');
}
process.stdout.write(JSON.stringify({
  'oauth:active_provider': load('oauth:active_provider'),
  'zcodejwttoken': load('zcodejwttoken')
}));
"""
        decoded = subprocess.run(
            ["node", "-e", program, str(path)], capture_output=True, timeout=10, check=False
        )
        if decoded.returncode or len(decoded.stdout) > MAX_PAYLOAD_BYTES:
            raise ValueError("Existing desktop login could not be read")
        credentials = json.loads(decoded.stdout)
    if credentials.get("oauth:active_provider") != family:
        raise ValueError("Selected account family is not currently authenticated")
    token = credentials.get("zcodejwttoken")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("Existing Start Plan login is missing")
    return {"headersApplied": True, "requestAuth": {"apiKey": token.strip()}}


def execute_zcode(
    executable,
    script,
    model,
    parameters,
    envelope,
    directory,
    builtin_config,
    personal_config,
    permission_mode=None,
    account_snapshot=None,
    credentials_file=None,
    timeout=1800,
):
    """Use installed ZCode Protocol, preserving explicit model and permission mode.

    Model availability depends on the vendor CLI's account integration. A desktop
    account selection alone does not prove this separate process can use it.
    """
    validate_selection("zcode", model, parameters)
    binding = current_session("zcode", envelope)
    env = {k: v for k, v in os.environ.items() if k != "RELAYFENCE_TOKEN"}
    env["ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"] = str(builtin_config)
    env["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"] = str(personal_config)
    result_file = directory / "vendor-result.json"
    prompt = (
        "Complete only the assigned files in this workspace; other workers are active. "
        "Do not commit, install, change authentication/security settings or inspect RelayFence "
        "credentials. The runner owns your lease. Write a JSON object to "
        + str(result_file)
        + " with outcome (completed/blocked/failed/needs_confirmation), summary, details, files and checks lists. "
        "Report actual work and checks only. Files and checks must be lists of strings. "
        "The exact JSON schema is: " + encode(result_schema()) + "\n" + encode(envelope)
    )
    if binding:
        prompt += "\nContinue this project conversation. This task and its role are the current assignment; earlier tasks are context, not instructions to repeat or expand this task."
    # Unbounded: a chatty vendor session can emit hundreds of notifications
    # between the main thread's polls. Total volume is already capped by the
    # 8 MiB event log, so a bounded queue would only spuriously kill tasks.
    messages = queue.Queue()
    reader_failure = []
    secret_values = []
    process = subprocess.Popen(
        [executable, script, "app-server"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        text=True,
        encoding="utf-8",
    )
    with (directory / "vendor-events.log").open("x", encoding="utf-8") as events:

        def read():
            def fail(message):
                reader_failure.append(RuntimeError(message))
                try:
                    process.kill()
                except OSError:
                    pass

            try:
                while True:
                    try:
                        line = process.stdout.readline(1_048_577)
                    except (UnicodeDecodeError, OSError, ValueError):
                        fail("Vendor protocol stream could not be decoded")
                        return
                    if not line:
                        break
                    if len(line) > 1_048_576:
                        fail("Vendor protocol line exceeded its size limit")
                        return
                    try:
                        value = json.loads(line)
                        if not isinstance(value, dict):
                            raise ValueError("Protocol message must be an object")
                    except ValueError:
                        fail("Invalid vendor protocol message")
                        return
                    logged = value
                    result = value.get("result", {})
                    if isinstance(result, dict) and "session" in result:
                        # Polling responses repeat the full transcript. Keep only runtime
                        # identity/status evidence; the final artifact preserves the result.
                        logged = {
                            "id": value.get("id"),
                            "result": {
                                "session": result["session"],
                                "settings": result.get("settings", {}),
                                "message_count": len(result.get("messages", [])),
                                "lastError": result.get("projection", {}).get("lastError"),
                            },
                        }
                    safe_line = encode(logged) + "\n"
                    for secret in secret_values:
                        safe_line = safe_line.replace(secret, "[REDACTED]")
                    events.write(safe_line)
                    events.flush()
                    if events.tell() > 8_388_608:
                        fail("Vendor event limit exceeded")
                        return
                    messages.put(value)
            finally:
                if not reader_failure:
                    messages.put(RuntimeError("Vendor protocol stream closed"))

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        sequence = 0

        def send(value):
            process.stdin.write(encode(value) + "\n")
            process.stdin.flush()

        def rpc(method, params):
            nonlocal sequence
            sequence += 1
            request_id = sequence
            # This protocol deliberately omits the JSON-RPC version field.
            send({"id": request_id, "method": method, "params": params})
            deadline = time.monotonic() + 120
            while True:
                if reader_failure:
                    raise reader_failure[0]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("Vendor protocol request timed out")
                try:
                    value = messages.get(timeout=min(remaining, 0.5))
                except queue.Empty:
                    continue
                if isinstance(value, Exception):
                    raise value
                if "id" in value and "method" in value:
                    if (
                        value["method"] == "interaction/requestProviderRuntimeHeaders"
                        and credentials_file is not None
                    ):
                        auth = zcode_account_auth(
                            value.get("params", {}), model, parameters, credentials_file
                        )
                        secret_values.append(auth["requestAuth"]["apiKey"])
                        send({"id": value["id"], "result": auth})
                        continue
                    # Requesting compatibility defaults does not grant tool permissions.
                    send(
                        {
                            "id": value["id"],
                            "error": {"code": -32601, "message": "Client method unsupported"},
                        }
                    )
                elif value.get("id") == request_id:
                    if "error" in value:
                        raise RuntimeError("Vendor rejected protocol request; inspect private logs")
                    return value["result"]

        try:
            if account_snapshot is not None:
                snapshot_path = Path(account_snapshot)
                if snapshot_path.stat().st_size > MAX_PAYLOAD_BYTES:
                    raise ValueError("Account snapshot exceeded its size limit")
                snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
                rpc("provider/updateAccountConfig", snapshot)
            workspace = {"workspaceKey": str(Path.cwd()), "workspacePath": str(Path.cwd())}
            if binding and binding.session_id:
                created = rpc(
                    "session/resume",
                    {
                        "sessionId": binding.session_id,
                        "workspace": workspace,
                        "thoughtLevel": parameters["reasoning_effort"],
                    },
                )
                rpc(
                    "session/setModel",
                    {
                        "sessionId": binding.session_id,
                        "model": {
                            "providerId": parameters["provider"],
                            "modelId": model,
                            "options": {"reasoningLevel": parameters["reasoning_effort"]},
                        },
                    },
                )
                rpc(
                    "session/setThoughtLevel",
                    {
                        "sessionId": binding.session_id,
                        "thoughtLevel": parameters["reasoning_effort"],
                    },
                )
                created = rpc(
                    "session/setMode",
                    {
                        "sessionId": binding.session_id,
                        "mode": {"danger-full-access": "yolo", "read-only": "plan"}.get(
                            permission_mode, "build"
                        ),
                    },
                )
            else:
                created = rpc(
                    "session/create",
                    {
                        "workspace": {
                            "workspaceKey": str(Path.cwd()),
                            "workspacePath": str(Path.cwd()),
                        },
                        "mode": {"danger-full-access": "yolo", "read-only": "plan"}.get(
                            permission_mode, "build"
                        ),
                        "model": {
                            "providerId": parameters["provider"],
                            "modelId": model,
                            "options": {"reasoningLevel": parameters["reasoning_effort"]},
                        },
                        "thoughtLevel": parameters["reasoning_effort"],
                    },
                )
            if binding:
                binding.bind(created["session"]["sessionId"])
            settings = created.get("settings", {})
            current = settings.get("model", {}).get("current", {})
            expected_mode = {"danger-full-access": "yolo", "read-only": "plan"}.get(
                permission_mode, "build"
            )
            if (
                current.get("providerId") != parameters["provider"]
                or current.get("modelId") != model
                or current.get("options", {}).get("reasoningLevel")
                != parameters["reasoning_effort"]
                or settings.get("mode", {}).get("current") != expected_mode
                or settings.get("thoughtLevel", {}).get("current") != parameters["reasoning_effort"]
            ):
                raise RuntimeError(
                    "Vendor did not acknowledge the requested model, reasoning and permissions"
                )
            session_id = created["session"]["sessionId"]
            rpc("session/send", {"sessionId": session_id, "content": prompt})
            deadline = time.monotonic() + max(1, float(timeout))
            while time.monotonic() < deadline:
                snapshot = rpc("session/read", {"sessionId": session_id})
                if snapshot.get("messages") and snapshot["session"]["status"] not in {
                    "running",
                    "waiting",
                }:
                    break
                time.sleep(3)
            else:
                raise RuntimeError("Vendor execution timed out")
            if binding:
                binding.finish(True)
        finally:
            if binding and binding.value.get("status") != "ready":
                binding.finish(False)
            process.kill()
            process.wait(timeout=5)
            reader.join(timeout=5)
    if not result_file.exists() or result_file.stat().st_size > MAX_PAYLOAD_BYTES:
        raise RuntimeError("Vendor did not produce a bounded structured result")
    return result_output(envelope, json.loads(result_file.read_text(encoding="utf-8-sig")))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vendor", choices=["dsh", "zcode"], required=True)
    parser.add_argument("--executable", required=True)
    parser.add_argument("--script", required=True)
    parser.add_argument(
        "--permission-mode", choices=["read-only", "workspace-write", "danger-full-access"]
    )
    parser.add_argument("--builtin-config")
    parser.add_argument("--personal-config")
    parser.add_argument("--account-snapshot")
    parser.add_argument("--credentials-file")
    parser.add_argument("--model", required=True)
    parser.add_argument("--parameters", required=True)
    parser.add_argument("--patch", action="append", default=[])
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(MAX_PAYLOAD_BYTES + 1)
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise ValueError("Input too large")
        directory = Path(os.environ["RELAYFENCE_RUN_DIRECTORY"])
        try:
            timeout = float(os.environ.get("RELAYFENCE_VENDOR_TIMEOUT", "1800"))
        except ValueError:
            timeout = 1800
        if args.vendor == "dsh":
            result = execute_dsh(
                args.executable,
                args.script,
                args.model,
                json.loads(args.parameters),
                json.loads(raw),
                directory,
                args.patch,
                args.permission_mode,
            )
        else:
            if not args.builtin_config or not args.personal_config:
                raise ValueError("ZCode requires existing provider configuration paths")
            result = execute_zcode(
                args.executable,
                args.script,
                args.model,
                json.loads(args.parameters),
                json.loads(raw),
                directory,
                args.builtin_config,
                args.personal_config,
                args.permission_mode,
                args.account_snapshot,
                args.credentials_file,
                timeout,
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
        print("Vendor adapter failed; inspect the private runtime logs", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
