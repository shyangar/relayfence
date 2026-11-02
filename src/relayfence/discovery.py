"""Read installed runtime catalogues; public metadata never contains local credentials."""

import hashlib
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

LIMIT = 2_097_152
# AgentPut.models is capped at 64, and local setup registers one agent per provider.
PROVIDER_MODEL_LIMIT = 64
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_OPENCODE_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "google": "GOOGLE_GENERATIVE_AI_API_KEY",
    "isoft": "ISOFT_API_KEY",
    "openai": "OPENAI_API_KEY",
    "opencode": "OPENCODE_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "zai": "ZAI_API_KEY",
}


def _auth(kind, authenticated, credential_env=None, source=None, message=""):
    return {
        "kind": kind,
        "authenticated": bool(authenticated),
        "credential_env": credential_env if isinstance(credential_env, str) else None,
        "source": source if isinstance(source, str) else None,
        "stored": False,
        "masked": None,
        "message": str(message or ""),
    }


def _dotenv_has(path, name):
    if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
        return False
    try:
        path = Path(path)
        if not path.is_file() or path.stat().st_size > LIMIT:
            return False
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return False
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        if separator and key.strip() == name and value.strip().strip("'\"") != "":
            return True
    return False


def _credential_source(name, *paths):
    """Return a non-secret source label for an environment credential."""
    if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
        return None
    if os.environ.get(name):
        return "environment"
    if any(_dotenv_has(path, name) for path in paths if path is not None):
        return "dotenv"
    return None


def _codex_auth():
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    path = home / "auth.json"
    data = {}
    try:
        if path.is_file() and path.stat().st_size <= LIMIT:
            data = _read_json(path)
    except (OSError, ValueError, TypeError):
        data = {}
    if isinstance(data, dict) and (data.get("tokens") or data.get("OPENAI_API_KEY")):
        return _auth("account", True, "OPENAI_API_KEY", "vendor", "已使用本机 Codex 登录")
    source = _credential_source("OPENAI_API_KEY", Path.cwd() / ".env")
    if source:
        return _auth("account", True, "OPENAI_API_KEY", source, "已从本机环境读取 OPENAI_API_KEY")
    return _auth(
        "account",
        False,
        "OPENAI_API_KEY",
        None,
        "请在 Codex 客户端登录，或配置 OPENAI_API_KEY",
    )


def _opencode_auth(executable, env, timeout):
    """Read the non-secret provider/auth source list printed by ``opencode auth list``."""
    try:
        with _process([executable, "auth", "list"], env) as process:
            output = _read_output(process, timeout)
            code = process.wait(timeout=min(timeout, 5))
    except (OSError, TimeoutError, queue.Empty, subprocess.SubprocessError, ValueError):
        return {}
    if code:
        return {}
    result = {}
    for raw in output.splitlines():
        line = _ANSI.sub("", raw).strip()
        if not line:
            continue
        parts = [part.strip() for part in re.split(r"\s{2,}|\t+", line) if part.strip()]
        if len(parts) < 2:
            continue
        name = parts[0].casefold().replace(" ", "")
        credential_env = parts[1] if _ENV_NAME.fullmatch(parts[1]) else None
        # The vendor table is display text, not a safe label. Keep the one source
        # that changes behaviour and collapse everything else, so unexpected
        # wording cannot reach the shared catalogue.
        raw_source = parts[2].casefold() if len(parts) > 2 else ""
        source = "oauth" if raw_source == "oauth" else "configured"
        result[name] = {
            "credential_env": credential_env,
            "source": source,
        }
    return result


def _opencode_key_env(provider):
    return _OPENCODE_KEY_ENV.get(str(provider).casefold().replace(" ", ""))


def _zcode_auth(home):
    path = Path(home) / "credentials.json"
    data = {}
    try:
        if path.is_file() and path.stat().st_size <= LIMIT:
            data = _read_json(path)
    except (OSError, ValueError, TypeError):
        data = {}
    if isinstance(data, dict):
        values = [data.get("zcodejwttoken")]
        values.extend(
            value
            for key, value in data.items()
            if (key.endswith(":access_token") or key.endswith(":api-key")) and value
        )
        if any(values):
            return _auth("account", True, None, "vendor", "已使用本机 ZCode 登录")
    return _auth("account", False, None, None, "请先在 ZCode 客户端登录，然后重新扫描")


@dataclass
class DiscoveryResult:
    catalogue: list[dict]
    runners: dict[str, dict]


def _read_json(path):
    if path.stat().st_size > LIMIT:
        raise ValueError("Configuration exceeds discovery limit")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _snapshot(directory, name, value):
    data = json.dumps(value, ensure_ascii=True, allow_nan=False).encode()
    digest = hashlib.sha256(data).hexdigest()[:20]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}-{digest}.json"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
    return path


def _install_roots(software):
    """Use package registration, PATH and conventional per-user installations."""
    roots = []
    override = os.environ.get(f"RELAYFENCE_{software.upper()}_HOME")
    if override:
        roots.append(Path(override))
    if os.name == "nt":
        import winreg

        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for branch in (
                r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
                r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
            ):
                try:
                    with winreg.OpenKey(hive, branch) as parent:
                        for index in range(winreg.QueryInfoKey(parent)[0]):
                            try:
                                with winreg.OpenKey(parent, winreg.EnumKey(parent, index)) as key:
                                    name = str(winreg.QueryValueEx(key, "DisplayName")[0]).lower()
                                    if software not in name and not (
                                        software == "dsh" and "deepseek harness" in name
                                    ):
                                        continue
                                    for field in ("InstallLocation", "DisplayIcon"):
                                        try:
                                            value = str(winreg.QueryValueEx(key, field)[0])
                                            path = Path(value.split(",")[0].strip('"'))
                                            roots.append(
                                                path if field == "InstallLocation" else path.parent
                                            )
                                        except OSError:
                                            pass
                            except OSError:
                                continue
                except OSError:
                    continue
    for name in ("ZCode", "zcode") if software == "zcode" else ("DeepSeek Harness", "dsh"):
        command = shutil.which(name)
        if command:
            roots.append(Path(command).parent)
        for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles")):
            if base:
                roots.extend([Path(base) / name, Path(base) / "Programs" / name])
        roots.extend([Path("/opt") / name, Path("/Applications") / f"{name}.app/Contents"])
    return list(dict.fromkeys(roots))


def _codex_executable():
    override = os.environ.get("RELAYFENCE_CODEX_EXECUTABLE")
    if override:
        return override
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share")) / "OpenAI/Codex/bin"
    candidates = list(base.glob("*/codex.exe")) + list(base.glob("codex.exe"))
    # Hash-named version directories cannot be ordered lexically.
    if candidates:
        return str(max(candidates, key=lambda path: path.stat().st_mtime_ns))
    found = shutil.which("codex")
    if found and Path(found).suffix.lower() not in {".cmd", ".bat"}:
        return found
    return None


@contextmanager
def _process(command, env=None):
    """Own every probe's descendants and reap pipes even on timeout/parse failure."""

    # RPC needs an interactive input pipe. The worker process helper's file gate
    # owns Windows descendants; use its job object directly for this read-only probe.
    job = None
    process = None
    try:
        if os.name == "nt":
            from ._windows_job import WindowsJob

            job = WindowsJob()
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=(
                env
                if env is not None
                else {k: v for k, v in os.environ.items() if k != "RELAYFENCE_TOKEN"}
            ),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        if job:
            job.assign(process.pid)
        yield process
    finally:
        try:
            if job:
                job.close()
            elif process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            if process is not None:
                if process.poll() is None:
                    process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if process is not None:
                for stream in (process.stdin, process.stdout):
                    if stream:
                        try:
                            stream.close()
                        except OSError:
                            pass


class _Protocol:
    def __init__(self, process, timeout):
        self.process = process
        self.deadline = time.monotonic() + timeout
        self.messages = queue.Queue(maxsize=256)
        self.sequence = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        while True:
            line = self.process.stdout.readline(LIMIT + 1)
            if not line or len(line) > LIMIT:
                break
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    self.messages.put_nowait(value)
            except (ValueError, queue.Full):
                break
        try:
            self.messages.put_nowait(None)
        except queue.Full:
            pass

    def send(self, value):
        self.process.stdin.write(json.dumps(value).encode() + b"\n")
        self.process.stdin.flush()

    def rpc(self, method, params):
        self.sequence += 1
        ident = self.sequence
        self.send({"id": ident, "method": method, "params": params})
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Runtime discovery timed out")
            value = self.messages.get(timeout=remaining)
            if value is None:
                raise ValueError("Runtime protocol closed")
            if "method" in value and "id" in value:
                self.send({"id": value["id"], "error": {"code": -32601, "message": "Unsupported"}})
            elif value.get("id") == ident:
                if "error" in value:
                    raise ValueError("Runtime rejected discovery")
                return value["result"]


def _read_output(process, timeout):
    """Read a bounded non-JSON probe stream before the probe deadline.

    ``_Protocol`` handles newline-delimited JSON. Plain-text catalogues such as
    ``opencode models`` need the same deadline and size discipline without a JSON
    assumption.
    """
    messages = queue.Queue(maxsize=4096)

    def read():
        total = 0
        try:
            while True:
                line = process.stdout.readline(LIMIT + 1)
                if not line:
                    break
                total += len(line)
                if len(line) > LIMIT or total > LIMIT:
                    messages.put_nowait(ValueError("Runtime discovery output exceeded its limit"))
                    return
                messages.put_nowait(line)
        except (OSError, ValueError):
            pass
        finally:
            try:
                messages.put_nowait(None)
            except queue.Full:
                pass

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    output = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Runtime discovery timed out")
        try:
            line = messages.get(timeout=remaining)
        except queue.Empty as exc:
            raise TimeoutError("Runtime discovery timed out") from exc
        if isinstance(line, BaseException):
            raise line
        if line is None:
            break
        output.append(line)
    reader.join(timeout=1)
    return b"".join(output).decode("utf-8", errors="replace")


def _opencode_metadata(executable, env, timeout):
    """Read optional labels/prices from OpenCode's local model API.

    The documented ``opencode models`` command remains the authoritative list.
    This enrichment is best-effort: an older CLI, an unavailable local service or
    a changed experimental payload must not make the whole runtime unavailable.
    """
    try:
        with _process([executable, "api", "--standalone", "GET", "/api/model"], env) as process:
            output = _read_output(process, timeout)
            try:
                code = process.wait(timeout=min(timeout, 5))
            except subprocess.TimeoutExpired:
                return {}
    except (
        OSError,
        ValueError,
        RuntimeError,
        TimeoutError,
        queue.Empty,
        subprocess.SubprocessError,
    ):
        return {}
    if code:
        return {}
    from .opencode_adapter import parse_model_metadata

    try:
        return parse_model_metadata(output)
    except (TypeError, ValueError):
        return {}


def _discover_opencode(directory, timeout):
    from .opencode_adapter import find_executable, parse_models, worker_command

    started = time.monotonic()
    executable = find_executable()
    if not executable:
        raise FileNotFoundError("OpenCode is not installed")
    if Path(executable).suffix.lower() in {".cmd", ".bat"}:
        raise ValueError("Use the OpenCode executable, not a Windows batch shim")
    env = {k: v for k, v in os.environ.items() if k != "RELAYFENCE_TOKEN"}
    with _process([executable, "models"], env) as process:
        output = _read_output(process, timeout)
        try:
            code = process.wait(timeout=min(timeout, 5))
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("Runtime discovery timed out") from exc
    if code:
        raise RuntimeError("OpenCode model listing failed")
    references = parse_models(output)
    if not references:
        raise ValueError("OpenCode reported no usable models")
    remaining = timeout - (time.monotonic() - started)
    auth_map = _opencode_auth(executable, env, min(5.0, remaining)) if remaining > 0.1 else {}
    remaining = timeout - (time.monotonic() - started)
    metadata = _opencode_metadata(executable, env, min(8.0, remaining)) if remaining > 0.1 else {}
    rows, per_provider = [], {}
    for reference in references:
        provider, model = reference.split("/", 1)
        # Large gateways such as OpenRouter exceed the one-agent model limit. Keep a
        # bounded prefix instead of rejecting the whole runtime catalogue.
        if per_provider.get(provider, 0) >= PROVIDER_MODEL_LIMIT:
            continue
        per_provider[provider] = per_provider.get(provider, 0) + 1
        info = metadata.get(reference, {})
        option = _model(reference, info.get("label") or model, provider)
        if "free" in info:
            option["free"] = info["free"]
        if "pricing" in info:
            option["pricing"] = info["pricing"]
        rows.append((provider, provider, option))
    providers = _providers(rows)
    for provider in providers:
        key = str(provider["id"]).casefold().replace(" ", "")
        info = auth_map.get(key) or auth_map.get(
            str(provider.get("label", "")).casefold().replace(" ", "")
        )
        env_name = info.get("credential_env") if info else _opencode_key_env(provider["id"])
        source = (
            info.get("source")
            if info
            else _credential_source(env_name, Path.cwd() / ".env")
            if env_name
            else None
        )
        if source:
            kind = "account" if source == "oauth" else "api_key"
            if source == "environment":
                message = f"已从环境变量读取 {env_name}"
            elif source == "dotenv":
                message = f"已从 .env 读取 {env_name}"
            else:
                message = "已检测到本机登录或已配置密钥"
            auth = _auth(kind, True, env_name, source, message)
        elif key == "opencode":
            auth = _auth("none", True, None, "free", "OpenCode 免费模型无需登录")
        else:
            auth = _auth(
                "api_key" if env_name else "unknown",
                False,
                env_name,
                None,
                f"缺少 {env_name}，请配置密钥" if env_name else "未检测到登录或密钥",
            )
        provider["auth"] = auth
    return providers, {"adapter": "command", "command": worker_command(executable)}


def _model(ident, label, provider, efforts=(), *, codex=False):
    if not isinstance(ident, str) or not ident or len(ident) > 200:
        raise ValueError("Invalid discovered model")
    efforts = list(dict.fromkeys(e for e in efforts if isinstance(e, str) and 0 < len(e) <= 100))
    if len(efforts) > 32:
        raise ValueError("Too many reasoning settings")
    parameters = {} if codex else {"provider": [provider]}
    if efforts:
        parameters["reasoning_effort"] = efforts
    return {
        "id": ident,
        "label": str(label or ident)[:200],
        "parameters": parameters,
        "reasoning_efforts": efforts,
    }


def _providers(rows):
    result = {}
    for provider, label, model in rows:
        item = result.setdefault(provider, {"id": provider, "label": label, "models": []})
        if model["id"] not in {m["id"] for m in item["models"]}:
            item["models"].append(model)
        if len(item["models"]) > PROVIDER_MODEL_LIMIT:
            raise ValueError("Catalogue exceeds registry model limit")
    return list(result.values())


def _discover_codex(directory, timeout):
    executable = _codex_executable()
    if not executable:
        raise FileNotFoundError("Codex is not installed")
    with _process([executable, "app-server"]) as process:
        protocol = _Protocol(process, timeout)
        protocol.rpc(
            "initialize", {"clientInfo": {"name": "relayfence-discovery", "version": "0.3.0"}}
        )
        protocol.send({"method": "initialized"})
        models, cursor = [], None
        while True:
            params = {"includeHidden": False, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            response = protocol.rpc("model/list", params)
            models.extend(response["data"])
            cursor = response.get("nextCursor")
            if not cursor:
                break
            if len(models) > 256:
                raise ValueError("Runtime catalogue too large")
    rows = [
        (
            "openai",
            "OpenAI",
            _model(
                m.get("model", m["id"]),
                m.get("displayName"),
                "openai",
                [e["reasoningEffort"] for e in m.get("supportedReasoningEfforts", [])],
                codex=True,
            ),
        )
        for m in models
    ]
    providers = _providers(rows)
    auth = _codex_auth()
    for provider in providers:
        provider["auth"] = dict(auth)
    return providers, {
        "adapter": "codex",
        "executable": executable,
        "sandbox": "danger-full-access",
    }


def _account_snapshot(log_directory, builtin_revision):
    """Recover the desktop's delivered revision without copying any log credentials."""
    for path in sorted(log_directory.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[
        :4
    ]:
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 4 * LIMIT))
            lines = stream.read(4 * LIMIT).decode("utf-8", errors="replace").splitlines()
        for line in reversed(lines):
            if '"receivedRevision"' not in line:
                continue
            try:
                record = json.loads(line[line.index("{") :])
                revision = record["receivedRevision"]
                base, providers, states = json.loads(revision.removeprefix("account:"))
                if not base.startswith(f"zcode-builtin:{builtin_revision}:"):
                    continue
                return {
                    "revision": revision,
                    "basedOnZCodeBuiltinRevision": base,
                    "providers": {p["providerId"]: p["config"] for p in providers},
                    "states": states,
                }
            except (ValueError, KeyError, TypeError):
                continue
    return None


def _vendor_command(vendor, executable, script):
    return [
        sys.executable,
        "-m",
        "relayfence.vendor_adapter",
        "--vendor",
        vendor,
        "--executable",
        str(executable),
        "--script",
        str(script),
        "--permission-mode",
        "danger-full-access",
        "--model",
        "{model}",
        "--parameters",
        "{parameters_json}",
    ]


def _discover_zcode(directory, timeout):
    script = next(
        (
            r / "resources/glm/zcode.cjs"
            for r in _install_roots("zcode")
            if (r / "resources/glm/zcode.cjs").is_file()
        ),
        None,
    )
    executable = shutil.which("node")
    if script is None or not executable:
        raise FileNotFoundError("ZCode or Node is not installed")
    home = Path(os.environ.get("ZCODE_HOME", Path.home() / ".zcode")) / "v2"
    candidates = list((home / "runtime/provider").glob("*/*/*/zcode-builtin.json"))
    candidates.append(script.parents[1] / "config/provider/zcode-builtin.json")
    builtin = max((p for p in candidates if p.is_file()), key=lambda p: p.stat().st_mtime)
    config = _read_json(builtin)
    personal = home / "provider_config.json"
    snapshot = _account_snapshot(home / "logs", config.get("revision"))
    env = {k: v for k, v in os.environ.items() if k != "RELAYFENCE_TOKEN"}
    env.update(
        ZCODE_BUILTIN_PROVIDER_CONFIG_FILE=str(builtin),
        ZCODE_PERSONAL_PROVIDER_CONFIG_FILE=str(personal),
    )
    with _process([executable, str(script), "app-server"], env) as process:
        protocol = _Protocol(process, timeout)
        if snapshot:
            protocol.rpc("provider/updateAccountConfig", snapshot)
        response = protocol.rpc(
            "session/create",
            {
                "workspace": {
                    "workspaceKey": str(directory.resolve()),
                    "workspacePath": str(directory.resolve()),
                },
                "mode": "plan",
            },
        )
    rows = []
    for m in response["settings"]["model"]["available"]:
        provider = m["ref"]["providerId"]
        efforts = [e["value"] for e in m.get("reasoning", {}).get("levels", [])]
        rows.append(
            (
                provider,
                m.get("providerLabel", provider),
                _model(m["ref"]["modelId"], m.get("label"), provider, efforts),
            )
        )
    command = _vendor_command("zcode", executable, script)
    command += [
        "--builtin-config",
        str(builtin),
        "--personal-config",
        str(personal),
        "--credentials-file",
        str(home / "credentials.json"),
    ]
    if snapshot:
        command += ["--account-snapshot", str(_snapshot(directory, "zcode-account", snapshot))]
    providers = _providers(rows)
    auth = _zcode_auth(home)
    for provider in providers:
        provider["auth"] = dict(auth)
    return providers, {"adapter": "command", "command": command}


_DSH_PROBE = r"""
(async()=>{
const fs=require('fs'),path=require('path'),{pathToFileURL}=require('url');
const root=process.argv[1],home=process.argv[2],yaml=require(path.join(root,'node_modules/yaml'));
const read=p=>fs.existsSync(p)?yaml.parse(fs.readFileSync(p,'utf8')):[];
const patches=[];
for(const profile of ['desktop','headless']) {
 const entries=read(path.join(home,'profiles',profile,'cordis.patch.yml'))||[];
 for(const e of entries) if(['llm-deepseek','llm-deepseek-account','llm-pi-ai'].includes(e.id)) patches.push(e);
}
const configs={};for(const e of patches) configs[e.id]={...(configs[e.id]||{}),...(e.config||{})};
const mod=await import(pathToFileURL(path.join(root,'node_modules/@deepseek-ai/dsh-llm-deepseek/lib/index.js')).href);
const rows=[];
for(const [id,key,label,authKind] of [['deepseek-official','llm-deepseek','DeepSeek','api_key'],['deepseek-account','llm-deepseek-account','DeepSeek Account','account']]){
 const c=configs[key]||{};
 const options=mod.resolveAdapterOptions(c,{get:n=>process.env[n]===undefined?undefined:{value:process.env[n]}});
 const credentialEnv=authKind==='api_key'?(c.apiKeyEnv||'DEEPSEEK_API_KEY'):null;
 rows.push({id,label,authKind,credentialEnv,models:options.models.map(m=>({id:m.id,label:m.name||m.id}))});
}
for(const [id,c] of Object.entries(configs['llm-pi-ai']?.providers||{})){
 rows.push({id,label:c.displayName||id,authKind:c.apiKeyEnv?'api_key':'none',credentialEnv:c.apiKeyEnv||null,models:(c.models||[]).map(m=>({id:m.id,label:m.name||m.id}))});
}
console.log(JSON.stringify({rows,patches}));
})().catch(()=>{process.exitCode=1});
"""


def _discover_dsh(directory, timeout):
    roots = _install_roots("dsh")
    root = next((r for r in roots if (r / "resources/app.asar").is_file()), None)
    if root is None:
        raise FileNotFoundError("DSH is not installed")
    executable = next(
        (
            root / n
            for n in ("DeepSeek Harness.exe", "deepseek-harness", "dsh")
            if (root / n).is_file()
        ),
        None,
    )
    if executable is None:
        raise FileNotFoundError("DSH executable unavailable")
    runtime = root / "resources/app.asar/dsh"
    home = Path(os.environ.get("DSH_HOME", Path.home() / ".dsh"))
    env = {k: v for k, v in os.environ.items() if k != "RELAYFENCE_TOKEN"}
    env["ELECTRON_RUN_AS_NODE"] = "1"
    with _process(
        [str(executable), "--expose-internals", "-e", _DSH_PROBE, str(runtime), str(home)], env
    ) as process:
        protocol = _Protocol(process, timeout)
        value = protocol.messages.get(timeout=timeout)
        if value is None:
            raise ValueError("DSH discovery failed")
    rows, auth_by_provider = [], {}
    for p in value["rows"]:
        models = p.get("models") or []
        if not models:
            continue
        kind = p.get("authKind") or ("account" if p.get("id") == "deepseek-account" else "api_key")
        credential_env = p.get("credentialEnv")
        if kind == "account":
            auth = _auth(
                "account",
                False,
                None,
                None,
                "Headless 模式不支持桌面账号登录；请改用 API Key 供应商",
            )
        elif not credential_env:
            auth = _auth("none", True, None, "runtime", "本地供应商无需密钥")
        else:
            source = _credential_source(credential_env, home / ".env", Path.cwd() / ".env")
            if source == "environment":
                message = f"已从环境变量读取 {credential_env}"
            elif source == "dotenv":
                message = f"已从 .env 读取 {credential_env}"
            else:
                message = f"缺少 {credential_env}，请配置密钥"
            auth = _auth("api_key", bool(source), credential_env, source, message)
        auth_by_provider[p["id"]] = auth
        for m in models:
            rows.append((p["id"], p["label"], _model(m["id"], m["label"], p["id"])))
    providers = _providers(rows)
    for provider in providers:
        provider["auth"] = auth_by_provider[provider["id"]]
    command = _vendor_command(
        "dsh", executable, runtime / "node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js"
    )
    if value["patches"]:
        # JSON is valid YAML. Copy only model-provider settings, never unrelated desktop tools.
        command += ["--patch", str(_snapshot(directory, "dsh-providers", value["patches"]))]
    return providers, {"adapter": "command", "command": command}


def discover_installed(directory, *, timeout_seconds=15):
    """Return fresh public catalogues and separately held private worker arguments.

    Each runtime has its own bounded deadline. Unavailable runtimes produce explicit
    status entries; neither cached fixtures nor guessed model lists are substituted.
    """
    directory = Path(directory).resolve()
    if not 0 < timeout_seconds <= 120:
        raise ValueError("Invalid discovery timeout")
    catalogue, runners = [], {}
    for ident, label, discover in (
        ("codex", "Codex", _discover_codex),
        ("opencode", "OpenCode", _discover_opencode),
        ("zcode", "ZCode", _discover_zcode),
        ("dsh", "DeepSeek Harness", _discover_dsh),
    ):
        entry = {
            "id": ident,
            "software": ident,
            "label": label,
            "providers": [],
            "installed": False,
            "status": "unavailable",
            "message": "未发现可用的本机运行时或已登录模型配置",
        }
        try:
            providers, runner = discover(directory, timeout_seconds)
            entry["installed"] = True
            if providers:
                entry.update(
                    providers=providers, status="available", message="已读取本机运行时模型列表"
                )
                runners[ident] = runner
            else:
                entry["message"] = "已安装，但未发现可用模型或登录配置"
        except FileNotFoundError:
            entry["message"] = "未找到已安装的运行时或必要配置"
        except (TimeoutError, queue.Empty):
            entry["installed"] = True
            entry["message"] = "本机运行时模型发现超时，请重试"
        except (
            OSError,
            ValueError,
            RuntimeError,
            KeyError,
            TypeError,
            StopIteration,
            subprocess.SubprocessError,
        ):
            # Runtime errors/logs can contain tokens and endpoints. Expose only a fixed message.
            entry["installed"] = True
            entry["message"] = "无法读取本机运行时配置或模型列表"
        catalogue.append(entry)
    return DiscoveryResult(catalogue, runners)
