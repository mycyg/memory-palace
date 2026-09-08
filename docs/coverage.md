# Functional coverage

SCARLETT entries below reflect the supplied architecture image. Its implementation and runnable benchmark adapter were not supplied. “Diagram” describes the reference evidence; it is not a verified implementation or performance result.

| Area | SCARLETT reference | MemoryPalace 1.0 |
|---|---|---|
| Collection | Chat JSON, Telegram, manual entry; diagram | Source/batch/upload/web snapshots; Claude Code and DSH event adapters; durable host spool |
| Receipt | Pending extraction and review; diagram | Stable source identity, content hashes, receipt transaction, separate processing stages |
| Validation | Structured extraction, reviewer; diagram | Typed proposals, exact quote checks, source authority, conflict proposals and controlled transitions |
| Write authority | Single push API; diagram | Unified Engine with SQLite transactions, CAS revisions and idempotent commands |
| Time | Conflict/existence/time judgment; diagram | Effective time, received time, historical cutoff, versions, superseded/current state |
| Correction | Correction endpoint and court; diagram | Immediate revision, dependent-account invalidation, history and rollback |
| Families | Topic families, triage, Leiden; diagram | Incremental Leiden candidates, explicit evidence/membership, publication/merge/split/rollback |
| Volumes | Narrative volumes and immutable revisions; diagram | Revisioned volume membership and lifecycle through the same organizer |
| Recall | Passive, dual retrieval, RRF and cooldown; diagram | Exact/FTS/vector/visual/graph fusion, current-state checks, configurable budgets and deduplication |
| Read tools | Family/volume/diary/image MCP; diagram | Bounded record/source/history/graph tools, HTTP attachments and clips, stdio/HTTP MCP |
| Startup | Wake bundle and checkpoint; diagram | Per-scenario startup budget, session checkpoint, explicit-read accounting, compact recovery |
| Events | Event log, diary and daily records; diagram | Parent/child episodes, decisions, procedures, source-linked generated diaries and summaries |
| Continuity | Ongoing state, portrait, notebook; diagram | Goals, confirmed/unverified progress, blockers, commitments, next entry and prefetched cues |
| Relationships | Cohesion and narrative identity; diagram | Scoped relationships, shared experiences, explicit emotional observations, cited narratives |
| Prospective memory | Notebook, ideas, calendar; diagram | Commitments, reminders, anniversary recurrence and separately labeled predictions |
| Active contact | Not established by supplied diagram | Persistent scheduler/outbox, per-role settings, quiet hours, confirmation, retry and uncertainty |
| Documents | Image attachments and diary sources; diagram | PDF/DOCX/PPTX/XLSX/MD/HTML/CSV, paragraph/page/table references and document versions |
| Audio/video | Not established by supplied diagram | FFmpeg decoding, ASR segments, visual keyframes, original attachments and bounded clips |
| Console | Observatory, admin/curate/graph/eval; diagram | Processing overview, virtualized browsing, correction, timeline/calendar, topics/2D/3D, knowledge/attachments, diary, recall lab, contact and maintenance |
| Feedback | Recall route logs and mistakes; diagram | Display/read/adoption/verification/correction/unknown events, ranking trace and model usage |
| Retention | Archive and revision history; diagram | Archive/restore, isolated import/backup recovery, export, dependent deletion and tombstones |
| Scale | Diagram counts only | 100k memories + 1m 1,024-dimensional vectors measured on the documented machine |
| Evaluation | Eval fixtures and audits; diagram | Temporal replay, actual pinned legacy adapter, declared retrieval baselines, optional answer/judge roles |

## Evaluation adapter

`eventmem.core.baselines.RecallAdapter` defines an external comparison contract. An adapter receives the same scoped, time-limited source snapshot, query and budget and returns record ids. A SCARLETT adapter must use a runnable implementation with documented settings. Until then, result files store `measurements: null` and no relative performance claim is made.
