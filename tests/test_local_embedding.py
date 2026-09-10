import os
import signal
import socket
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from eventmem.core.local_embedding import MODEL, create_app, ensure_started, token


def test_lazy_load_auth_dimensions_and_failed_inference_reloads(tmp_path):
    np = pytest.importorskip("numpy")
    loads = []

    class Model:
        def encode(self, texts, **kwargs):
            if texts == ["fail"]:
                raise RuntimeError("private input and secrets must not escape")
            return np.ones((len(texts), 1024))

    def load():
        loads.append(True)
        return Model()

    client = TestClient(create_app(tmp_path, load))
    assert client.get("/health").status_code == 401
    client.headers["Authorization"] = "Bearer " + token(tmp_path)
    assert not client.get("/health").json()["loaded"]
    assert not loads
    assert (
        client.post(
            "/v1/embeddings", json={"model": MODEL, "input": ["a"], "dimensions": 16}
        ).status_code
        == 422
    )
    response = client.post(
        "/v1/embeddings", json={"model": MODEL, "input": ["a", "b"], "dimensions": 256}
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert [item["index"] for item in data] == [0, 1]
    assert len(data[0]["embedding"]) == 256
    assert np.linalg.norm(data[0]["embedding"]) == pytest.approx(1)
    bad = client.post("/v1/embeddings", json={"model": MODEL, "input": ["fail"]})
    assert bad.status_code == 503
    assert "private" not in bad.text
    assert (
        client.post("/v1/embeddings", json={"model": MODEL, "input": ["a"]}).status_code
        == 200
    )
    assert len(loads) == 2


def test_wakeup_rejects_remote_endpoints(tmp_path):
    for endpoint in [
        "https://example.com/v1",
        "http://127.0.0.1/v1?x=1",
        "http://user@127.0.0.1/v1",
    ]:
        with pytest.raises(ValueError):
            ensure_started(tmp_path, endpoint, MODEL)


def test_concurrent_wakeup_and_process_crash_recovery(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}/v1"
    pids = set()
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            tokens = list(
                pool.map(lambda _: ensure_started(tmp_path, endpoint, MODEL), range(4))
            )
        assert len(set(tokens)) == 1
        with httpx.Client(
            trust_env=False, headers={"Authorization": "Bearer " + tokens[0]}
        ) as client:
            health = lambda: client.get(f"http://127.0.0.1:{port}/health").json()
            first = health()["pid"]
            pids.add(first)
            assert not health()["loaded"]
            os.kill(first, signal.SIGKILL)
            # Wait for the socket to close without relying on a fixed startup delay.
            import time

            for _ in range(100):
                try:
                    health()
                except httpx.TransportError:
                    break
                time.sleep(0.02)
            ensure_started(tmp_path, endpoint, MODEL)
            second = health()["pid"]
            pids.add(second)
            assert second != first
    finally:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("failure", ["connection", "inference"])
def test_provider_retries_idempotent_embedding_once(tmp_path, monkeypatch, failure):
    from eventmem.core import Engine, local_embedding
    from eventmem.core.providers import Providers

    engine = Engine(tmp_path)
    engine.settings(
        "models",
        {
            "embedding": {
                "endpoint": "http://127.0.0.1:8321/v1",
                "model": MODEL,
                "local_embedding": True,
            }
        },
    )
    wakes = []
    calls = []
    monkeypatch.setattr(
        local_embedding, "ensure_started", lambda *args: wakes.append(1) or "test-token"
    )

    def respond(request):
        calls.append(1)
        if len(calls) == 1:
            if failure == "connection":
                raise httpx.ReadError("socket closed")
            return httpx.Response(503, json={"detail": "model unavailable"})
        return httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]}
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    result = Providers(engine).request(
        "embedding", "embeddings", json_={"model": MODEL, "input": ["test"]}
    )
    assert result["data"] and len(calls) == 2
    assert len(wakes) == (2 if failure == "connection" else 1)
