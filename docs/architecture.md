# MemoryPalace 2.0 architecture

MemoryPalace is a work memory library behind one local Python service. Its CLI, HTTP and MCP interfaces, SDKs, and host plugins use the same source, revision, and retrieval rules. SQLite is the sole authoritative runtime store. Full-text indexes, optional vector indexes, graph layouts, and context caches are derived data that can be rebuilt.

```text
Host events / files / API calls
             ↓
    immutable source snapshot
             ↓
 SQLite records + revisions + evidence links
             ↓
 jobs, summaries, sessions, checkpoints
             ↓
 scope + time + budget checked recall
             ↓
       host reads context
```

The host owns its conversation, tools, task execution, and delivery channel. MemoryPalace does not run an agent.

## Sources, records, and time

A source has a stable `(namespace, key, version, scope)` identity, a content hash, a durable snapshot, an event time, and a receipt time. Retrying the same source is idempotent; changing content under an existing identity is a conflict. A scope separates projects and collections. The optional `persona` dimension is a generic agent namespace and does not activate character behavior.

Sources can create records such as observations, episodes, facts, procedures, commitments, reminders, knowledge, summaries, and checkpoints. Evidence links connect a record to its sources. Explicit statements, observed operations, documents, and model output retain distinct authority labels. Generated interpretations remain marked as generated.

Record changes append revisions and require the expected revision plus a stable command ID. Correction, replacement, confirmation, retraction, refutation, archive, restore, and rollback have distinct effects. Current reads check the effective state. Historical reads can use both event time and the time information became known, so a later correction is not silently inserted into an earlier answer.

## Work continuity

A session boundary can record a checkpoint with goals, confirmed progress, unverified results, and a next entry point. Checkpoints, commitments, and procedures are ordinary source-linked records; there is no separate task executor. Summaries and topic families organize existing evidence. The organizer can propose candidate groups, while publication and revision remain explicit operations.

A host may send message and tool observations through a service plugin. The plugin keeps a bounded local spool during service interruption and retries receipt with stable identities. Replayed observations are records of what the host saw; they do not by themselves consume the live context budget or prove the host completed a task.

Automatic event grouping works on a bounded set of changed, source-backed records and reuses the existing family and member structures. Summaries split evidence into input batches of at most 6,000 tokens and reject output above 2,000 tokens. A family summary is refreshed when member revisions or the configured summary model change; the original sources remain readable.

## Processing and retrieval

Receipt commits the source before any optional model work begins. Persistent jobs track extraction, parsing, indexing, and summarization. Missing model configuration leaves dependent work visible for later processing; it does not erase the source. Revisions and deletions invalidate derived entries and context caches.

Recall combines exact cues and SQLite full-text search with any configured vector and relation candidates. Every returned record is checked against scope, revision, status, time, and evidence policy after candidate generation. A token budget bounds assembled context; `fast` favors bounded latency and `deep` can examine a wider lexical set. The API also provides bounded reads of records, source snapshots, revisions, attachments, and processing state.

Session recall prepares a context delivery ID and body hash. Preparation does not consume the session's context budget. The host reports `sending`, `unconfirmed`, or `accepted` to `POST /v1/context/receipts`; only acceptance of the exact body updates the budget and seen-record ledger. A compaction boundary starts a new context window. A session boundary can also report `foreground_seconds` (up to one hour) so background jobs yield while foreground work is active.

## Optional facilities

Document, image, audio, and video processing may require parser packages or configured model endpoints. Vector storage and graph clustering are optional extras. A reminder schedule references an existing record and follows a policy. A due reminder can produce a suggestion or call a configured host callback; the host selects the recipient and performs any external effect. Callback delivery IDs and acknowledgment state support reconciliation.

The local service binds to loopback by default and uses a token stored under its root. Plugins and SDKs call that service; MCP stdio can use the same local root. Backups, exports, and migrations are explicit operations. See [operations](operations.md) for installation, data migration, and release checks.
