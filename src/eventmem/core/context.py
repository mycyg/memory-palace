"""Context delivery receipts stored with the existing host session.

Preparing text is not delivery. Only the host's receipt for the exact body can
consume a window budget. Compression completion starts a new window once.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field

from .db import Conflict, Missing, digest, dumps
from .models import Model, Scope


class ContextReceipt(Model):
    session: str
    scope: Scope = Field(default_factory=Scope)
    delivery_id: str
    body_hash: str
    state: Literal["sending", "unconfirmed", "accepted", "discarded"]


def session_state(conn, session, scope):
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (session,)).fetchone()
    if row and row["scope"] != scope.key():
        raise Conflict("Session belongs to another scope")
    return {
        "seen": {},
        "resident": {},
        "used": 0,
        "epoch": 0,
        "deliveries": {},
        **(json.loads(row["data"]) if row else {}),
    }


def save_session(conn, session, scope, state):
    conn.execute(
        "INSERT INTO sessions VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
        (session, scope.key(), dumps(state)),
    )


def prepare_context(state, request, text, items, token_count):
    epoch = state["epoch"]
    body_hash = digest(text.encode())
    state["delivery_sequence"] = state.get("delivery_sequence", 0) + 1
    # Repeated text is a new operation, not a reason to reopen an accepted receipt.
    identity = (
        "ctx_"
        + digest(
            [
                request.session,
                epoch,
                state["delivery_sequence"],
                request.host_mode,
                body_hash,
            ]
        )[:32]
    )
    receipt = {
        "id": identity,
        "body_hash": body_hash,
        "epoch": epoch,
        "state": "prepared",
    }
    state["deliveries"][identity] = {
        **receipt,
        "sequence": state["delivery_sequence"],
        "tokens": token_count,
        "records": {r["id"]: r["revision"] for r in items},
        "host_mode": request.host_mode,
    }
    # Retain uncertain receipts; completed windows need only bounded dedup history.
    settled = [
        key
        for key, value in state["deliveries"].items()
        if value["state"] in {"accepted", "discarded"}
    ]
    for key in settled[:-32]:
        del state["deliveries"][key]
    return receipt


def settle_context(engine, request: ContextReceipt):
    with engine.db.connect(write=True) as conn:
        state = session_state(conn, request.session, request.scope)
        delivery = state["deliveries"].get(request.delivery_id)
        if not delivery:
            raise Missing("Context delivery")
        if delivery["body_hash"] != request.body_hash:
            raise Conflict("Receipt body differs from prepared context")
        if delivery["state"] in {"accepted", "discarded"}:
            return {
                "id": request.delivery_id,
                "state": delivery["state"],
                "epoch": delivery["epoch"],
                "session_used": state["used"],
            }
        if request.state == "accepted" and delivery["epoch"] == state["epoch"]:
            if delivery["host_mode"] == "replace":
                if delivery.get("sequence", 0) >= state.get("last_replace_sequence", 0):
                    state["resident"] = delivery["records"]
                    state["used"] = delivery["tokens"]
                    state["last_replace_sequence"] = delivery.get("sequence", 0)
            else:
                for rid, revision in delivery["records"].items():
                    state["resident"][rid] = max(
                        revision, state["resident"].get(rid, 0)
                    )
                state["used"] += delivery["tokens"]
            for rid, revision in delivery["records"].items():
                state["seen"][rid] = max(revision, state["seen"].get(rid, 0))
        delivery["state"] = request.state
        save_session(conn, request.session, request.scope, state)
        return {
            "id": request.delivery_id,
            "state": delivery["state"],
            "epoch": delivery["epoch"],
            "session_used": state["used"],
        }


def complete_compaction(engine, session, scope, command_id):
    with engine.db.connect(write=True) as conn:

        def apply():
            state = session_state(conn, session, scope)
            state.update(epoch=state["epoch"] + 1, used=0, seen={}, resident={})
            save_session(conn, session, scope, state)
            return {"session": session, "epoch": state["epoch"]}

        return engine.command(
            conn,
            "compaction:" + command_id,
            {"session": session, "scope": scope.model_dump()},
            apply,
        )
