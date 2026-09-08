from __future__ import annotations

import calendar
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from .db import Conflict, Missing, digest, dumps
from .models import ContactPolicy, ScheduleInput, Scope, now


class Scheduler:
    def __init__(self, engine):
        self.engine = engine
        self.last_automatic = float("-inf")

    def policy(self, policy: ContactPolicy):
        with self.engine.db.connect(write=True) as conn:
            conn.execute(
                "INSERT INTO policies VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (policy.id, policy.model_dump_json()),
            )
        return policy.model_dump()

    def schedule(self, request: ScheduleInput):
        with self.engine.db.connect(write=True) as conn:

            def run():
                record = self.engine._get(conn, request.record_id)
                row = conn.execute(
                    "SELECT data FROM policies WHERE id=?", (request.policy_id,)
                ).fetchone()
                if row:
                    policy = ContactPolicy.model_validate_json(row[0])
                    if policy.scope.model_dump() != record["scope"]:
                        raise Conflict("Contact policy and memory scopes differ")
                else:
                    policy = ContactPolicy(
                        id=request.policy_id, scope=Scope(**record["scope"])
                    )
                    conn.execute(
                        "INSERT INTO policies VALUES(?,?)",
                        (policy.id, policy.model_dump_json()),
                    )
                sid = "schedule_" + digest(request.command_id)[:32]
                data = request.model_dump() | {"record_revision": record["revision"]}
                conn.execute(
                    "INSERT INTO schedules VALUES(?,?,?,?,?,?,?)",
                    (
                        sid,
                        request.policy_id,
                        request.record_id,
                        request.due_at,
                        "scheduled",
                        1,
                        dumps(data),
                    ),
                )
                return {
                    "id": sid,
                    "revision": 1,
                    "status": "scheduled",
                    "source_ids": record["source_ids"],
                }

            return self.engine.command(
                conn, "schedule:" + request.command_id, request.model_dump(), run
            )

    def control(self, sid, action, expected_revision, due_at=None):
        from .models import utc

        if action not in {"cancel", "pause", "resume", "snooze", "confirm"}:
            raise ValueError("Unknown schedule action")
        with self.engine.db.connect(write=True) as conn:
            row = conn.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
            if not row:
                raise Missing(sid)
            if row["revision"] != expected_revision:
                raise Conflict("Schedule revision changed")
            data = json.loads(row["data"])
            if action == "confirm":
                data["confirmed"] = True
                conn.execute(
                    "UPDATE outbox SET state='ready' WHERE schedule_id=? AND state='suggested'",
                    (sid,),
                )
                state = row["state"]
            else:
                state = {
                    "cancel": "canceled",
                    "pause": "paused",
                    "resume": "scheduled",
                    "snooze": "scheduled",
                }[action]
                conn.execute(
                    "UPDATE outbox SET state='canceled' WHERE schedule_id=? AND state IN ('suggested','ready','retry')",
                    (sid,),
                )
            if action in {"resume", "snooze"}:
                data["generation"] = data.get("generation", 0) + 1
                data.pop("confirmed", None)
            if action == "snooze" and not due_at:
                raise ValueError("Snooze requires due_at")
            due = utc(due_at) if due_at else row["due_at"]
            conn.execute(
                "UPDATE schedules SET state=?,revision=revision+1,due_at=?,data=? WHERE id=?",
                (state, due, dumps(data), sid),
            )
            return {"id": sid, "revision": expected_revision + 1, "status": state}

    def _eligible(self, conn, schedule, policy, stamp):
        if schedule["state"] in ("canceled", "paused", "complete"):
            return False, "schedule_inactive"
        try:
            record = self.engine._get(conn, schedule["record_id"])
        except Missing:
            return False, "memory_deleted"
        if (
            record["scope"] != policy.scope.model_dump()
            or record["status"] != "active"
            or record["attributes"].get("completed")
            or record["attributes"].get("canceled")
        ):
            return False, "memory_inactive"
        if record["valid_until"] and record["valid_until"] <= stamp:
            return False, "memory_expired"
        data = json.loads(schedule["data"])
        if (
            data["trigger"] not in policy.triggers
            or record["kind"] not in policy.allowed_kinds
        ):
            return False, "outside_policy"
        return record, None

    def automatic_greetings(self, stamp):
        # Only a user-enabled greeting policy creates autonomous suggestions.
        # Daily source and command identities survive restart and cancellation.
        if time.monotonic() - self.last_automatic < 60:
            return
        self.last_automatic = time.monotonic()
        with self.engine.db.connect() as conn:
            policies = [
                ContactPolicy.model_validate_json(r[0])
                for r in conn.execute("SELECT data FROM policies")
            ]
        from .models import SourceInput

        for policy in policies:
            if (
                not policy.enabled
                or "greeting" not in policy.triggers
                or "reminder" not in policy.allowed_kinds
            ):
                continue
            local = datetime.fromisoformat(stamp).astimezone(ZoneInfo(policy.timezone))
            key = f"greeting:{policy.id}:{local.date().isoformat()}"
            with self.engine.db.connect() as conn:
                if conn.execute(
                    "SELECT 1 FROM commands WHERE id=?", ("schedule:" + key,)
                ).fetchone():
                    continue
            source = self.engine.receive(
                SourceInput(
                    namespace="scheduled-greeting",
                    key=key,
                    scope=policy.scope,
                    kind="reminder",
                    title="主动问候",
                    text=policy.greeting_text,
                    authority="model",
                    metadata={"trigger": "greeting", "policy_id": policy.id},
                )
            )
            rid = self.engine.source(source["id"])["record_ids"][0]
            due = local.replace(
                hour=policy.quiet_end, minute=0, second=0, microsecond=0
            )
            self.schedule(
                ScheduleInput(
                    command_id=key,
                    policy_id=policy.id,
                    record_id=rid,
                    due_at=due.isoformat(),
                    trigger="greeting",
                )
            )

    def tick(self, *, deliver=True, stamp=None):
        stamp = stamp or now()
        self.automatic_greetings(stamp)
        created = []
        with self.engine.db.connect(write=True) as conn:
            # A process dying during an unacknowledged send is explicitly uncertain.
            stale = conn.execute(
                "SELECT o.*,s.policy_id FROM outbox o JOIN schedules s ON s.id=o.schedule_id WHERE o.state='sending' AND o.lease_until<?",
                (time.time(),),
            ).fetchall()
            for row in stale:
                p = conn.execute(
                    "SELECT data FROM policies WHERE id=?", (row["policy_id"],)
                ).fetchone()
                retry = p and ContactPolicy.model_validate_json(p[0]).idempotent_channel
                conn.execute(
                    "UPDATE outbox SET state=? WHERE id=?",
                    ("retry" if retry else "uncertain", row["id"]),
                )
            for schedule in conn.execute(
                "SELECT * FROM schedules WHERE state='scheduled' AND due_at<=? ORDER BY due_at LIMIT 100",
                (stamp,),
            ).fetchall():
                row = conn.execute(
                    "SELECT data FROM policies WHERE id=?", (schedule["policy_id"],)
                ).fetchone()
                policy = ContactPolicy.model_validate_json(row[0])
                record, reason = self._eligible(conn, schedule, policy, stamp)
                if not record:
                    conn.execute(
                        "UPDATE schedules SET state='canceled',revision=revision+1 WHERE id=?",
                        (schedule["id"],),
                    )
                    continue
                delivery_id = (
                    "delivery_"
                    + digest(
                        [
                            schedule["id"],
                            schedule["due_at"],
                            json.loads(schedule["data"]).get("generation", 0),
                        ]
                    )[:32]
                )
                data = {
                    "id": delivery_id,
                    "schedule_id": schedule["id"],
                    "record_id": record["id"],
                    "record_revision": record["revision"],
                    "created_at": stamp,
                    "text": record["content"],
                    "source_ids": record["source_ids"],
                    "scope": record["scope"],
                    "attempts": [],
                }
                confirmed = json.loads(schedule["data"]).get("confirmed")
                state = (
                    "ready"
                    if policy.enabled
                    and policy.channel
                    and (not policy.require_confirmation or confirmed)
                    else "suggested"
                )
                conn.execute(
                    "INSERT OR IGNORE INTO outbox(id,schedule_id,state,available,data) VALUES(?,?,?,?,?)",
                    (delivery_id, schedule["id"], state, time.time(), dumps(data)),
                )
                conn.execute(
                    "UPDATE schedules SET state='queued',revision=revision+1 WHERE id=?",
                    (schedule["id"],),
                )
                created.append(delivery_id)
        if deliver:
            self.deliver_one(stamp)
        return {"created": created}

    def deliver_one(self, stamp=None):
        stamp = stamp or now()
        # Commit the send claim before entering the network transaction. A crash
        # after remote acceptance leaves a durable 'sending' row, never 'ready'.
        with self.engine.db.connect(write=True) as conn:
            claimed = conn.execute(
                "SELECT id FROM outbox WHERE state IN ('ready','retry') AND available<=? ORDER BY available LIMIT 1",
                (time.time(),),
            ).fetchone()
            if not claimed:
                return
            delivery_id = claimed["id"]
            conn.execute(
                "UPDATE outbox SET state='sending',lease_until=? WHERE id=?",
                (time.time() + 30, delivery_id),
            )
        # The bounded callback is serialized with cancellation commits: once a
        # cancel returns, no subsequent send can start for that schedule.
        with self.engine.db.connect(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM outbox WHERE id=? AND state='sending'", (delivery_id,)
            ).fetchone()
            if not row:
                return
            schedule = conn.execute(
                "SELECT * FROM schedules WHERE id=?", (row["schedule_id"],)
            ).fetchone()
            p = conn.execute(
                "SELECT data FROM policies WHERE id=?", (schedule["policy_id"],)
            ).fetchone()
            policy = ContactPolicy.model_validate_json(p[0])
            record, reason = self._eligible(conn, schedule, policy, stamp)
            if not record:
                conn.execute(
                    "UPDATE outbox SET state='canceled' WHERE id=?", (row["id"],)
                )
                return
            if (
                not policy.enabled
                or not policy.channel
                or (
                    policy.require_confirmation
                    and not json.loads(schedule["data"]).get("confirmed")
                )
            ):
                conn.execute(
                    "UPDATE outbox SET state='suggested' WHERE id=?", (row["id"],)
                )
                return
            local = datetime.fromisoformat(stamp).astimezone(ZoneInfo(policy.timezone))
            hour = local.hour
            quiet = (
                policy.quiet_start <= hour < policy.quiet_end
                if policy.quiet_start < policy.quiet_end
                else hour >= policy.quiet_start or hour < policy.quiet_end
                if policy.quiet_start != policy.quiet_end
                else False
            )
            prior = conn.execute(
                "SELECT o.data FROM outbox o JOIN schedules s ON s.id=o.schedule_id WHERE s.policy_id=? AND o.state IN ('sent','acknowledged') ORDER BY o.available DESC LIMIT 200",
                (policy.id,),
            ).fetchall()
            sent = [
                datetime.fromisoformat(json.loads(r[0])["sent_at"])
                for r in prior
                if json.loads(r[0]).get("sent_at")
            ]
            today_count = sum(
                t.astimezone(ZoneInfo(policy.timezone)).date() == local.date()
                for t in sent
            )
            interval = (
                bool(sent)
                and (local - max(sent)).total_seconds()
                < policy.min_interval_minutes * 60
            )
            if quiet or today_count >= policy.max_per_day or interval:
                conn.execute(
                    "UPDATE outbox SET state='ready',lease_until=NULL,available=? WHERE id=?",
                    (time.time() + 60, row["id"]),
                )
                return
            data = json.loads(row["data"])
            data.update(text=record["content"], record_revision=record["revision"])
            body = dumps({k: v for k, v in data.items() if k != "attempts"}).encode()
            headers = {"Content-Type": "application/json", "Idempotency-Key": row["id"]}
            secret = os.environ.get("EVENTMEM_WEBHOOK_SECRET")
            if secret:
                headers["X-MemoryPalace-Signature"] = hmac.new(
                    secret.encode(), body, hashlib.sha256
                ).hexdigest()
            conn.execute(
                "UPDATE outbox SET state='sending',attempts=attempts+1,lease_until=? WHERE id=?",
                (time.time() + 30, row["id"]),
            )
            try:
                with httpx.Client(timeout=5, follow_redirects=False) as client:
                    response = client.post(
                        policy.channel, content=body, headers=headers
                    )
                if not 200 <= response.status_code < 300:
                    raise RuntimeError(f"HTTP {response.status_code}")
                data["sent_at"] = stamp
                state = "sent"
            except Exception:
                state = (
                    "retry"
                    if policy.idempotent_channel and row["attempts"] < 4
                    else "uncertain"
                )
            data["attempts"].append({"at": stamp, "status": state})
            conn.execute(
                "UPDATE outbox SET state=?,data=?,lease_until=NULL,available=? WHERE id=?",
                (
                    state,
                    dumps(data),
                    time.time() + min(300, 2 ** (row["attempts"] + 1)),
                    row["id"],
                ),
            )
            if state == "sent":
                self._complete_schedule(conn, schedule, policy, stamp, row["id"])

    def _complete_schedule(self, conn, schedule, policy, stamp, delivery_id):
        config = json.loads(schedule["data"])
        current_id = (
            "delivery_"
            + digest([schedule["id"], schedule["due_at"], config.get("generation", 0)])[
                :32
            ]
        )
        # Late acknowledgments must not advance a resumed or canceled occurrence.
        if schedule["state"] != "queued" or current_id != delivery_id:
            return
        if config["recurrence"] == "none":
            conn.execute(
                "UPDATE schedules SET state='complete',revision=revision+1 WHERE id=?",
                (schedule["id"],),
            )
            return
        local = datetime.fromisoformat(stamp).astimezone(ZoneInfo(policy.timezone))
        due = datetime.fromisoformat(schedule["due_at"]).astimezone(
            ZoneInfo(policy.timezone)
        )
        while due <= local:
            if config["recurrence"] == "yearly":
                due = due.replace(
                    year=due.year + 1,
                    day=min(due.day, calendar.monthrange(due.year + 1, due.month)[1]),
                )
            else:
                due += timedelta(days=1 if config["recurrence"] == "daily" else 7)
        config.pop("confirmed", None)
        conn.execute(
            "UPDATE schedules SET state='scheduled',due_at=?,data=?,revision=revision+1 WHERE id=?",
            (
                due.astimezone(timezone.utc).isoformat(timespec="microseconds"),
                dumps(config),
                schedule["id"],
            ),
        )

    def acknowledge(self, delivery_id):
        with self.engine.db.connect(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM outbox WHERE id=?", (delivery_id,)
            ).fetchone()
            if not row:
                raise Missing(delivery_id)
            if row["state"] not in {"sent", "uncertain", "acknowledged"}:
                raise Conflict("Delivery has not been attempted")
            if row["state"] == "acknowledged":
                return {"id": delivery_id, "status": "acknowledged"}
            data = json.loads(row["data"])
            data.setdefault("sent_at", now())
            conn.execute(
                "UPDATE outbox SET state='acknowledged',data=? WHERE id=?",
                (dumps(data), delivery_id),
            )
            schedule = conn.execute(
                "SELECT * FROM schedules WHERE id=?", (row["schedule_id"],)
            ).fetchone()
            policy = ContactPolicy.model_validate_json(
                conn.execute(
                    "SELECT data FROM policies WHERE id=?", (schedule["policy_id"],)
                ).fetchone()[0]
            )
            self._complete_schedule(conn, schedule, policy, now(), delivery_id)
        return {"id": delivery_id, "status": "acknowledged"}
