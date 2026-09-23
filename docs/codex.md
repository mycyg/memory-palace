# Codex integration

MemoryPalace can receive Codex observations and provide scoped context through its service and MCP server. The host remains responsible for tool execution, session handling, and choosing when to use returned context.

Start the service with a private root:

```sh
eventmem serve --root /private/work-memory
```

Install the project integration with the same root, then restart Codex and review the generated hook definitions in Codex:

```sh
eventmem codex install --project /path/to/project --root /private/work-memory
```

The installer selects a project scope by default. A shared root can serve more than one project while their scopes stay separate. Use `--scope` when several hosts intentionally share a work collection. The integration records user messages, final assistant replies, and tool observations with distinct authority labels. It can return bounded context at lifecycle boundaries. A local spool retains observations while the service is unavailable and replays them with stable identities.

For explicit tools, add an MCP server using the same root:

```toml
[mcp_servers.memorypalace]
command = "/absolute/path/to/eventmem"
args = ["mcp", "--root", "/private/work-memory"]
```

MCP offers source receipt, recall, bounded reads, corrections, history, feedback, topic browsing, maintenance, and status. MCP alone does not observe every host message. The HTTP service and stdio MCP access the same SQLite store; avoid configuring different roots by mistake.

A hook receipt records an observation or returns context. It does not prove that Codex executed a plan. Offline spool acceptance is not a service receipt until replay succeeds. To remove the generated project integration, run:

```sh
eventmem codex uninstall --project /path/to/project
```

Removing the integration leaves the private memory root intact.
