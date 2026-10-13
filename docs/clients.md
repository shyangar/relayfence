# Connect Codex, ZCode and DSH

Updated 2026-10-10. Here, **ZCode** means Zhipu's ZCode and **DSH** means DeepSeek Harness.

The coordination boundary is the service URL plus the project name, not the vendor or model. Two Codex instances can coordinate; a Codex instance and a ZCode instance can coordinate through the same API as well.

## Start one service first

From the RelayFence checkout:

~~~bash
uv sync --locked
uv run relayfence init
uv run relayfence serve
~~~

Keep this running while clients connect. The service's access token is in the local `.relayfence/access.token` file. This is a RelayFence credential, not a model API key.

The quickest reproducible setup uses the included stdio bridge. Generate a snippet from a second terminal:

~~~bash
uv run relayfence config codex
uv run relayfence config zcode
uv run relayfence config dsh
~~~

The bridge command contains the absolute Python executable, `-m relayfence bridge`, the shared URL, and the absolute token file path. It forwards to the shared service. Do not replace it with independent database-backed servers in each client.

Use `--url` and `--token-file` when the service/token location differs. Generated snippets depend on the current virtual environment's path; regenerate after moving the checkout or replacing its environment. The generator prints configuration only.

## Codex

Merge the generated TOML section into your user configuration or trusted project's `.codex/config.toml`. Preserve other sections.

For direct Streamable HTTP instead, export `RELAYFENCE_TOKEN` from the token file in the environment used to start Codex, then run:

~~~bash
codex mcp add relayfence --url http://127.0.0.1:7433/mcp --bearer-token-env-var RELAYFENCE_TOKEN
~~~

PowerShell example for the current terminal:

~~~powershell
$env:RELAYFENCE_TOKEN = (Get-Content -Raw .relayfence/access.token).Trim()
~~~

An already-running desktop process may not inherit a terminal's updated environment; the generated stdio configuration with a token file avoids that requirement.

Source: [official Codex MCP documentation](https://developers.openai.com/codex/mcp). The installed CLI's `mcp add/get --help` was also checked.

## ZCode

Merge the generated JSON into the chosen scope:

- Workspace: `<workspace>/.zcode/config.json`, key `mcp.servers`.
- User: `~/.zcode/cli/config.json`, key `mcp.servers`.

For direct HTTP, open Settings → MCP Servers → New MCP Server, choose HTTP, enter `http://127.0.0.1:7433/mcp`, then add an `Authorization` header containing `Bearer <your RelayFence token>`. Choose the intended user/workspace scope.

If you use the generic `.agents/mcp.json` fallback, note that ZCode's native configuration takes precedence in the same scope. Keep credentials out of committed configuration.

Source: [official ZCode MCP services documentation](https://zcode.z.ai/en/docs/mcp-services).

## DeepSeek Harness

Add the generated YAML entry to the MCP client plugin entries in the profile you run. The entry uses the official `@deepseek-ai/dsh-mcp-client` package and `transport: stdio`.

When adding a new plugin to an existing `cordis.patch.yml`, wrap the generated entry in an `insert` operation (verified with desktop 0.2.0-rc.2):

~~~yaml
- insert:
    - id: mcp-relayfence
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: relayfence
        transport: stdio
        command: /absolute/path/to/python
        args: ['-m', relayfence, bridge, '--url', 'http://127.0.0.1:7433', '--token-file', '/private/instance.token']
~~~

For direct HTTP, the equivalent entry is:

~~~yaml
- id: mcp-relayfence
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: relayfence
    transport: streamable-http
    url: http://127.0.0.1:7433/mcp
    headers:
      Authorization: !!js '"Bearer " + process.env.RELAYFENCE_TOKEN'
~~~

Set `RELAYFENCE_TOKEN` in the harness environment first. Enable the plugin entry in the profile you actually start; merely keeping a YAML file beside a project does not activate it.

Source: [official DSH MCP client package](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md).

## Verify the connection

Ask each client to list RelayFence tools, then call `items_list` with `{"request":{"project":"shop"}}`. The two clients should see the same project.

Next, create one task and ask both clients to claim its ID using distinct actor/request IDs. Exactly one live claim should succeed. Feed each client [the cooperation instructions](../examples/agent-instructions.md), choosing a project and actor name explicitly.

The service exposes tools; it cannot force a model to call them, infer what files it read, or automatically wake another application. Event consumers poll `events_list`. There is no push subscription. The optional v0.3 local dispatcher polls and executes configured command-mode agents; it does not wake existing desktop chats.

For role-assigned work, register an instance, issue its own credential and generate its connector with `--token-file <instance.token>`. Its actor must match its instance ID. Set the required model in the external client before acknowledging the role selection in `task_claim`. The coordination MCP tools do not switch host models. See [roles and models](roles-and-models.md) for console setup and the separate local command adapter.

## Verification matrix

| Connection | Verification included in this release |
| --- | --- |
| MCP Streamable HTTP, modern negotiation | Real server process and official Python SDK client; tool discovery, success, conflict and freshness tested |
| MCP Streamable HTTP, legacy initialization | Same server, official SDK `mode="legacy"`; discovery and calls tested |
| MCP stdio bridge → HTTP ledger | Actual stdio subprocess plus an independent HTTP client; shared ownership conflict tested |
| Python HTTP client | Authentication, structured errors, retry receipts and concurrency tested |
| Codex | Generated TOML parsed and installed CLI options checked; live Codex CLI 0.130.0 with native MCP completed a specification task; desktop UI control was not tested |
| ZCode | Workspace configuration checked in the installed application; no usable model session was available, so ZCode generation remains unverified |
| DSH | DeepSeek Harness desktop 0.2.0-rc.2 read a Codex artifact and published 10 cases through MCP; wrong-role/model claims rejected; malformed model output recovered by reusing the live lease |

Protocol compatibility is useful evidence, but it does not establish model behavior or vendor end-to-end certification. Submit a reproducible issue with client version, transport, redacted configuration and exact error to extend this matrix. Never attach access tokens or lease tokens.
