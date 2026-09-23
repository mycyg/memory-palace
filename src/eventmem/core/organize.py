from __future__ import annotations

import json
import math

from .db import Conflict, Missing, digest, dumps
from .models import Scope, now


def prepare_communities(engine, scope):
    import igraph as ig

    with engine.db.connect() as conn:
        dirty = conn.execute(
            "SELECT r.id,r.data,d.revision FROM dirty d JOIN records r ON r.id=d.record_id WHERE r.scope=? AND r.deleted=0 AND r.status='active' AND d.revision=r.revision ORDER BY r.id LIMIT 2000",
            (scope.key(),),
        ).fetchall()
        ids = {r["id"] for r in dirty}
        # Include bounded neighbors of changed records; unrelated topics are not
        # scanned or sent to a model.
        edges = []
        for rid in sorted(ids):
            rows = conn.execute(
                "SELECT subject,object FROM relations WHERE scope=? AND (subject=? OR object=?) LIMIT 30",
                (scope.key(), rid, rid),
            ).fetchall()
            edges.extend((r[0], r[1]) for r in rows)
        node_ids = sorted(ids | {x for e in edges for x in e})[:4000]
        # A topic family is offered as material that was lived. A role agreement, its examples,
        # a self-claim and a host envelope are none of those, so they are not clustered.
        from .read_policy import ReadPolicy

        policy = ReadPolicy.load(engine, scope, "experience_recall", conn=conn)
        records = {}
        for rid in node_ids:
            try:
                data = engine._get(conn, rid)
                if data["status"] == "active" and policy.visible(data):
                    records[rid] = data
            except Missing:
                pass
    node_ids = sorted(records)
    index = {rid: i for i, rid in enumerate(node_ids)}
    graph = ig.Graph(
        n=len(node_ids),
        edges=[(index[a], index[b]) for a, b in edges if a in index and b in index],
        directed=False,
    )
    if not node_ids:
        return lambda conn: None
    # Exact normalized topic labels are deterministic candidate links, not facts.
    topics = {}
    for rid, data in records.items():
        topic = data["attributes"].get("topic")
        if topic:
            topics.setdefault(str(topic), []).append(index[rid])
    for members in topics.values():
        graph.add_edges((members[0], other) for other in members[1:])
    graph.simplify()
    clusters = (
        graph.community_leiden(objective_function="modularity", n_iterations=2)
        if graph.ecount()
        else [[i] for i in range(len(node_ids))]
    )
    families = []
    clusters = [
        list(cluster)[start : start + 1000]
        for cluster in clusters
        for start in range(0, len(cluster), 1000)
    ]
    for cluster in clusters:
        members = sorted(node_ids[i] for i in cluster)
        if len(members) < 2:
            continue
        fid = "family_" + digest([scope.key(), members])[:24]
        subgraph = graph.induced_subgraph(cluster)
        layout = subgraph.layout_fruchterman_reingold(dim=3, niter=100)
        positions = {
            node_ids[original]: list(layout[i]) for i, original in enumerate(cluster)
        }
        title = (
            records[members[0]]["attributes"].get("topic")
            or records[members[0]]["title"]
            or "Topic"
        )
        families.append(
            {
                "id": fid,
                "scope": scope.model_dump(),
                "kind": "family",
                "state": "candidate",
                "revision": 1,
                "title": str(title)[:200],
                "members": members,
                "positions": positions,
                "basis": "Leiden on explicit relations and topic labels",
                "generated_at": now(),
            }
        )

    def apply(conn):
        # The graph was prepared outside the writer transaction. Retry the job
        # when a correction or deletion has changed any of its inputs.
        for rid, snapshot in records.items():
            try:
                current = engine._get(conn, rid)
            except Missing:
                raise Conflict("Community input was deleted") from None
            if (
                current["revision"] != snapshot["revision"]
                or current["status"] != "active"
            ):
                raise Conflict("Community input revision changed")
        for family in families:
            conn.execute(
                "INSERT OR IGNORE INTO families VALUES(?,?,?,?,?,?)",
                (family["id"], scope.key(), "family", "candidate", 1, dumps(family)),
            )
            conn.execute(
                "INSERT OR IGNORE INTO family_revisions VALUES(?,?,?)",
                (family["id"], 1, dumps(family)),
            )
            for rid in family["members"]:
                conn.execute(
                    "INSERT OR IGNORE INTO members VALUES(?,?,?)",
                    (
                        family["id"],
                        rid,
                        dumps({"basis": family["basis"], "state": "candidate"}),
                    ),
                )
        for row in dirty:
            conn.execute(
                "DELETE FROM dirty WHERE record_id=? AND revision=?",
                (row["id"], row["revision"]),
            )

    return apply


class Organizer:
    def __init__(self, engine):
        self.engine = engine

    def list(self, scope, limit=100, cursor=""):
        with self.engine.db.connect() as conn:
            return [
                json.loads(r[0])
                for r in conn.execute(
                    "SELECT data FROM families WHERE scope=? AND id>? ORDER BY id LIMIT ?",
                    (scope.key(), cursor, min(200, limit)),
                )
            ]

    def create(self, scope, title, members, kind="volume"):
        from .engine import uid

        if kind not in {"volume", "family", "event"} or not 1 <= len(members) <= 1000:
            raise ValueError("A family or volume needs 1–1000 members")
        with self.engine.db.connect(write=True) as conn:
            for rid in members:
                data = self.engine._get(conn, rid)
                if data["scope"] != scope.model_dump():
                    raise Conflict("Members must share a scope")
            fid = uid(kind)
            data = {
                "id": fid,
                "scope": scope.model_dump(),
                "kind": kind,
                "state": "candidate",
                "revision": 1,
                "title": title,
                "members": sorted(set(members)),
                "positions": {},
                "basis": "Explicit membership",
                "summary": {"state": "dirty"},
            }
            conn.execute(
                "INSERT INTO families VALUES(?,?,?,?,?,?)",
                (fid, scope.key(), kind, "candidate", 1, dumps(data)),
            )
            conn.execute(
                "INSERT INTO family_revisions VALUES(?,?,?)", (fid, 1, dumps(data))
            )
            for rid in data["members"]:
                conn.execute(
                    "INSERT INTO members VALUES(?,?,?)",
                    (fid, rid, dumps({"basis": "explicit"})),
                )
            return data

    def change(
        self,
        fid,
        revision,
        action,
        *,
        members=None,
        target=None,
        title=None,
        target_revision=None,
    ):
        with self.engine.db.connect(write=True) as conn:
            row = conn.execute("SELECT * FROM families WHERE id=?", (fid,)).fetchone()
            if not row:
                raise Missing(fid)
            if row["revision"] != revision:
                raise Conflict("Family revision changed")
            data = json.loads(row["data"])
            previous_members, previous_title = data["members"], data["title"]
            if action == "rollback":
                old = conn.execute(
                    "SELECT data FROM family_revisions WHERE id=? AND revision=?",
                    (fid, target_revision),
                ).fetchone()
                if not old:
                    raise Missing("Family revision")
                # The historical snapshot may point at a different summary.
                invalidate_summary(self.engine, conn, fid, "Event family rolled back")
                data = json.loads(old[0])
            elif action == "publish":
                data["state"] = "published"
            elif action == "archive":
                data["state"] = "archived"
            elif action == "merge":
                other = conn.execute(
                    "SELECT * FROM families WHERE id=?", (target,)
                ).fetchone()
                if not other or other["scope"] != row["scope"] or target == fid:
                    raise Conflict("Merge target must share scope")
                other_data = json.loads(other["data"])
                data["members"] = sorted(set(data["members"] + other_data["members"]))
                other_data.update(
                    state="merged",
                    merged_into=fid,
                    revision=other_data["revision"] + 1,
                    summary={**other_data.get("summary", {}), "state": "dirty"},
                )
                conn.execute(
                    "UPDATE families SET state='merged',revision=?,data=? WHERE id=?",
                    (other_data["revision"], dumps(other_data), target),
                )
                conn.execute(
                    "INSERT INTO family_revisions VALUES(?,?,?)",
                    (target, other_data["revision"], dumps(other_data)),
                )
                invalidate_summary(self.engine, conn, target, "Event family merged")
            elif action in {"revise", "split"}:
                if members is None:
                    raise ValueError("Members required")
                if action == "split":
                    if not set(members) < set(data["members"]):
                        raise ValueError("Split requires a proper subset of members")
                    from .engine import uid

                    split_id = uid(data["kind"])
                    split_data = data | {
                        "id": split_id,
                        "revision": 1,
                        "members": sorted(set(members)),
                        "title": title or data["title"],
                        "state": "candidate",
                        "summary": {"state": "dirty"},
                    }
                    conn.execute(
                        "INSERT INTO families VALUES(?,?,?,?,?,?)",
                        (
                            split_id,
                            row["scope"],
                            row["kind"],
                            "candidate",
                            1,
                            dumps(split_data),
                        ),
                    )
                    conn.execute(
                        "INSERT INTO family_revisions VALUES(?,?,?)",
                        (split_id, 1, dumps(split_data)),
                    )
                    for rid in members:
                        conn.execute(
                            "INSERT INTO members VALUES(?,?,?)",
                            (split_id, rid, '{"basis":"split"}'),
                        )
                    data["members"] = sorted(set(data["members"]) - set(members))
                    data["split_into"] = split_id
                else:
                    data["members"] = sorted(set(members))
                    if title:
                        data["title"] = title
            else:
                raise ValueError("Unknown family action")
            if len(data["members"]) > 1000:
                raise ValueError("Family exceeds 1000 members")
            for rid in data["members"]:
                if Scope(**self.engine._get(conn, rid)["scope"]).key() != row["scope"]:
                    raise Conflict("Member scope mismatch")
            data["revision"] = revision + 1
            data["summary"] = {**data.get("summary", {}), "state": "dirty"}
            conn.execute(
                "UPDATE families SET state=?,revision=?,data=? WHERE id=?",
                (data["state"], data["revision"], dumps(data), fid),
            )
            conn.execute(
                "INSERT INTO family_revisions VALUES(?,?,?)",
                (fid, data["revision"], dumps(data)),
            )
            conn.execute("DELETE FROM members WHERE family_id=?", (fid,))
            for rid in data["members"]:
                conn.execute(
                    "INSERT INTO members VALUES(?,?,?)",
                    (fid, rid, dumps({"basis": action})),
                )
            if (
                data["members"] != previous_members
                or data["title"] != previous_title
                or data["state"] in {"archived", "merged"}
                or action == "rollback"
            ):
                invalidate_summary(self.engine, conn, fid, "Event family changed")
            self.engine.db.bump(conn)
            return data

    def graph(self, scope, family_id=None, limit=150):
        limit = max(1, min(limit, 300))
        with self.engine.db.connect() as conn:
            positions = {}
            if family_id:
                row = conn.execute(
                    "SELECT data FROM families WHERE id=? AND scope=?",
                    (family_id, scope.key()),
                ).fetchone()
                if not row:
                    raise Missing(family_id)
                data = json.loads(row[0])
                ids = data["members"][:limit]
                positions = data.get("positions", {})
            else:
                ids = [
                    r[0]
                    for r in conn.execute(
                        "SELECT id FROM records WHERE scope=? AND deleted=0 ORDER BY importance DESC LIMIT ?",
                        (scope.key(), limit),
                    )
                ]
            nodes = []
            for i, rid in enumerate(ids):
                try:
                    record = self.engine._get(conn, rid)
                    nodes.append(
                        {
                            "id": rid,
                            "title": record["title"] or record["kind"],
                            "kind": record["kind"],
                            "status": record["status"],
                            "position": positions.get(
                                rid,
                                [
                                    math.cos(i * 2.4) * math.sqrt(i + 1),
                                    math.sin(i * 2.4) * math.sqrt(i + 1),
                                    (i % 7) - 3,
                                ],
                            ),
                        }
                    )
                except Missing:
                    pass
            allowed = {n["id"] for n in nodes}
            edges = []
            for rid in allowed:
                edges.extend(
                    dict(r)
                    for r in conn.execute(
                        "SELECT id,subject,object,predicate FROM relations WHERE subject=? LIMIT 40",
                        (rid,),
                    )
                    if r["object"] in allowed
                )
        return {
            "nodes": nodes,
            "edges": edges,
            "limit": limit,
            "layout": "precomputed" if positions else "deterministic",
        }


def invalidate_summary(engine, conn, family_id, reason):
    """Withdraw the current summary while retaining its sourced revision history."""
    row = conn.execute("SELECT data FROM families WHERE id=?", (family_id,)).fetchone()
    if not row:
        raise Missing(family_id)
    family = json.loads(row[0])
    summary = family.get("summary", {})
    if summary.get("state") != "dirty":
        family["summary"] = {**summary, "state": "dirty"}
        conn.execute("UPDATE families SET data=? WHERE id=?", (dumps(family), family_id))
    rid = summary.get("record_id")
    if rid:
        try:
            record = engine._get(conn, rid)
        except Missing:
            return
        if record["status"] == "active":
            record["status"] = "unverified"
            engine._save_revision(conn, record, "summary_invalidated", reason)


def invalidate_membership(conn, record_id):
    """A changed member invalidates its derived summary, not the original evidence."""
    conn.execute(
        "UPDATE families SET data=json_set(data,'$.summary.state','dirty') "
        "WHERE id IN (SELECT family_id FROM members WHERE record_id=?)",
        (record_id,),
    )


def summary_snapshot(engine, conn, family_id):
    row = conn.execute("SELECT data FROM families WHERE id=?", (family_id,)).fetchone()
    if not row:
        raise Missing(family_id)
    family = json.loads(row[0])
    records = []
    for rid in family["members"]:
        try:
            records.append(engine._get(conn, rid))
        except Missing:
            pass
    records = [r for r in records if r["status"] == "active"]
    from .read_policy import ReadPolicy

    policy = ReadPolicy.load(
        engine, Scope(**family["scope"]), "experience_recall", conn=conn
    )
    records = [r for r in records if policy.visible(r)]
    signature = digest(
        [
            family["revision"],
            [(r["id"], r["revision"]) for r in records],
            engine.settings("models").get("summary", {}),
        ]
    )
    return family, records, signature


def summary_chunks(records, budget=6000):
    from .retrieval import encoding, tokens

    chunks, current, used = [], [], 0
    for record in records:
        encoded = encoding().encode(record["content"], disallowed_special=())
        offset = 0
        while offset < len(encoded):

            def piece_at(end):
                return {
                    "id": record["id"],
                    "revision": record["revision"],
                    "content": encoding().decode(encoded[offset:end]),
                    "token_offset": offset,
                    "confirmation": record["confirmation"],
                }

            low, high = offset + 1, min(len(encoded), offset + budget - 300)
            while low < high:
                mid = (low + high + 1) // 2
                if tokens(dumps(piece_at(mid))) + 2 <= budget - 128:
                    low = mid
                else:
                    high = mid - 1
            piece = piece_at(low)
            cost = tokens(dumps(piece)) + 2
            if current and used + cost > budget - 128:
                chunks.append(current)
                current, used = [], 0
            current.append(piece)
            used += cost
            offset = low
    if current:
        chunks.append(current)
    return chunks


def summary_output(engine, title, records):
    from .providers import Providers

    output = Providers(engine).json(
        "summary",
        "Summarize the sourced work event: decisions, results, corrections, remaining work and uncertainty. "
        'Keep names, negation and unfinished commitments. Return {"content":string,"evidence_ids":[record_id]}. '
        "Cite supplied evidence and never upgrade an inference to a confirmed result. Keep the summary under 2000 tokens.",
        {"title": title, "records": records},
    )
    evidence = output.get("evidence_ids", [])
    if (
        not isinstance(output.get("content"), str)
        or not output["content"].strip()
        or not evidence
        or not set(evidence) <= {r["id"] for r in records}
    ):
        raise ValueError("Summary needs content and valid evidence")
    from .retrieval import tokens

    if tokens(output["content"]) > 2000:
        raise ValueError("Summary exceeds its 2000-token output budget")
    return output


def prepare_summary(engine, payload):
    from .models import RecordInput

    fid = payload["family_id"]
    with engine.db.connect() as conn:
        family, records, signature = summary_snapshot(engine, conn, fid)
    if payload.get("input_revision", signature) != signature:
        raise Conflict("Summary input changed")
    if not records:

        def empty(conn):
            current, _, fresh = summary_snapshot(engine, conn, fid)
            if fresh != signature:
                raise Conflict("Summary input changed")
            current["summary"] = {
                "state": "ready",
                "record_id": None,
                "input_revision": signature,
            }
            conn.execute("UPDATE families SET data=? WHERE id=?", (dumps(current), fid))

        return empty
    if payload.get("parts"):
        with engine.db.connect() as conn:
            records = [engine._get(conn, rid) for rid in payload["parts"]]
        if any(
            r["scope"] != family["scope"]
            or r["status"] != "active"
            or not r["attributes"].get("summary_part")
            for r in records
        ):
            raise Conflict("Summary parts changed")
    chunks = summary_chunks(records)
    config = engine.settings("models").get("summary", {})
    part_ids = [
        "mem_"
        + digest(["summary-part-v2", family["scope"], family["title"], config, chunk])[:32]
        for chunk in chunks
    ]
    if len(chunks) > 1:

        def schedule(conn):
            current, _, fresh = summary_snapshot(engine, conn, fid)
            if fresh != signature:
                raise Conflict("Summary input changed")
            dependencies = []
            for rid, chunk in zip(part_ids, chunks):
                if conn.execute(
                    "SELECT 1 FROM records WHERE id=? AND deleted=0 AND status='active'",
                    (rid,),
                ).fetchone():
                    continue
                dependencies.append(
                    engine.enqueue(
                        "summary_part",
                        {
                            "family_id": fid,
                            "input_revision": signature,
                            "record_id": rid,
                            "pieces": chunk,
                        },
                        f"summary-part:{rid}:{signature}",
                        conn=conn,
                    )
                )
            engine.enqueue(
                "event_summary",
                {"family_id": fid, "input_revision": signature, "parts": part_ids},
                f"summary-final:{fid}:{signature}:{digest(part_ids)}",
                dependencies,
                conn=conn,
            )
            current["summary"] = {
                **current.get("summary", {}),
                "state": "refreshing",
                "input_revision": signature,
            }
            conn.execute("UPDATE families SET data=? WHERE id=?", (dumps(current), fid))

        return schedule
    from .retrieval import tokens

    if (
        len(records) == 1
        and not payload.get("parts")
        and tokens(records[0]["content"]) <= 2000
    ):
        output = {"content": records[0]["content"], "evidence_ids": [records[0]["id"]]}
    else:
        output = summary_output(engine, family["title"], chunks[0])
    evidence = [r["id"] for r in records]
    source_ids = sorted({sid for r in records for sid in r["source_ids"]})
    record = RecordInput(
        id="mem_" + digest([fid, signature, config])[:32],
        kind="summary",
        scope=Scope(**family["scope"]),
        title=family["title"],
        content=output["content"],
        source_ids=source_ids,
        evidence_ids=evidence,
        generated=True,
        attributes={
            "family_id": fid,
            "input_revision": signature,
            "cited_ids": output["evidence_ids"],
        },
    )

    def apply(conn):
        current, _, fresh = summary_snapshot(engine, conn, fid)
        if fresh != signature:
            raise Conflict("Summary input changed during generation")
        previous = current.get("summary", {}).get("record_id")
        engine._insert(conn, record)
        if previous and previous != record.id:
            try:
                old = engine._get(conn, previous)
                old["status"] = "superseded"
                engine._save_revision(conn, old, "replace", "Event summary refreshed")
            except Missing:
                pass
        current["summary"] = {
            "state": "ready",
            "record_id": record.id,
            "input_revision": signature,
        }
        conn.execute("UPDATE families SET data=? WHERE id=?", (dumps(current), fid))

    return apply


def prepare_summary_part(engine, payload):
    from .models import RecordInput

    with engine.db.connect() as conn:
        family, records, signature = summary_snapshot(
            engine, conn, payload["family_id"]
        )
    if signature != payload["input_revision"]:
        raise Conflict("Summary inputs changed")
    with engine.db.connect() as conn:
        inputs = [
            engine._get(conn, rid)
            for rid in dict.fromkeys(p["id"] for p in payload["pieces"])
        ]
    output = summary_output(engine, family["title"], payload["pieces"])
    record = RecordInput(
        id=payload["record_id"],
        kind="summary",
        title=family["title"],
        scope=Scope(**family["scope"]),
        content=output["content"],
        generated=True,
        evidence_ids=[r["id"] for r in inputs],
        source_ids=sorted({sid for r in inputs for sid in r["source_ids"]}),
        attributes={
            "summary_part": True,
            "input_revision": signature,
            "cited_ids": output["evidence_ids"],
        },
    )

    def apply(conn):
        if summary_snapshot(engine, conn, payload["family_id"])[2] != signature:
            raise Conflict("Summary inputs changed during generation")
        for previous in inputs:
            if engine._get(conn, previous["id"])["revision"] != previous["revision"]:
                raise Conflict("Summary part input changed")
        engine._insert(conn, record)

    return apply


def prepare_events(engine, scope):
    """Semantic grouping uses the existing families/members; no second graph store."""
    from .providers import Providers

    with engine.db.connect() as conn:
        records = [
            json.loads(r[0])
            for r in conn.execute(
                "SELECT r.data FROM dirty d JOIN records r ON r.id=d.record_id "
                "WHERE r.scope=? AND r.status='active' AND r.deleted=0 AND r.revision=d.revision "
                "AND json_extract(r.data,'$.generated')=0 ORDER BY r.updated_at,r.id LIMIT 64",
                (scope.key(),),
            )
        ]
        from .read_policy import ReadPolicy

        policy = ReadPolicy.load(engine, scope, "experience_recall", conn=conn)
        records = [r for r in records if policy.visible(r)][:16]
        families = [
            json.loads(r[0])
            for r in conn.execute(
                "SELECT data FROM families WHERE scope=? AND kind='event' AND state NOT IN ('archived','merged') ORDER BY rowid DESC LIMIT 12",
                (scope.key(),),
            )
        ]
    if not records:
        return lambda conn: None
    existing = {f["id"]: f for f in families}
    candidates = []
    with engine.db.connect() as conn:
        for family in families:
            members = []
            for rid in family["members"][-3:]:
                try:
                    r = engine._get(conn, rid)
                    members.append(
                        {
                            "id": rid,
                            "revision": r["revision"],
                            "excerpt": r["content"][:800],
                        }
                    )
                except Missing:
                    pass
            candidates.append(
                {
                    "id": family["id"],
                    "title": family["title"],
                    "revision": family["revision"],
                    "members": members,
                }
            )
    output = Providers(engine).json(
        "organization",
        "Group the supplied work records into concrete events. Reuse a candidate event only when this is the same task or sourced continuation, not merely the same topic. "
        'Return {"events":[{"id":existing_id_or_null,"title":string,"member_ids":[record_id]}]}. Leave uncertain records ungrouped.',
        {
            "records": [
                {
                    "id": r["id"],
                    "revision": r["revision"],
                    "title": r["title"],
                    "excerpt": r["content"][:1600],
                    "partial": len(r["content"]) > 1600,
                    "source_ids": r["source_ids"],
                }
                for r in records
            ],
            "candidate_events": candidates,
        },
    )
    proposals = output.get("events")
    if not isinstance(proposals, list) or len(proposals) > len(records):
        raise ValueError("Invalid event grouping")
    ids = {r["id"] for r in records}
    destinations = [p["id"] for p in proposals if p.get("id")]
    if len(set(destinations)) != len(destinations):
        raise ValueError("An event may be revised once per proposal")
    for proposal in proposals:
        members = proposal.get("member_ids", [])
        if (
            not members
            or not set(members) <= ids
            or (proposal.get("id") and proposal["id"] not in existing)
        ):
            raise ValueError("Event grouping refers outside its input")

    def apply(conn):
        for candidate in candidates:
            for member in candidate["members"]:
                if engine._get(conn, member["id"])["revision"] != member["revision"]:
                    raise Conflict("Candidate event evidence changed during grouping")
        for record in records:
            if engine._get(conn, record["id"])["revision"] != record["revision"]:
                raise Conflict("Event input changed during grouping")
        for proposal in proposals:
            fid = (
                proposal.get("id")
                or "event_" + digest([scope.key(), sorted(proposal["member_ids"])])[:24]
            )
            old = conn.execute(
                "SELECT data FROM families WHERE id=?", (fid,)
            ).fetchone()
            current = json.loads(old[0]) if old else None
            if fid in existing and (
                not current or current["revision"] != existing[fid]["revision"]
            ):
                raise Conflict("Event changed during grouping")
            members = sorted(
                set(proposal["member_ids"]) | set(current["members"] if current else [])
            )
            if len(members) > 1000:
                raise ValueError("Event exceeds 1000 members")
            if current and current["members"] == members:
                continue
            data = {
                "id": fid,
                "scope": scope.model_dump(),
                "kind": "event",
                "state": "candidate",
                "revision": current["revision"] + 1 if current else 1,
                "title": str(proposal["title"])[:1000],
                "members": members,
                "positions": {},
                "basis": "Sourced semantic grouping",
                "summary": {**(current or {}).get("summary", {}), "state": "dirty"},
            }
            conn.execute(
                "INSERT INTO families VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,data=excluded.data",
                (
                    fid,
                    scope.key(),
                    "event",
                    data["state"],
                    data["revision"],
                    dumps(data),
                ),
            )
            conn.execute(
                "INSERT INTO family_revisions VALUES(?,?,?)",
                (fid, data["revision"], dumps(data)),
            )
            for rid in members:
                conn.execute(
                    "INSERT OR IGNORE INTO members VALUES(?,?,?)",
                    (fid, rid, '{"basis":"semantic"}'),
                )
            if current:
                invalidate_summary(engine, conn, fid, "Event membership changed")
        for record in records:
            conn.execute(
                "DELETE FROM dirty WHERE record_id=? AND revision=?",
                (record["id"], record["revision"]),
            )

    return apply


def prepare_procedure(engine, payload):
    """Review a sourced method against actual outcomes using the configured model."""
    from .providers import Providers

    record = engine.get(payload["record_id"])
    if record["revision"] != payload["revision"]:
        raise Conflict("Procedure changed before review")
    with engine.db.connect() as conn:
        relations = [
            dict(r)
            for r in conn.execute(
                "SELECT subject,predicate,object FROM relations WHERE (subject=? OR object=?) AND predicate IN ('verifies','counterexample','refutes')",
                (record["id"], record["id"]),
            )
        ]
        ids = {r[k] for r in relations for k in ("subject", "object")} - {record["id"]}
        evidence = [engine._get(conn, rid) for rid in ids]
    if not evidence:
        return lambda conn: None
    result = Providers(engine).json(
        "organization",
        "Review this method against sourced results and counterexamples. Do not infer that execution succeeded from a plan. "
        'Return {"applicability":string,"limitations":string,"evidence_ids":[record_id]}. Keep uncertainty and environment requirements.',
        {"procedure": record, "relations": relations, "outcomes": evidence},
    )
    if (
        not result.get("applicability")
        or not result.get("evidence_ids")
        or not set(result["evidence_ids"]) <= ids
    ):
        raise ValueError("Procedure review needs cited outcomes")

    def apply(conn):
        for old in [record, *evidence]:
            if engine._get(conn, old["id"])["revision"] != old["revision"]:
                raise Conflict("Procedure evidence changed during review")
        current = engine._get(conn, record["id"])
        current["attributes"].update(
            review_required=False,
            applicability=result["applicability"],
            limitations=result.get("limitations", ""),
            review_evidence=result["evidence_ids"],
            review_basis="model_inference",
        )
        for rid in ids:
            conn.execute(
                "INSERT OR IGNORE INTO dependencies VALUES(?,?)", (record["id"], rid)
            )
        engine._save_revision(
            conn, current, "procedure_review", "Sourced outcome review"
        )

    return apply
