"""Classify evidence without treating configuration or synthetic examples as events.

Classification is derived from source metadata and optional host namespace rules.
Audit reads preserve all legacy classes with labels; ordinary recall and automatic
summaries consume experience. No external application or persona files are read.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from typing import NamedTuple

from .models import Scope

PURPOSES = ("experience_recall", "audit")
CLASSES = (
    "experience",
    "role_configuration",
    "synthetic_example",
    "self_knowledge",
    "host_envelope",
)
NON_EXPERIENCE = frozenset(CLASSES) - {"experience"}
# A label, not a class: the owner asking for a configuration is something that happened.
REQUEST_LABEL = "configuration_request"
# Which classes each purpose returns. A class this version does not know is returned to audit only.
ADMITS = {
    "experience_recall": frozenset({"experience"}),
    "audit": frozenset(CLASSES),
}
# When a record's sources disagree about why it is not experience, the first of these names it.
PRECEDENCE = (
    "synthetic_example",
    "role_configuration",
    "host_envelope",
    "self_knowledge",
)

# Bumped whenever the same source would be classified differently. Rows keep the version that
# wrote them, so a migration can find the ones an older rule set produced.
RULES_VERSION = "evidence-classes-v1"
SETTINGS_KEY = "evidence_classes"
STAMP = "origin_kind"
EXAMPLE_FLAGS = ("examples_are_synthetic", "example_kind", "example_count")

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_evidence_class(
 scope TEXT NOT NULL,source_id TEXT NOT NULL,class TEXT NOT NULL,rule TEXT NOT NULL,
 rules_version TEXT NOT NULL,PRIMARY KEY(scope,source_id));
"""

# Rule names are static. They are what a row, a trace and the migration's impact list show.
RULE_SELF_KNOWLEDGE = "self-knowledge-entry"
RULE_FLAG_EXAMPLE = "metadata-synthetic-example"
RULE_FLAG_CONFIGURATION = "metadata-configuration-only"
RULE_DECLARED = "metadata-origin-kind"
RULE_ENVELOPE = "host-envelope-content"
RULE_NAMESPACE = "namespace-registry"
RULE_APPROVED = "persona-approved-source"
RULE_REQUEST = "owner-configuration-request"
RULE_STAMP = "insert-stamp"
# A stored row whose rule starts with this was written by an operator with the text in hand. It
# is the only thing that can hide the owner's own words; every rule above is automatic and the
# classification and the migration never write one.
RULE_OPERATOR = "operator-"

# Namespace -> class for sources the host itself writes there. A trailing `*` is a prefix. The
# owner's own words inside such a namespace stay experience, labelled as a configuration
# request. A host adds its private namespaces with `configure_registry`; none is listed here.
NAMESPACE_REGISTRY = (
    ("role-configuration", "role_configuration"),
    ("role-configuration:*", "role_configuration"),
    ("persona-configuration", "role_configuration"),
    ("persona-configuration:*", "role_configuration"),
    ("synthetic-example", "synthetic_example"),
    ("synthetic-example:*", "synthetic_example"),
)

# Whole-message envelopes a legacy transport stored as owner turns. One definition for every
# reader: recall, the adaptive lanes, the light context path and the event snapshot.
HOST_PREFIXES = (
    "## Memory Writing Agent:",
    "# AGENTS.md instructions",
    "<environment_context>",
    "<turn_aborted>",
    "Warning: Heads up: Long threads",
)

_IDENTIFIER = re.compile(r"\b(?:src|mem)_[0-9a-f]{32}\b")
_cache: dict = {}
_cache_lock = threading.Lock()


def host_envelope(text, prefixes=HOST_PREFIXES):
    """Legacy transport imports sometimes labelled host blocks as user turns.

    Recognize only known whole-message envelopes, never delete source records
    or reinterpret quoted phrases inside an actual conversation.
    """
    return text.lstrip().startswith(prefixes)


def envelope_prefixes(engine, conn=None):
    """The envelope prefixes in force: the built-in ones, plus whatever a host added. The
    built-ins are part of the read policy, so configuration only ever adds to them."""
    if conn is None:
        with engine.db.connect() as connection:
            return envelope_prefixes(engine, connection)
    row = conn.execute(
        "SELECT data FROM settings WHERE key=?", (SETTINGS_KEY,)
    ).fetchone()
    private = ()
    if row:
        try:
            private = json.loads(row[0]).get("envelope_prefixes") or ()
        except (ValueError, AttributeError):
            private = ()
    extra = [p for p in private if isinstance(p, str) and p.strip()]
    return tuple(dict.fromkeys([*HOST_PREFIXES, *extra]))


def configure_envelopes(engine, prefixes):
    """Host-only: the private envelope prefixes, replacing the previous private set. They are a
    settings value, so a rolled back host ignores them, and the built-in list still stands."""
    if (
        not isinstance(prefixes, (list, tuple))
        or len(prefixes) > 32
        or any(
            not isinstance(prefix, str) or not prefix.strip() or len(prefix) > 200
            for prefix in prefixes
        )
    ):
        raise ValueError("Envelope prefixes are up to 32 short whole-message openings")
    stored = engine.settings(SETTINGS_KEY)
    engine.settings(
        SETTINGS_KEY,
        {
            **(stored if isinstance(stored, dict) else {}),
            "envelope_prefixes": list(prefixes),
            "rules_version": RULES_VERSION,
        },
    )
    return envelope_prefixes(engine)


class Found(NamedTuple):
    kind: str
    label: str | None
    rule: str


PLAIN = Found("experience", None, "")


def _matches(namespace, pattern):
    return (
        namespace.startswith(pattern[:-1])
        if pattern.endswith("*")
        else namespace == pattern
    )


def proposed_rules(namespaces):
    """The public rules plus the private ones given here, most specific first: exact names,
    then longer prefixes. A migration's dry run uses this to read as a registry file would."""
    private = namespaces if isinstance(namespaces, dict) else {}
    rules = dict(NAMESPACE_REGISTRY)
    rules.update(
        {
            str(name): kind
            for name, kind in private.items()
            if kind in NON_EXPERIENCE and str(name).strip("*")
        }
    )
    return tuple(
        sorted(
            rules.items(),
            key=lambda rule: (rule[0].endswith("*"), -len(rule[0]), rule[0]),
        )
    )


def registry(engine, conn=None):
    """The namespace rules in force: the public ones above plus the host's private additions."""
    if conn is None:
        with engine.db.connect() as connection:
            return registry(engine, connection)
    row = conn.execute(
        "SELECT data FROM settings WHERE key=?", (SETTINGS_KEY,)
    ).fetchone()
    private = {}
    if row:
        try:
            private = json.loads(row[0]).get("namespaces", {})
        except (ValueError, AttributeError):
            private = {}
    return proposed_rules(private)


def configure_registry(engine, namespaces):
    """Host-only: install the private namespace rules, replacing the previous private set. They
    are a settings value, so a rolled back host ignores them, and writing them moves the
    generation every cached policy depends on."""
    if not isinstance(namespaces, dict) or any(
        not isinstance(name, str) or not name.strip("*") or kind not in NON_EXPERIENCE
        for name, kind in namespaces.items()
    ):
        raise ValueError("Namespace rules map a namespace to a non-experience class")
    stored = engine.settings(SETTINGS_KEY)
    engine.settings(
        SETTINGS_KEY,
        {
            **(stored if isinstance(stored, dict) else {}),
            "namespaces": dict(namespaces),
            "rules_version": RULES_VERSION,
        },
    )
    return registry(engine)


def flagged(attributes):
    """The ad-hoc marks a source's metadata, and so its root record's attributes, may carry.
    They say what the content is about, so they never hold against whoever wrote it."""
    if any(attributes.get(flag) for flag in EXAMPLE_FLAGS):
        return Found("synthetic_example", None, RULE_FLAG_EXAMPLE)
    if attributes.get("configuration_only"):
        return Found("role_configuration", None, RULE_FLAG_CONFIGURATION)
    return None


def source_rule(
    namespace,
    metadata,
    authority,
    *,
    source_id=None,
    approved=(),
    rules=NAMESPACE_REGISTRY,
    text=None,
    prefixes=HOST_PREFIXES,
):
    """Classify one source from what is stored about it. None means plain experience.

    A declaration can only take a source out of experience, never vouch for it: metadata is
    caller-supplied, so `origin_kind: experience` decides nothing. The owner's own explicit turn
    is an event that happened whatever it asked for: a configuration signal only labels it, and
    only a host envelope — by text, by declaration or by namespace — is read before authorship.
    """
    metadata = metadata if isinstance(metadata, dict) else {}
    if text and host_envelope(text, prefixes):
        return Found("host_envelope", None, RULE_ENVELOPE)
    declared = metadata.get(STAMP)
    declared = (
        declared if isinstance(declared, str) and declared in NON_EXPERIENCE else None
    )
    found = flagged(metadata)
    kind = next(
        (value for pattern, value in rules if _matches(namespace, pattern)), None
    )
    rule = RULE_NAMESPACE
    if kind is None and source_id is not None and source_id in approved:
        kind, rule = "role_configuration", RULE_APPROVED
    if (
        "host_envelope" not in (declared, kind)
        and authority == "explicit"
        and metadata.get("role") == "user"
    ):
        return (
            Found("experience", REQUEST_LABEL, RULE_REQUEST)
            if declared or found or kind
            else None
        )
    if declared:
        return Found(declared, None, RULE_DECLARED)
    if found:
        return found
    return Found(kind, None, rule) if kind is not None else None


def _shared(scope):
    return Scope(project="*", persona="*", collection="preferences", world=scope.world)


def _source_found(row, approved, rules, text=None, prefixes=HOST_PREFIXES):
    data = json.loads(row["data"])
    return source_rule(
        row["namespace"],
        data.get("metadata"),
        data.get("authority"),
        source_id=row["id"],
        approved=approved,
        rules=rules,
        text=text,
        prefixes=prefixes,
    )


class _Snapshot(NamedTuple):
    rows: dict  # source id -> Found, as stored in source_evidence_class
    derived: dict  # source id -> Found, for registered or approved sources that have no row yet
    rules: tuple
    approved: frozenset
    prefixes: tuple = (
        HOST_PREFIXES  # the built-in envelope openings plus the host's own
    )


def _load_snapshot(engine, conn, scope, rules=None):
    scopes = [scope.key(), _shared(scope).key()]
    stored = {}
    try:
        for row in conn.execute(
            "SELECT source_id,class,rule FROM source_evidence_class WHERE scope IN (?,?)",
            scopes,
        ):
            stored[row["source_id"]] = Found(
                row["class"],
                REQUEST_LABEL if row["rule"] == RULE_REQUEST else None,
                row["rule"],
            )
    except sqlite3.OperationalError:
        # A database this schema has not reached: every source is classified on the fly.
        pass
    rules = registry(engine, conn) if rules is None else rules
    approved = frozenset()
    sources = {}
    # One lookup on the namespace index for the whole registry. `+scope` keeps the planner off
    # the scope index, which would walk every source of the scope for a handful of names.
    names = [pattern for pattern, _ in rules if not pattern.endswith("*")]
    prefixes = [pattern[:-1] for pattern, _ in rules if pattern.endswith("*")]
    clauses = (
        ["namespace IN (" + ",".join("?" for _ in names) + ")"] if names else []
    ) + ["(namespace>=? AND namespace<?)" for _ in prefixes]
    if clauses:
        values = names + [
            bound for prefix in prefixes for bound in (prefix, prefix + "\U0010ffff")
        ]
        for row in conn.execute(
            "SELECT id,namespace,data FROM sources WHERE ("
            + " OR ".join(clauses)
            + ") AND +scope IN (?,?) AND deleted=0",
            values + scopes,
        ):
            if row["id"] not in stored:
                found = _source_found(row, approved, rules)
                if found:
                    sources[row["id"]] = found
    return _Snapshot(stored, sources, rules, approved, envelope_prefixes(engine, conn))


def _snapshot(engine, conn, scope):
    try:
        inode = os.stat(engine.db.path).st_ino
    except OSError:
        inode = None
    if conn.total_changes:
        # This connection has written. What it sees may never be committed, so it is neither
        # served from the cache nor allowed to fill it.
        return _load_snapshot(engine, conn, scope)
    key = (str(engine.db.path), inode, scope.key())
    version = engine.db.generation(conn)
    with _cache_lock:
        cached = _cache.get(key)
    if cached and cached[0] == version:
        return cached[1]
    value = _load_snapshot(engine, conn, scope)
    with _cache_lock:
        if len(_cache) >= 64:
            _cache.clear()
        _cache[key] = (version, value)
    return value


class ReadPolicy:
    """One read's view of the classification. Immutable after load, so a recall may hand the
    same policy to its candidate threads."""

    def __init__(self, purpose, enabled, snapshot, state=None):
        if purpose not in PURPOSES:
            raise ValueError("Unknown recall purpose")
        self.purpose, self.enabled = purpose, bool(enabled)
        self.rules_version = RULES_VERSION
        self._snapshot = snapshot
        self._rows, self._derived = snapshot.rows, snapshot.derived
        self._admitted = ADMITS[purpose]

    @classmethod
    def load(cls, engine, scope, purpose="experience_recall", *, conn=None):
        """Read classifications cached by the current database generation."""
        if purpose not in PURPOSES:
            raise ValueError("Unknown recall purpose")
        if conn is None:
            with engine.db.connect() as connection:
                return cls.load(engine, scope, purpose, conn=connection)
        scope = scope if isinstance(scope, Scope) else Scope.model_validate(scope)
        return cls(purpose, True, _snapshot(engine, conn, scope), None)

    @property
    def prefixes(self):
        """The envelope openings this read filters by: the built-in ones plus the host's own."""
        return self._snapshot.prefixes

    def envelope(self, text):
        """One reader for every lane that still filters by prefix rather than by class."""
        return host_envelope(text or "", self._snapshot.prefixes)

    def admits(self, kind):
        return not self.enabled or kind in self._admitted or self.purpose == "audit"

    def source_class(self, source_id, namespace=None, metadata=None, authority=None):
        """One source. With only an id, a source outside the loaded rules reads as experience."""
        if not self.enabled:
            return PLAIN
        found = self._rows.get(source_id) or self._derived.get(source_id)
        if found is None and namespace is not None:
            found = source_rule(
                namespace,
                metadata,
                authority,
                source_id=source_id,
                approved=self._snapshot.approved,
                rules=self._snapshot.rules,
            )
        return found or PLAIN

    @staticmethod
    def _combine(found):
        """Not experience only when nothing behind it is. A label only when no plain experience is."""
        if not found:
            return PLAIN
        if len(found) == 1:
            return found[0]
        outside = [f for f in found if f.kind != "experience"]
        if len(outside) == len(found):
            first = next(
                (k for k in PRECEDENCE if any(f.kind == k for f in outside)),
                outside[0].kind,
            )
            return next(f for f in outside if f.kind == first)
        if all(f.label for f in found if f.kind == "experience"):
            return next(f for f in found if f.label)
        return PLAIN

    def classify(self, record):
        """In the order of the module's first paragraph. A claim's own sources are the owner's
        words, so being a self-knowledge entry is asked first; then an operator's row, the one
        review a person wrote; then who said it, which nothing stored about a source outranks."""
        if not self.enabled:
            return PLAIN
        attributes = record.get("attributes") or {}
        if attributes.get("self_knowledge"):
            return Found("self_knowledge", None, RULE_SELF_KNOWLEDGE)
        sources = record.get("source_ids") or ()
        rows = [self._rows.get(sid) for sid in sources]
        behind = [row or self._derived.get(sid) for row, sid in zip(rows, sources)]
        reviewed = next(
            (f for f in behind if f and f.rule.startswith(RULE_OPERATOR)), None
        )
        if reviewed:
            return reviewed
        # A host prompt is stored as an owner turn, so its text is read before the flags and
        # before who is recorded as having written it.
        if self.envelope(record.get("content")):
            return Found("host_envelope", None, RULE_ENVELOPE)
        stamp = attributes.get(STAMP)
        stamp = stamp if isinstance(stamp, str) else None
        if attributes.get("role") == "user" and not record.get("generated"):
            # An event that happened, whatever it asked for. Every signal here only labels it.
            asked = (
                flagged(attributes)
                or stamp in NON_EXPERIENCE
                or stamp == REQUEST_LABEL
                or any(f and (f.kind != "experience" or f.label) for f in behind)
            )
            return Found("experience", REQUEST_LABEL, RULE_REQUEST) if asked else PLAIN
        # Not the owner's own words: the order these rules have always had. A row saying only
        # that the source's root record was an owner turn decides nothing about this record.
        if any(f and f.rule != RULE_REQUEST for f in rows):
            return self._combine([f or PLAIN for f in behind])
        found = flagged(attributes)
        if found:
            return found
        if any(f and f.rule != RULE_REQUEST for f in behind):
            return self._combine([f or PLAIN for f in behind])
        if stamp in NON_EXPERIENCE:
            return Found(stamp, None, RULE_STAMP)
        if stamp == REQUEST_LABEL:
            return Found("experience", REQUEST_LABEL, RULE_STAMP)
        return next((f for f in behind if f), PLAIN)

    def refusal(self, record, history=False):
        """Why this read may not return the record, or None. With the switch off this is the one
        rule that existed before: self-knowledge needs `history`."""
        if not self.enabled:
            if (record.get("attributes") or {}).get("self_knowledge") and not history:
                return "self_knowledge_requires_versioned_view"
            return None
        kind = self.classify(record).kind
        if kind in self._admitted or self.purpose == "audit":
            return None
        return (
            "self_knowledge_requires_versioned_view"
            if kind == "self_knowledge"
            else "not_experience:" + kind
        )

    def visible(self, record, history=False):
        return self.refusal(record, history) is None

    def label(self, record):
        """What a reader is told besides the text: the class of what is not experience, the
        request label of an owner configuration request, None for plain experience."""
        found = self.classify(record)
        return found.kind if found.kind != "experience" else found.label

    def basis(self, record):
        """The epistemic basis shown for a record: never `explicit` for what is not experience."""
        found = self.classify(record)
        return found.kind if found.kind != "experience" else record["confirmation"]

    def present(self, record, view):
        """Label one outgoing copy of a record. The stored record is never touched."""
        found = self.classify(record)
        if found.kind != "experience":
            view["evidence_class"] = found.kind
            if "confirmation" in view:
                view["confirmation"] = found.kind
        elif found.label:
            view["evidence_label"] = found.label
        return view

    def present_source(self, source, text=None):
        """Label one outgoing copy of a source. `text` is its root record's content when the
        caller has it: an envelope shows in the text, not in the namespace or the metadata."""
        found = self.source_class(
            source["id"],
            source.get("namespace"),
            source.get("metadata"),
            source.get("authority"),
        )
        if self.enabled and found is PLAIN and text and host_envelope(text):
            found = Found("host_envelope", None, RULE_ENVELOPE)
        if found.kind != "experience":
            source.update(evidence_class=found.kind, basis=found.kind)
        elif found.label:
            source["evidence_label"] = found.label
        return source

    def prefix(self, record):
        """The bracket `recall()` puts before a line, after the id and status bracket."""
        info = (record.get("attributes") or {}).get("self_knowledge")
        if not self.enabled:
            if not info:
                return ""
            label = info.get("basis", info.get("entry", "self_knowledge"))
            return f"[{label} {record['confirmation']} {info.get('agent_version', 'unknown')}] "
        found = self.classify(record)
        if found.kind == "experience":
            return f"[{found.label}] " if found.label else ""
        if info:
            return f"[{found.kind} {info.get('basis', info.get('entry', 'self_knowledge'))} {info.get('agent_version', 'unknown')}] "
        return f"[{found.kind}] "

    def node_class(self, node, records=None):
        """A graph node or edge. `records` maps the ids of the records it projects or cites to
        those records, when the caller has them: a projected self-claim shows only there, because
        the claim's own sources are the owner's words. Without them the stored evidence refs
        decide, which carry namespace and metadata. Hidden only when nothing behind it is experience."""
        if not self.enabled:
            return PLAIN
        found = []
        if records:
            ids = dict.fromkeys(
                [
                    *node.get("record_ids", []),
                    *[r["record_id"] for r in node.get("evidence", [])],
                    *(
                        [node["id"]]
                        if str(node.get("id", "")).startswith("mem_")
                        else []
                    ),
                ]
            )
            found = [self.classify(records[i]) for i in ids if i in records]
        if not found:
            found = [
                self.source_class(
                    r["source_id"],
                    r.get("namespace"),
                    r.get("metadata"),
                    r.get("authority"),
                )
                for r in node.get("evidence", [])
            ]
        return self._combine(found)

    def node_visible(self, node, records=None):
        if not self.enabled:
            return True
        kind = self.node_class(node, records).kind
        return kind in self._admitted or self.purpose == "audit"

    def node_label(self, node, records=None):
        found = self.node_class(node, records)
        return found.kind if found.kind != "experience" else found.label


def stamp_source(engine, conn, sid, source, text):
    """Classify a source as it is received, inside the receipt's transaction. The row and the
    stamp belong to the first revision, so nothing that exists is rewritten. Returns the value
    for the root record's `origin_kind`, or None. A receipt never fails over its label."""
    try:
        found = source_rule(
            source.namespace,
            source.metadata,
            source.authority,
            source_id=sid,
            approved=frozenset(),
            rules=registry(engine, conn),
            text=text,
            prefixes=envelope_prefixes(engine, conn),
        )
        if not found:
            return None
        conn.execute(
            "INSERT OR REPLACE INTO source_evidence_class VALUES(?,?,?,?,?)",
            (source.scope.key(), sid, found.kind, found.rule, RULES_VERSION),
        )
        return found.label or found.kind
    except sqlite3.Error:
        return None


def proposed_policy(
    engine, conn, scope, rows, purpose="experience_recall", *, rules=None, state=None
):
    """A policy that reads as if `rows` were already stored, over the rules given or installed.
    The migration's dry run needs both: it may not write its rows, nor install its registry."""
    live = _load_snapshot(engine, conn, scope, rules)
    stored = dict(live.rows)
    for row in rows:
        stored[row["source_id"]] = Found(
            row["class"],
            REQUEST_LABEL if row["rule"] == RULE_REQUEST else None,
            row["rule"],
        )
    return ReadPolicy(
        purpose,
        True,
        _Snapshot(stored, live.derived, live.rules, live.approved, live.prefixes),
        state,
    )


def classify_scope(engine, conn, scope, *, rules=None):
    """Every source of a scope the rules take out of plain experience, for the migration that
    fills the table: [{source_id, namespace, class, rule, rules_version}]. Reads only. The root
    record supplies the text for the envelope rule; ids and static names, never content."""
    from .db import digest

    rules = registry(engine, conn) if rules is None else rules
    approved, proposed = frozenset(), []
    for row in conn.execute(
        "SELECT id,namespace,data FROM sources WHERE scope=? AND deleted=0 ORDER BY id",
        (scope.key(),),
    ).fetchall():
        root = conn.execute(
            "SELECT data FROM records WHERE id=? AND deleted=0",
            ("mem_" + digest([row["id"], "root"])[:32],),
        ).fetchone()
        found = _source_found(
            row, approved, rules, text=json.loads(root[0])["content"] if root else None
        )
        if found:
            proposed.append(
                {
                    "source_id": row["id"],
                    "namespace": row["namespace"],
                    "class": found.kind,
                    "rule": found.rule,
                    "rules_version": RULES_VERSION,
                }
            )
    return proposed


def record_classes(engine, conn, scope, rows):
    """Write classification rows inside the caller's write transaction and move the generation,
    so every cached policy reloads. Idempotent: the same rows leave the same table."""
    for row in rows:
        if row["class"] not in CLASSES:
            raise ValueError("Unknown evidence class")
        conn.execute(
            "INSERT OR REPLACE INTO source_evidence_class VALUES(?,?,?,?,?)",
            (
                scope.key(),
                row["source_id"],
                row["class"],
                row["rule"],
                row.get("rules_version", RULES_VERSION),
            ),
        )
    engine.db.bump(conn)
    return len(rows)
