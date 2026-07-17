# MCP Integration Design

> **Status:** Design complete; implementation not started.

## Protocol boundary

Phi uses the official Python MCP SDK for JSON-RPC framing, stdio process management, initialization,
and protocol calls. Reimplementing the protocol provides no Harness learning value and would add a
second transport implementation to maintain.

This differs from the Model gateway: LiteLLM already provides a server-side compatibility layer, so
Phi speaks OpenAI-compatible HTTP directly there.

## Configuration

Phi uses the common configuration shape:

```json
{
  "mcpServers": {
    "name": {
      "command": "...",
      "args": [],
      "env": {},
      "enabled": true
    }
  }
}
```

Configuration is loaded from:

- global `~/.phi/mcp.json`;
- project `.phi/mcp.json`.

Project servers override global servers with the same ID. The JSON files are the source of truth;
v1 does not add a database. Plain async load/save operations back both CLI management and startup.

## Transport and lifecycle

v1 supports stdio servers only. Bootstrap connects all enabled servers, initializes a client
session, and caches discovered tools plus supported resource and prompt metadata.

One failed server emits a diagnostic and is skipped without preventing other servers from loading.
The shared Event bus receives:

- `McpServerConnected(server_id, tool_count)`;
- `McpServerConnectFailed(server_id, error)`.

Every child process and client session has an explicit async shutdown path.

## Tools

Every discovered MCP Tool is directly registered in the common `ToolRegistry` as:

```text
mcp__{server_id}__{tool_name}
```

Double underscores preserve visible server/tool boundaries while remaining valid for
OpenAI-compatible tool-name character restrictions.

MCP tools use the remote `inputSchema` as `Tool.args_schema` and set `args_model=None`. Phi does not
perform a second local JSON Schema validation pass; server rejection becomes `ToolResult.error`.

MCP Tools are `approval_class="unconfined"`. They execute through an external process or service,
not through `ConfinedEnvironment`, so approval is the only Phi-controlled gate in v1.

## Resources and prompts

The three MCP primitives retain their intended control semantics:

| Primitive | Phi behavior |
| --- | --- |
| Tools | Registered directly for Model selection |
| Resources | Exposed through read-only `mcp_list_resources` and `mcp_read_resource` meta-tools |
| Prompts | Loaded through plain async functions and exposed as user-selected slash commands |

Prompts are not Model-callable tools because their purpose is user-selected instruction templates.
Generated slash-command names use `/mcp__{server_id}__{prompt_name}`.

## Required tests

- global/project config merge and persistence;
- stdio lifecycle, initialization, and cleanup;
- one failed server not blocking another;
- tool-name construction and collision behavior;
- raw schema registration and remote validation errors;
- resource listing and reads;
- prompt listing, retrieval, and user-only routing;
- Event emission;
- approval of unconfined tools and absence of false confinement claims.

Discovery meta-tools, remote transports, OAuth, and local JSON Schema validation are deferred; see
[`../deferred.md`](../deferred.md).
