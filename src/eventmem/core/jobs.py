from __future__ import annotations

import json
import threading
import time
import uuid

from .db import Conflict, Deleted, Missing, digest, dumps
from .models import RecordInput, Scope, now
from .providers import NotConfigured, ProviderError, Providers


class Worker:
    def __init__(self, engine, lease_seconds=90):
        self.engine = engine
        self.owner = uuid.uuid4().hex
        self.lease_seconds = lease_seconds
        self.stopped = threading.Event()
        self.last_maintenance = float("-inf")

    def claim(self):
        if self.engine.interactive_until > time.monotonic():
            return None
        with self.engine.db.connect(write=True) as conn:
            foreground = bool(
                conn.execute(
                    "SELECT 1 FROM sessions WHERE json_extract(data,'$.foreground_until')>? LIMIT 1",
                    (time.time(),),
                ).fetchone()
            )
            row = conn.execute(
                "SELECT data FROM settings WHERE key='maintenance'"
            ).fetchone()
            capacity = (
                max(1, int(json.loads(row[0]).get("concurrency", 2))) if row else 2
            )
            if (
                conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE state='running' AND lease_until>?",
                    (time.time(),),
                ).fetchone()[0]
                >= capacity
            ):
                return None
            conn.execute(
                "UPDATE jobs SET state='failed',error='Lease expired after maximum attempts' WHERE state='running' AND lease_until<? AND attempts>=max_attempts",
                (time.time(),),
            )
            conn.execute(
                "UPDATE jobs SET state='failed',error='Dependency failed or canceled' WHERE state='pending' AND EXISTS(SELECT 1 FROM job_dependencies d JOIN jobs parent ON parent.id=d.dependency_id WHERE d.job_id=jobs.id AND parent.state IN ('failed','canceled'))"
            )
            row = conn.execute(
                "SELECT * FROM jobs j WHERE ((state IN ('pending','retry') AND available<=?) OR (state='running' AND lease_until<?)) AND "
                "(?=0 OR priority<=30) AND "
                "NOT EXISTS(SELECT 1 FROM job_dependencies d LEFT JOIN jobs parent ON parent.id=d.dependency_id WHERE d.job_id=j.id AND (parent.state IS NULL OR parent.state!='complete')) ORDER BY priority,available,id LIMIT 1",
                (time.time(), time.time(), int(foreground)),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE jobs SET state='running',owner=?,lease_until=?,fence=fence+1,attempts=attempts+1,updated_at=? WHERE id=?",
                (self.owner, time.time() + self.lease_seconds, now(), row["id"]),
            )
            return dict(
                conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            )

    def owns(self, conn, job):
        row = conn.execute(
            "SELECT state,owner,fence,lease_until FROM jobs WHERE id=?", (job["id"],)
        ).fetchone()
        return (
            row
            and row["state"] == "running"
            and row["owner"] == self.owner
            and row["fence"] == job["fence"]
            and row["lease_until"] > time.time()
        )

    def renew(self, job, done):
        while not done.wait(max(0.05, self.lease_seconds / 3)):
            with self.engine.db.connect(write=True) as conn:
                if not self.owns(conn, job):
                    return
                conn.execute(
                    "UPDATE jobs SET lease_until=? WHERE id=?",
                    (time.time() + self.lease_seconds, job["id"]),
                )

    def run_once(self):
        job = self.claim()
        if not job:
            return False
        done = threading.Event()
        heartbeat = threading.Thread(target=self.renew, args=(job, done), daemon=True)
        heartbeat.start()
        try:
            payload = json.loads(job["payload"])
            model_config = self.engine.settings("models")
            inputs = {}
            with self.engine.db.connect() as conn:
                if payload.get("source_id"):
                    self.engine.source_current(conn, payload["source_id"])
                    inputs.update(
                        (r[0], r[1])
                        for r in conn.execute(
                            "SELECT r.id,r.revision FROM records r JOIN evidence e ON e.record_id=r.id WHERE e.source_id=? AND r.deleted=0",
                            (payload["source_id"],),
                        )
                    )
                if payload.get("record_id") and job["kind"] != "summary_part":
                    record = self.engine._get(conn, payload["record_id"])
                    inputs[record["id"]] = record["revision"]
            # A deadline belongs to the durable attempt, not to each nested request.
            timeout = float(
                self.engine.settings("maintenance").get("job_timeout_seconds", 300)
            )
            timeout = max(0.1, min(3600, timeout))
            finished = threading.Event()
            outcome = {}

            def prepare():
                try:
                    outcome["apply"] = self.prepare(job)
                except Exception as exc:
                    outcome["error"] = exc
                finally:
                    finished.set()

            threading.Thread(target=prepare, daemon=True).start()
            if not finished.wait(timeout):
                raise TimeoutError("Job execution deadline exceeded")
            if "error" in outcome:
                raise outcome["error"]
            apply = outcome["apply"]
            with self.engine.db.connect(write=True) as conn:
                if not self.owns(conn, job):
                    return True
                row = conn.execute(
                    "SELECT data FROM settings WHERE key='models'"
                ).fetchone()
                if (json.loads(row[0]) if row else {}) != model_config:
                    raise Conflict("Model configuration changed during execution")
                if payload.get("source_id"):
                    self.engine.source_current(conn, payload["source_id"])
                for rid, revision in inputs.items():
                    if self.engine._get(conn, rid)["revision"] != revision:
                        raise Conflict("Job input changed during execution")
                apply(conn)
                conn.execute(
                    "UPDATE jobs SET state='complete',lease_until=NULL,error=NULL,updated_at=? WHERE id=?",
                    (now(), job["id"]),
                )
                self.engine.db.bump(conn)
        except Exception as exc:
            with self.engine.db.connect(write=True) as conn:
                if self.owns(conn, job):
                    state = (
                        "waiting_config"
                        if isinstance(exc, (NotConfigured, ImportError))
                        else "canceled"
                        if isinstance(exc, (Deleted, Missing, Conflict))
                        else "failed"
                        if job["attempts"] >= job["max_attempts"]
                        else "retry"
                    )
                    error = (
                        str(exc)
                        if isinstance(
                            exc, (NotConfigured, ValueError, Conflict, ProviderError)
                        )
                        else type(exc).__name__
                    )
                    conn.execute(
                        "UPDATE jobs SET state=?,error=?,available=?,lease_until=NULL,updated_at=? WHERE id=?",
                        (
                            state,
                            error[:500],
                            time.time() + min(300, 2 ** job["attempts"]),
                            now(),
                            job["id"],
                        ),
                    )
                    if job["kind"] in {"event_summary", "summary_part"} and state in {
                        "failed",
                        "waiting_config",
                    }:
                        fid = json.loads(job["payload"]).get("family_id")
                        conn.execute(
                            "UPDATE families SET data=json_set(data,'$.summary.state','failed','$.summary.error',?) WHERE id=?",
                            (error[:500], fid),
                        )
        finally:
            done.set()
            heartbeat.join(timeout=1)
        return True

    def prepare(self, job):
        engine, kind = self.engine, job["kind"]
        payload = json.loads(job["payload"])
        if kind == "parse":
            from .media import parse

            records = parse(engine, payload["source_id"])

            def apply(conn):
                analysis_jobs = []
                for record in records:
                    engine._insert(conn, record)
                    if record.locator.get("analysis_role"):
                        analysis_jobs.append(
                            engine.enqueue(
                                "analyze_media",
                                {"record_id": record.id},
                                f"analyze:{record.id}",
                                conn=conn,
                            )
                        )
                sid = payload["source_id"]
                if analysis_jobs:
                    conn.execute(
                        "UPDATE sources SET model='pending' WHERE id=?", (sid,)
                    )
                    finish = engine.enqueue(
                        "media_complete",
                        {"source_id": sid},
                        f"media-complete:{sid}",
                        analysis_jobs,
                        conn=conn,
                    )
                    extraction = "job_" + digest(f"extract:{sid}")[:32]
                    if conn.execute(
                        "SELECT 1 FROM jobs WHERE id=?", (extraction,)
                    ).fetchone():
                        conn.execute(
                            "INSERT OR IGNORE INTO job_dependencies VALUES(?,?)",
                            (extraction, finish),
                        )
                engine.supersede_source_versions(conn, sid)
                conn.execute(
                    "UPDATE sources SET mechanical='complete' WHERE id=?", (sid,)
                )
                source = engine._source(
                    conn.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
                )
                if (
                    not source.get("extract")
                    and not analysis_jobs
                    and source["media_type"].startswith(("image/", "audio/", "video/"))
                ):
                    conn.execute(
                        "UPDATE sources SET model='complete' WHERE id=?", (sid,)
                    )

            return apply
        if kind == "media_complete":
            source = engine.source(payload["source_id"])
            return (
                (lambda conn: None)
                if source.get("extract")
                else (
                    lambda conn: conn.execute(
                        "UPDATE sources SET model='complete' WHERE id=?",
                        (source["id"],),
                    )
                )
            )
        if kind == "analyze_media":
            record = engine.get(payload["record_id"])
            if not record["attributes"].get("analysis_pending"):
                return lambda conn: None
            locator = record["locator"]
            path = (
                engine.db.blobs / locator["blob"]
                if locator.get("blob")
                else engine.source(locator["source_id"], content=True)
            )
            provider = Providers(engine)
            if locator["analysis_role"] == "asr":
                response = provider.transcribe(path)
                segments = response.get("segments") or [
                    {"start": 0, "end": 300, "text": response.get("text", "")}
                ]
                content = "\n".join(s["text"] for s in segments).strip()
            else:
                response = provider.json(
                    "vision",
                    'Describe visible content and transcribe visible text, preserving tables. Return {"description":"...","ocr":"..."}.',
                    {"locator": locator},
                    image=(locator.get("mime", "image/png"), path.read_bytes()),
                )
                content = (
                    response.get("description", "") + "\n" + response.get("ocr", "")
                ).strip()
                segments = []
            if not content:
                content = "No recognizable text or description returned"

            def apply(conn):
                current = engine._get(conn, record["id"])
                if current["revision"] != record["revision"]:
                    raise Conflict("Attachment annotation changed during analysis")
                current.update(content=content, generated=True, confirmation="inferred")
                current["attributes"]["analysis_pending"] = False
                if segments:
                    current["locator"]["segments"] = [
                        {
                            "start_seconds": locator["start_seconds"]
                            + float(s.get("start", 0)),
                            "end_seconds": locator["start_seconds"]
                            + float(s.get("end", 0)),
                            "text": s["text"],
                        }
                        for s in segments
                    ]
                engine._save_revision(
                    conn, current, "media_analysis", "Configured model analysis"
                )

            return apply
        if kind == "extract_complete":
            engine.source(payload["source_id"])
            return lambda conn: conn.execute(
                "UPDATE sources SET model='complete' WHERE id=?",
                (payload["source_id"],),
            )
        if kind in {"extract", "extract_part"}:
            sid = payload["source_id"]
            source = engine.source(sid)
            if source["mechanical"] != "complete":
                raise Conflict("Mechanical parsing has not completed")
            if kind == "extract_part":
                with engine.db.connect() as conn:
                    for rid, revision in payload.get("input_revisions", {}).items():
                        if engine._get(conn, rid)["revision"] != revision:
                            raise Conflict("Extraction batch input changed")
                text = payload["text"]
            else:
                with engine.db.connect() as conn:
                    rows = conn.execute(
                        "SELECT r.data FROM records r JOIN evidence e ON e.record_id=r.id WHERE e.source_id=? AND r.deleted=0 AND r.status IN ('active','unverified') AND (json_extract(r.data,'$.generated')=0 OR (json_extract(r.data,'$.locator.source_id')=? AND json_extract(r.data,'$.locator.analysis_role') IN ('asr','vision'))) ORDER BY r.id",
                        (sid, sid),
                    )
                    batches, current = [], ""
                    for row in rows:
                        content = json.loads(row[0])["content"]
                        for offset in range(0, len(content), 47000):
                            piece = content[offset : offset + 48000]
                            if current and len(current) + len(piece) + 1 > 48000:
                                batches.append(current)
                                current = ""
                            current += ("\n" if current else "") + piece
                    if current:
                        batches.append(current)
                if len(batches) > 1:

                    def schedule_parts(conn):
                        dependencies = []
                        for i, text in enumerate(batches):
                            dependencies.append(
                                engine.enqueue(
                                    "extract_part",
                                    {
                                        "source_id": sid,
                                        "text": text,
                                        "part": i,
                                        "input_revisions": {
                                            json.loads(row[0])["id"]: json.loads(
                                                row[0]
                                            )["revision"]
                                            for row in conn.execute(
                                                "SELECT r.data FROM records r JOIN evidence e ON e.record_id=r.id WHERE e.source_id=? AND r.deleted=0",
                                                (sid,),
                                            )
                                        },
                                    },
                                    f"extract-part:{sid}:{i}",
                                    conn=conn,
                                )
                            )
                        engine.enqueue(
                            "extract_complete",
                            {"source_id": sid},
                            f"extract-complete:{sid}",
                            dependencies,
                            conn=conn,
                        )

                    return schedule_parts
                text = batches[0] if batches else ""
            result = Providers(engine).json(
                "extraction",
                'Extract facts, preferences, relationships, commitments, procedures and episodes. Return {"candidates":[{"kind":"fact","content":"...","quote":"exact source excerpt","title":"...","attributes":{}}]}. Only include conclusions supported by an exact quote. Inferences remain unverified.',
                {"source_id": sid, "text": text, "scope": source["scope"]},
            )
            proposals = []
            for i, candidate in enumerate(result.get("candidates", [])[:100]):
                quote = candidate.get("quote", "")
                if not quote or quote not in text:
                    continue
                proposals.append(
                    RecordInput(
                        id="mem_"
                        + digest([sid, "extracted", payload.get("part", 0), i])[:32],
                        kind=candidate["kind"],
                        content=candidate["content"],
                        title=candidate.get("title", ""),
                        source_ids=[sid],
                        scope=Scope(**source["scope"]),
                        valid_from=source["occurred_at"],
                        generated=True,
                        confirmation="inferred",
                        attributes=candidate.get("attributes", {}),
                        locator={"quote": quote, "source_id": sid},
                    )
                )

            def apply(conn):
                for record in proposals:
                    created = engine._insert(conn, record)
                    engine.enqueue(
                        "conflict",
                        {"record_id": created["id"]},
                        f"conflict:{created['id']}",
                        conn=conn,
                    )
                if kind == "extract":
                    conn.execute(
                        "UPDATE sources SET model='complete' WHERE id=?", (sid,)
                    )

            return apply
        if kind == "conflict":
            data = engine.get(payload["record_id"])
            from .models import RecallRequest

            # Model proposals can describe conflicts; only a typed user/domain
            # command can replace an active authoritative assertion.
            hits = engine.recall(
                RecallRequest(
                    query=data["content"][:2000],
                    scope=Scope(**data["scope"]),
                    history=True,
                    limit=10,
                )
            )
            result = Providers(engine).json(
                "conflict",
                'Return {"relations":[{"id":"existing id","relation":"coexists|refutes|supports","reason":"..."}]}. Consider time, scope, version and independent evidence.',
                {"candidate": data, "existing": hits["items"], "scope": data["scope"]},
            )

            def apply(conn):
                for relation in result.get("relations", [])[:20]:
                    if (
                        relation.get("id") in {r["id"] for r in hits["items"]}
                        and relation["id"] != data["id"]
                        and relation.get("relation")
                        in {"coexists", "refutes", "supports"}
                    ):
                        engine._relation(
                            conn,
                            data["id"],
                            relation["relation"],
                            relation["id"],
                            {
                                "generated": True,
                                "confirmed": False,
                                "reason": relation.get("reason", ""),
                            },
                        )

            return apply
        if kind in {"embed", "visual_embed"}:
            record = engine.get(payload["record_id"])
            if record["revision"] != payload["revision"]:
                return lambda conn: None
            if kind == "visual_embed":
                locator = record["locator"]
                raw = (
                    (engine.db.blobs / locator["blob"]).read_bytes()
                    if locator.get("blob")
                    else engine.source(locator["source_id"], content=True).read_bytes()
                )
                vector, index_id = Providers(engine).visual_embed(image=raw)
                vectors = [vector]
            else:
                vectors, index_id = Providers(engine).embed(
                    [record["title"] + "\n" + record["content"]]
                )
            from .vectors import VectorIndex

            def apply(conn):
                current = engine._get(conn, record["id"])
                if current["revision"] == record["revision"]:
                    VectorIndex(engine, index_id).upsert(
                        [
                            {
                                "id": record["id"],
                                "scope": Scope(**record["scope"]).key(),
                                "revision": record["revision"],
                                "vector": vectors[0],
                            }
                        ]
                    )

            return apply
        if kind == "summary_part":
            from .organize import prepare_summary_part

            return prepare_summary_part(engine, payload)
        if kind == "procedure_review":
            from .organize import prepare_procedure

            return prepare_procedure(engine, payload)
        if kind == "event_summary":
            from .organize import prepare_summary

            return prepare_summary(engine, payload)
        if kind == "event_group":
            from .organize import prepare_events

            return prepare_events(engine, Scope(**payload["scope"]))
        if kind == "prefetch":
            from .db import tokenize
            from .models import RecallRequest

            request = RecallRequest(
                query=str(payload["cue"])[:4000],
                scope=Scope(**payload["scope"]),
                budget=2000,
            )
            result = engine.recall(request)

            def apply(conn):
                conn.execute(
                    "DELETE FROM prefetch WHERE scope=?", (request.scope.key(),)
                )
                for cue in list(dict.fromkeys(tokenize(request.query).split()))[:30]:
                    for record in result["items"]:
                        conn.execute(
                            "INSERT OR IGNORE INTO prefetch VALUES(?,?,?,?)",
                            (
                                request.scope.key(),
                                cue,
                                record["id"],
                                record["revision"],
                            ),
                        )

            return apply
        if kind == "organize":
            from .organize import prepare_communities

            return prepare_communities(engine, Scope(**payload["scope"]))
        if kind == "rebuild":

            def apply(conn):
                conn.execute("DELETE FROM search")
                for row in conn.execute(
                    "SELECT data FROM records WHERE deleted=0"
                ).fetchall():
                    data = json.loads(row[0])
                    engine._index_text(conn, data)
                    engine._dirty(conn, data)

            return apply
        if kind in ("build_vectors", "purge_vectors"):
            from .vectors import VectorIndex

            with engine.db.connect() as conn:
                indexes = [r[0] for r in conn.execute("SELECT id FROM vector_indexes")]
            # Derived work is safely repeatable after lease loss; canonical reads
            # still reject deleted or superseded records.
            for index_id in indexes:
                index = VectorIndex(engine, index_id)
                index.build() if kind == "build_vectors" else index.purge()
            return lambda conn: None
        raise ValueError(f"Unknown job kind: {kind}")

    def run(self):
        from .scheduler import Scheduler

        scheduler = Scheduler(self.engine)
        while not self.stopped.is_set():
            self.replay_hosts()
            self.schedule_maintenance()
            scheduler.tick()
            if not self.run_once():
                self.stopped.wait(0.5)

    def schedule_maintenance(self):
        config = self.engine.settings("maintenance")
        interval = max(30, min(86400, config.get("interval_seconds", 60)))
        if (
            time.monotonic() - self.last_maintenance < interval
            or self.engine.interactive_until > time.monotonic()
        ):
            return
        self.last_maintenance = time.monotonic()
        from .organize import summary_snapshot
        from .read_policy import ReadPolicy

        with self.engine.db.connect(write=True) as conn:
            if conn.execute(
                "SELECT 1 FROM sessions WHERE json_extract(data,'$.foreground_until')>? LIMIT 1",
                (time.time(),),
            ).fetchone():
                return
            scopes = conn.execute(
                "SELECT DISTINCT r.scope FROM dirty d JOIN records r ON r.id=d.record_id WHERE r.deleted=0 AND r.status='active' AND json_extract(r.data,'$.generated')=0 LIMIT 30"
            ).fetchall()
            for row in scopes:
                scope = Scope(**json.loads(row[0]))
                policy = ReadPolicy.load(self.engine, scope, conn=conn)
                versions = [
                    (r["id"], r["revision"])
                    for item in conn.execute(
                        "SELECT r.data FROM dirty d JOIN records r ON r.id=d.record_id WHERE r.scope=? AND r.deleted=0 AND r.status='active' AND r.revision=d.revision AND json_extract(r.data,'$.generated')=0 ORDER BY r.updated_at,r.id LIMIT 64",
                        (scope.key(),),
                    )
                    if policy.visible(r := json.loads(item[0]))
                ]
                if versions:
                    self.engine.enqueue(
                        "event_group",
                        {"scope": scope.model_dump()},
                        "event-group:" + digest([scope.key(), versions]),
                        conn=conn,
                    )
            for row in conn.execute(
                "SELECT id FROM families WHERE state NOT IN ('archived','merged') AND json_extract(data,'$.summary.state')='dirty' LIMIT 30"
            ).fetchall():
                _, _, signature = summary_snapshot(self.engine, conn, row[0])
                self.engine.enqueue(
                    "event_summary",
                    {"family_id": row[0], "input_revision": signature},
                    "event-summary:" + digest([row[0], signature]),
                    conn=conn,
                )

    def replay_hosts(self):
        from .hosts import handle

        spool = self.engine.db.root / "host-spool"
        if not spool.exists():
            return
        for path in sorted(spool.glob("*.json"))[:20]:
            try:
                data = json.loads(path.read_text())
                # A replay cannot inject into a past host context; only receipt
                # events are replayed. Startup/compact are fetched again live.
                if data["event"] == "context_receipt":
                    from .context import ContextReceipt, settle_context

                    settle_context(
                        self.engine, ContextReceipt.model_validate(data["payload"])
                    )
                elif data["event"] in {"tool", "message", "boundary", "end", "compact"}:
                    handle(
                        self.engine, data["event"], data["payload"], receipt_only=True
                    )
                path.unlink(missing_ok=True)
            except Deleted:
                path.unlink(missing_ok=True)
            except Exception:
                continue

    def control(self, jid, action):
        if action not in {"cancel", "retry"}:
            raise ValueError("Unknown job action")
        with self.engine.db.connect(write=True) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            if not row:
                raise Missing(jid)
            conn.execute(
                "UPDATE jobs SET state=?,available=?,owner=NULL,lease_until=NULL,fence=fence+1,error=NULL,attempts=0 WHERE id=?",
                ("canceled" if action == "cancel" else "pending", time.time(), jid),
            )
        return {"id": jid, "status": "canceled" if action == "cancel" else "pending"}

    def recover(self, job_ids, *, command_id, target=None):
        """Requeue failed jobs once, keeping the failure linked to the recovery.

        `control(retry)` answers an operator's click; it drops the error and leaves
        nothing behind. A derivative recovery needs the opposite bookkeeping: the
        failed row keeps its identity, the failure moves into `job_recovery`, and a
        second call for the same batch finds no `failed` row left to touch. Only
        `failed` rows move, never to `complete`; a job that failed again after an
        earlier recovery is a new failure and takes a new command id."""
        if (
            not command_id
            or not 1 <= len(job_ids) <= 50
            or len(set(job_ids)) != len(job_ids)
        ):
            raise ValueError(
                "Recovery requires a sourced command and unique bounded jobs"
            )
        recovered, skipped = [], []
        with self.engine.db.connect(write=True) as conn:
            for jid in job_ids:
                row = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
                if not row:
                    raise Missing(jid)
                if row["state"] != "failed":
                    skipped.append({"id": jid, "state": row["state"]})
                    continue
                payload = json.loads(row["payload"])
                conn.execute(
                    "INSERT OR IGNORE INTO job_recovery VALUES(?,?,?,?,?,?,?,?)",
                    (
                        jid,
                        command_id,
                        row["kind"],
                        target or dumps(payload),
                        row["state"],
                        row["error"],
                        row["attempts"],
                        now(),
                    ),
                )
                moved = conn.execute(
                    "UPDATE jobs SET state='pending',available=?,owner=NULL,lease_until=NULL,fence=fence+1,error=NULL,attempts=0,updated_at=? WHERE id=? AND state='failed'",
                    (time.time(), now(), jid),
                ).rowcount
                if moved:
                    recovered.append(jid)
                else:
                    # Lost the race to another recovery; the audit row above is the
                    # loser too, so take it back out.
                    conn.execute(
                        "DELETE FROM job_recovery WHERE job_id=? AND command_id=?",
                        (jid, command_id),
                    )
                    skipped.append({"id": jid, "state": "changed-during-recovery"})
        return {"recovered": recovered, "skipped": skipped, "command_id": command_id}
