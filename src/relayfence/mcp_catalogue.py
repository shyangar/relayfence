"""Bounded local MCP inventory. Public metadata never contains launch secrets.

Discovery does not start servers or authenticate. Explicit selection controls
configured servers only; plugin and managed tools remain runtime-owned.
"""

import copy
import hashlib
import itertools
import json
import os
import re
import tomllib
from pathlib import Path

import json5
import yaml

from .errors import CoordinationError
from .skill_catalogue import SOFTWARE_LABELS, _home, _opencode_config_home

LIMIT = 1_048_576
NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_./:@-]{0,127}$")


def read_config(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return {}, None
    try:
        with path.open("rb") as stream:
            raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT:
            return {}, "config_too_large"
        text = raw.decode("utf-8-sig")
        if path.suffix == ".toml":
            value = tomllib.loads(text)
        elif path.suffix in {".yml", ".yaml"}:
            value = yaml.safe_load(text)
        else:
            value = json5.loads(text)
        return value, None
    except (OSError, ValueError, UnicodeError, yaml.YAMLError, RecursionError):
        return {}, "config_unreadable"


def config_paths(software, workspace=None):
    home = _home(software.upper() + "_HOME", "." + software)
    if software == "codex":
        paths = [(home / "config.toml", "user")]
        if workspace:
            paths.extend(
                (p / ".codex/config.toml", "project")
                for p in reversed([Path(workspace), *Path(workspace).parents])
            )
    elif software == "opencode":
        home = _opencode_config_home()
        paths = [(home / name, "user") for name in ("opencode.json", "opencode.jsonc")]
        if os.environ.get("OPENCODE_CONFIG"):
            paths.append((Path(os.environ["OPENCODE_CONFIG"]), "user"))
        if workspace:
            paths.extend(
                (p / name, "project")
                for p in reversed([Path(workspace), *Path(workspace).parents])
                for name in (
                    "opencode.json",
                    "opencode.jsonc",
                    ".opencode/opencode.json",
                    ".opencode/opencode.jsonc",
                )
            )
    elif software == "zcode":
        paths = [
            (home / name, "user")
            for name in ("mcp.json", ".mcp.json", "cli/.mcp.json", "cli/settings.json")
        ]
        if workspace:
            paths.append((Path(workspace) / ".mcp.json", "project"))
    else:
        paths = [
            (p, "user")
            for p in itertools.islice((home / "profiles").glob("*/cordis.patch.yml"), 32)
        ]
    return paths


def configured_servers(software, workspace=None):
    """Private records; callers must project with inventory(), never serialize."""
    records, warnings = {}, set()

    def merge(value, origin, source):
        if software == "dsh" and isinstance(value, list):
            for item in value[:256]:
                if not isinstance(item, dict) or "mcp" not in str(item.get("id", "")).lower():
                    continue
                config = item.get("config") or {}
                if isinstance(config, dict):
                    name = config.get("serverName", item.get("id"))
                    if isinstance(name, str) and NAME.fullmatch(name):
                        records[name] = {
                            "config": config,
                            "origin": origin,
                            "source": source,
                            "v2": False,
                        }
            return
        if not isinstance(value, dict):
            return
        servers = (
            value.get("mcp_servers")
            if software == "codex"
            else value.get("mcpServers", value.get("mcp", {}))
        )
        v2 = isinstance(servers, dict) and isinstance(servers.get("servers"), dict)
        if v2:
            servers = servers["servers"]
        if not isinstance(servers, dict):
            return
        if len(servers) > 256:
            warnings.add("catalogue_truncated")
        for name, config in list(servers.items())[:256]:
            if (
                not isinstance(name, str)
                or not NAME.fullmatch(name)
                or not isinstance(config, dict)
            ):
                continue
            if not any(config.get(k) for k in ("command", "url", "transport")):
                continue
            records[name] = {"config": config, "origin": origin, "source": source, "v2": v2}

    for path, origin in config_paths(software, workspace):
        value, warning = read_config(path)
        if warning:
            warnings.add(warning)
        merge(value, origin, str(path))
    if software == "opencode" and os.environ.get("OPENCODE_CONFIG_CONTENT"):
        try:
            raw = os.environ["OPENCODE_CONFIG_CONTENT"]
            if len(raw) > LIMIT:
                raise ValueError()
            merge(json5.loads(raw), "environment", "OPENCODE_CONFIG_CONTENT")
        except (ValueError, RecursionError):
            warnings.add("inline_config_unreadable")
    return records, sorted(warnings)


def inventory(software, workspace=None):
    records, warnings = configured_servers(software, workspace)
    supported = software in {"codex", "opencode"}
    servers = []
    for name, record in records.items():
        config = record["config"]
        servers.append(
            {
                "id": name,
                "name": name,
                "origin": record["origin"],
                "enabled": config.get("enabled", True) is not False
                and config.get("disabled") is not True,
                "transport": "http" if config.get("url") else "stdio",
                "selectable": supported,
                "connection": "not_probed",
            }
        )
    if software in {"codex", "zcode"}:
        root = _home(software.upper() + "_HOME", "." + software)
        if software == "zcode":
            root /= "cli"
        for path in itertools.islice((root / "plugins/cache").glob("*/*/*/.mcp.json"), 64):
            value, warning = read_config(path)
            if warning:
                warnings.append(warning)
            entries = value.get("mcpServers", {}) if isinstance(value, dict) else {}
            if isinstance(entries, dict):
                for name in list(entries)[:32]:
                    servers.append(
                        {
                            "id": "plugin-"
                            + hashlib.sha256((str(path) + str(name)).encode()).hexdigest()[:24],
                            "name": str(name)[:128],
                            "origin": "plugin",
                            "enabled": None,
                            "transport": "runtime",
                            "selectable": False,
                            "connection": "not_probed",
                        }
                    )
    return {
        "software": software,
        "label": SOFTWARE_LABELS[software],
        "servers": servers,
        "selection_supported": supported,
        "warnings": sorted(set(warnings)),
        "scope": "configured_servers_only",
    }


def discover_mcp(workspace=None):
    return [inventory(software, workspace) for software in SOFTWARE_LABELS]


def validate_mcp_selection(software, names, workspace=None):
    if names is None:
        return
    if software not in {"codex", "opencode"}:
        raise CoordinationError(
            "mcp_selection_unsupported", "该软件暂只支持沿用自身 MCP 配置", status=422
        )
    if (
        len(names) > 32
        or len(names) != len(set(names))
        or any(not isinstance(n, str) or not NAME.fullmatch(n) for n in names)
    ):
        raise CoordinationError("invalid_mcp_selection", "MCP 选择无效", status=422)
    records, warnings = configured_servers(software, workspace)
    if warnings:
        raise CoordinationError(
            "mcp_config_unreadable", "MCP 配置读取不完整，请修复后重新扫描", status=422
        )
    if set(names) - records.keys():
        raise CoordinationError(
            "mcp_not_found", "所选 MCP 已不在当前软件的配置中，请重新扫描", status=422
        )


def codex_overrides(names, workspace=None):
    if names is None:
        return []
    validate_mcp_selection("codex", names, workspace)
    records, _ = configured_servers("codex", workspace)
    return [
        f"mcp_servers.{json.dumps(name)}.enabled={str(name in names).lower()}" for name in records
    ]


def opencode_environment(names, env, workspace=None):
    if names is None:
        return env
    validate_mcp_selection("opencode", names, workspace)
    records, _ = configured_servers("opencode", workspace)
    config = json5.loads(env.get("OPENCODE_CONFIG_CONTENT", "{}"))
    config = copy.deepcopy(config)
    v2 = any(record["v2"] for record in records.values())
    servers = {}
    for name, record in records.items():
        # V2 replaces a server object: keep its full private connection definition.
        server = copy.deepcopy(record["config"])
        if v2:
            server.pop("enabled", None)
            server["disabled"] = name not in names
        else:
            server["enabled"] = name in names
        servers[name] = server
    if servers:
        if v2:
            config.setdefault("mcp", {})["servers"] = servers
        else:
            config["mcp"] = servers
    return {**env, "OPENCODE_CONFIG_CONTENT": json.dumps(config)}
