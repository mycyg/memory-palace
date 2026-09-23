# Work reminders

Reminders are optional schedules for existing source-backed `reminder` or `commitment` records. MemoryPalace stores the due time, policy, revision, and callback status. A host decides who receives anything and performs the external effect. The default policy is disabled and requires confirmation.

Create a record first through `POST /v1/sources` or `POST /v1/memories`. Use its record ID when scheduling. This policy keeps due work as suggestions until an operator enables delivery:

```sh
eventmem api PUT /v1/reminders/policies --json '{"id":"work-review","scope":{"project":"demo"},"timezone":"UTC","triggers":["reminder","commitment"],"allowed_kinds":["reminder","commitment"]}'
```

Then schedule an existing record:

```sh
eventmem api POST /v1/reminders --json '{"command_id":"review-note-1","policy_id":"work-review","record_id":"REPLACE_WITH_RECORD_ID","due_at":"2030-09-09T10:00:00+00:00","trigger":"reminder"}'
```

Use a stable `command_id` for retries of the same request. Changing its payload under the same ID is a conflict. Times require a UTC offset. Supported recurrence values are `none`, `daily`, `weekly`, and `yearly`. Policy scope, allowed kind, trigger, quiet hours, rate limits, and confirmation are checked before delivery. `GET /v1/reminders/state/{table}` exposes schedules and outbox state; `POST /v1/reminders/{schedule_id}` changes a schedule with its expected revision.

A callback URL is configured in the policy. The worker sends only when the policy and schedule allow it. `sent` means the configured callback acknowledged the attempt; it does not prove a user read a message. A timeout after an unknown external effect is retained for reconciliation. Hosts should deduplicate by delivery ID. The Python `eventmem.sdk.DeliveryInbox` can commit a local SQLite effect and delivery ID in one transaction; an identical retry returns without repeating the effect, while a changed body under the same ID is rejected. External APIs must honor an idempotency key or provide their own reconciliation.

`examples/callback.py` is a local callback that records synthetic notifications to SQLite. It sends no network message to a recipient.

The TypeScript SDK's `acceptDelivery` uses the same rule. Its transactional store supplies `get(id)` returning the previously stored body (or `undefined`) and `put(id, body)`. The effect and receipt must commit together; reusing an ID with a different body is an error. A callback merely echoing an ID is not evidence that its external effects are deduplicated.
