"""Local reminder callback that only records synthetic deliveries in SQLite."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

from eventmem.sdk import DeliveryInbox

app = FastAPI(title="MemoryPalace example callback")
db_path = Path(os.environ.get("EVENTMEM_CALLBACK_DB", "./example-callback.sqlite3"))
db_path.parent.mkdir(parents=True, exist_ok=True)
inbox = DeliveryInbox(db_path)


@app.post("/callback")
async def callback(request: Request) -> dict:
    body = await request.body()
    secret = os.environ.get("EVENTMEM_WEBHOOK_SECRET")
    if secret:
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(
            request.headers.get("X-MemoryPalace-Signature", ""), signature
        ):
            raise HTTPException(401, "Invalid signature")
    delivery = json.loads(body)

    def store(conn, value):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS notifications(id TEXT PRIMARY KEY, text TEXT)"
        )
        conn.execute(
            "INSERT INTO notifications(id,text) VALUES(?,?)",
            (value["id"], value["text"]),
        )

    try:
        accepted = inbox.accept(delivery, store)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"delivery_id": delivery["id"], "accepted": accepted}
