# Codex

MemoryPalace supports Codex's native command hooks and MCP. Hooks receive user prompts, final assistant replies and tool results, and return scoped memories at session start, prompt submission and tool boundaries. MCP supplies explicit reads, writes, corrections and provenance lookup. Both use the same private database.

Use a Codex release with the eight events listed below. The integration was checked with Codex CLI 0.153.4. See the [official Codex hook reference](https://learn.chatgpt.com/docs/hooks).

## Install

Install the checkout as described in the [operations guide](operations.md), then start the service:

```sh
uv run eventmem serve --root /private/memorypalace
```

In another terminal, install project hooks:

```sh
uv run eventmem codex install --project /path/to/project --root /private/memorypalace
```

The installer merges `.codex/hooks.json`, preserves unrelated handlers and metadata, and updates its own handlers without duplication. Changed files receive a `.json.memorypalace-backup` copy. It records the installation's Python executable; reinstall after moving that environment. `--user` targets the effective Codex user configuration directory. Choose one level per project to avoid duplicate handlers.

Restart Codex in a trusted project. Open `/hooks`, inspect the MemoryPalace definitions and trust them. New or changed definitions are skipped until reviewed. The installer does not bypass hook trust or alter tool approval settings.

## MCP

Add this table to the project's `.codex/config.toml`, using the same private root:

```toml
[mcp_servers.memorypalace]
command = "/absolute/path/to/memory-palace/.venv/bin/eventmem"
args = ["mcp", "--root", "/private/memorypalace"]
```

Restart Codex to load the server. The stdio server opens the shared database; the HTTP service handles hooks and background jobs. The `memorypalace` server name lets hooks exclude memory-tool output from new observations. Set `EVENTMEM_MCP_SERVER` if using another name.

## Shared companion memory and WeChat

Default scopes isolate each absolute working directory. Desktop Codex and an ACP client such as `wechat-acp` can share companion memory by installing into each agent's actual working directory with the same root and scope:

```sh
uv run eventmem codex install \
  --project /path/to/agent-working-directory \
  --root /private/memorypalace \
  --scenario companion \
  --scope '{"project":"personal","persona":"companion","collection":"default","world":"real"}'
```

Use that scope in MCP calls too. The ACP host must run a compatible Codex executable and load reviewed project hooks. Restart its agent after installation. MemoryPalace stores no WeChat credentials or transport implementation. Active-session continuity remains the host's responsibility; the database persists across sessions and transports.

## Lifecycle

| Event | MemoryPalace behavior |
|---|---|
| SessionStart | Restore scoped memory; `source: compact` resets context accounting |
| UserPromptSubmit | Recall before recording the prompt, avoiding self-echo |
| PreToolUse / PostToolUse | Recall relevant context; record completed tool observations |
| Stop | Record the final reply as a model source; return JSON without extending the turn |
| PreCompact / Interrupt | Record a receipt boundary without consuming future context budget |
| SessionEnd | Finalize the boundary within Codex's short exit-hook timeout |

User statements remain explicit sources; model replies remain inferred sources. Hooks use stable lifecycle fields, exclude memory MCP results, and do not parse rollout files or ingest developer instructions. They do not reconstruct older conversations. Incomplete assistant text that never reaches Stop is not guaranteed to be captured.

Each request is spooled locally before transmission. The worker replays offline observations idempotently without spending live context budget. Hooks fail open. There are no inline model requests; model-free storage and recall remain available while extraction awaits model configuration. Keep the service running for background processing.

Remove only MemoryPalace's hooks with:

```sh
uv run eventmem codex uninstall --project /path/to/project
```

Remove the MCP table separately if no longer needed. The private database is preserved.
