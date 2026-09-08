"""Local callback: uvicorn examples.v1.callback:app --host 127.0.0.1 --port 8320.

Set MEMORY_CALLBACK_DB to a private path. Configure policy.idempotent_channel=true
only when all application effects occur in the DeliveryInbox transaction.
"""

import hashlib, hmac, os
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from eventmem.sdk import DeliveryInbox

app = FastAPI(title="MemoryPalace example callback")
path = Path(
    os.environ.get(
        "MEMORY_CALLBACK_DB",
        str(Path.home() / ".memorypalace" / "example-inbox.sqlite3"),
    )
)
path.parent.mkdir(parents=True, exist_ok=True)
inbox = DeliveryInbox(path)


@app.post("/callback")
async def callback(request: Request):
    body = await request.body()
    secret = os.environ.get("EVENTMEM_WEBHOOK_SECRET")
    if secret and not hmac.compare_digest(
        request.headers.get("X-MemoryPalace-Signature", ""),
        hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
    ):
        raise HTTPException(401, "Invalid signature")
    delivery = __import__("json").loads(body)

    def store(conn, value):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,text TEXT)"
        )
        conn.execute("INSERT INTO messages VALUES(?,?)", (value["id"], value["text"]))

    accepted = inbox.accept(delivery, store)
    return {"delivery_id": delivery["id"], "accepted": accepted}
