"""Local operations, connector snippets, and a no-model-key executable demo."""

import argparse
import json
import os
import secrets
import sys
from pathlib import Path

from . import __version__
from .client import RelayFenceClient
from .store import Store


def init_directory(directory: Path) -> Path:
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    token_file = directory / "access.token"
    try:
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(secrets.token_urlsafe(32) + "\n")
    Store(directory / "ledger.sqlite3")
    return directory


def read_token(path: Path) -> str:
    token = os.environ.get("RELAYFENCE_TOKEN")
    if token is None:
        try:
            token = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise ValueError(
                "Access token missing. Run 'relayfence init' or set RELAYFENCE_TOKEN"
            ) from exc
    if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("Access token must be at least 32 ASCII characters without whitespace")
    return token


def connector_config(client: str, url: str, token_file: Path) -> str:
    from .client import validate_url

    validate_url(url)
    command = str(Path(sys.executable).resolve())
    args = ["-m", "relayfence", "bridge", "--url", url, "--token-file", str(token_file.resolve())]
    if client == "codex":
        return (
            "[mcp_servers.relayfence]\ncommand = "
            + json.dumps(command)
            + "\nargs = "
            + json.dumps(args)
            + "\n"
        )
    if client == "zcode":
        return json.dumps(
            {"mcp": {"servers": {"relayfence": {"command": command, "args": args}}}}, indent=2
        )
    return "\n".join(
        [
            "- id: mcp-relayfence",
            "  name: '@deepseek-ai/dsh-mcp-client'",
            "  config:",
            "    serverName: relayfence",
            "    transport: stdio",
            "    command: " + json.dumps(command),
            "    args: " + json.dumps(args),
            "",
        ]
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="relayfence", description="Shared coordination for agents across tools"
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create a local ledger and access token (never overwrites)")
    init.add_argument("--directory", type=Path, default=Path(".relayfence"))
    serve = sub.add_parser("serve", help="Serve MCP, HTTP and the read-only console on loopback")
    serve.add_argument("--directory", type=Path, default=Path(".relayfence"))
    serve.add_argument("--host", choices=["127.0.0.1", "::1"], default="127.0.0.1")
    serve.add_argument("--port", type=int, default=7433)
    serve.add_argument("--token-file", type=Path)
    bridge = sub.add_parser(
        "bridge", help="Expose stdio MCP tools backed by an existing shared HTTP service"
    )
    bridge.add_argument("--url", default="http://127.0.0.1:7433")
    bridge.add_argument("--token-file", type=Path, default=Path(".relayfence/access.token"))
    config = sub.add_parser(
        "config", help="Print a client config snippet; does not modify client settings"
    )
    config.add_argument("client", choices=["codex", "zcode", "dsh"])
    config.add_argument("--url", default="http://127.0.0.1:7433")
    config.add_argument("--token-file", type=Path, default=Path(".relayfence/access.token"))
    call = sub.add_parser(
        "call", help="Call the public HTTP API with a JSON payload file, or '-' for stdin"
    )
    call.add_argument("operation")
    call.add_argument("payload")
    call.add_argument("--url", default="http://127.0.0.1:7433")
    call.add_argument("--token-file", type=Path, default=Path(".relayfence/access.token"))
    backup = sub.add_parser(
        "backup", help="Create an online consistent backup; destination must be new"
    )
    backup.add_argument("destination", type=Path)
    backup.add_argument("--directory", type=Path, default=Path(".relayfence"))
    demo = sub.add_parser(
        "demo", help="Run real MCP clients through races, stale inputs, takeover and restart"
    )
    demo.add_argument(
        "--directory",
        type=Path,
        help="New directory for demo data; default: a unique .relayfence/demo-* directory",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            path = init_directory(args.directory)
            print(
                f'Ledger initialized: {path}\nAccess token file: {path / "access.token"}\nStart: relayfence serve --directory "{path}"'
            )
        elif args.command == "serve":
            import uvicorn

            from .server import create_app

            if not 1 <= args.port <= 65535:
                raise ValueError("Port must be between 1 and 65535")
            token = read_token(args.token_file or args.directory / "access.token")
            store = Store(args.directory / "ledger.sqlite3")
            app = create_app(store, token)
            uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False)
        elif args.command == "bridge":
            from .mcp_server import make_mcp

            with RelayFenceClient(args.url, read_token(args.token_file)) as client:
                make_mcp(client.call_raw).run(transport="stdio")
        elif args.command == "config":
            print(connector_config(args.client, args.url, args.token_file))
        elif args.command == "call":
            raw = (
                sys.stdin.read()
                if args.payload == "-"
                else Path(args.payload).read_text(encoding="utf-8")
            )
            with RelayFenceClient(args.url, read_token(args.token_file)) as client:
                result, _ = client.call_raw(args.operation, json.loads(raw))
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if result["ok"] else 1
        elif args.command == "backup":
            database = args.directory / "ledger.sqlite3"
            if not database.is_file():
                raise ValueError("Ledger does not exist; check --directory")
            Store(database).backup(args.destination)
            print(f"Backup created: {args.destination.resolve()}")
        elif args.command == "demo":
            from .demo import run_demo

            run_demo(args.directory)
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"relayfence: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
