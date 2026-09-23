# MemoryPalace engineering guide

MemoryPalace is a general-purpose library for work collaboration memory. The Python distribution and CLI are named `eventmem`. Version 2.0 is a breaking release. SQLite is the sole authoritative runtime store; indexes and caches are derived from it. Hosts own conversations, task execution, recipients, and delivery channels. MemoryPalace records sources, revisions, events, summaries, checkpoints, commitments, procedures, and bounded recall. Optional integrations provide media parsing, vectors, graphs, reminders, and callbacks.

Keep the public core independent of application-specific character configuration, affect, romance, proactive chat, exploration executors, and phone operations. A host may use generic APIs for its own application, but private host behavior does not belong in this package. Plugins call the service and may spool observations while offline; they do not maintain a second memory engine.

## Design and implementation

- 非必要勿增实体。Prefer existing source, record, revision, session, job, and schedule flows before creating a new table or object family.
- Reuse a real flow. Add an abstraction only after actual duplication makes the common behavior clear.
- Keep each module and function responsible for one clear task. Use direct names and explicit inputs; keep invariants near the write path.
- Preserve source identity, revision checks, scope boundaries, and honest evidence labels. An inferred statement must not silently become a confirmed fact.
- Validate in proportion to risk. Run focused checks for a local change; run package, client, and end-to-end checks when their boundary changes. Do not optimize for line count or test count.
- Prefer a simple, elegant implementation that can be read and repaired over compatibility scaffolding for retired 1.x behavior.

`docs/architecture.md` is the current architecture. `DESIGN.md`, `SPEC.md`, and documents marked historical describe earlier versions and are not implementation contracts. Keep public examples synthetic, runnable, and free of local credentials or personal data. Do not publish measured performance claims without a reproducible report for the exact version.
