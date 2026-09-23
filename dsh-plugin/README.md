# MemoryPalace DeepSeek Harness adapter

This plugin sends work observations to the local MemoryPalace service and injects scoped context returned by that service. It uses one service-owned retrieval and maintenance path. Events that cannot be delivered remain in `EVENTMEM_HOME/host-spool` for replay.

Build it with `npm ci && npm run build` in this directory. Configure the Harness bundle with `cordis.patch.yml`, then set `EVENTMEM_HOME` to the same private service root and `EVENTMEM_URL` to its loopback URL. The service token is read from `EVENTMEM_HOME/local-token`.

The available config fields are `enabled`, `injectWorkingSet`, and `writeFeed`, all enabled by default. The plugin captures user and assistant messages, tool calls and results, todo updates, session boundaries, and session end. It never sends user messages to a phone or external channel.

Run `npm run typecheck`, `npm test`, and `npm run build` after changes. The adapter uses the service's HTTP contract; it has no separate local memory database or ranking path.
