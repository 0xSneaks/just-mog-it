"""Offline tests for the Telegram launch announcer (api/telegram_launch.py).

All network surfaces (Solana RPC, Vercel Blob, Telegram) are faked.
Run: python -m pytest tests/test_telegram_launch.py -q
"""

import importlib.util
import json
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location(
    "telegram_launch", os.path.join(HERE, "..", "api", "telegram_launch.py")
)
tl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tl)

MINT = "3cHj1iCgZb18Q8ZrqbsAtwNGjn6gifQRqnCedHhdmogz"
SECRET = "test-secret-value"

ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:test-token",
    "TELEGRAM_CHANNEL_ID": "@justmogit_test_public",
    "TELEGRAM_TEST_CHANNEL_ID": "-1001234567890",
    "LAUNCH_POST_SECRET": SECRET,
    "BLOB_READ_WRITE_TOKEN": "vercel_blob_rw_test",
    "SOLANA_RPC_URL": "https://rpc.example.invalid",
}

PAYLOAD = {
    "mint": MINT,
    "name": "Jose",
    "ticker": "JOSE",
    "venue": "pumpfun",
    "image_url": "https://example.com/jose.png",
    "original": {"name": "Jose Original", "ticker": "JOSEOG"},
    "channel": "test",
}


class FakeTransport:
    """Routes http_json calls by URL substring; records everything."""

    def __init__(self, *, mint_on_chain=True, registry=None, telegram_ok=True):
        self.calls = []
        self.mint_on_chain = mint_on_chain
        self.registry = list(registry or [])
        self.telegram_ok = telegram_ok

    def __call__(self, method, url, payload=None, headers=None, timeout=20):
        self.calls.append({"method": method, "url": url, "payload": payload})
        if "rpc.example" in url:
            value = (
                {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}
                if self.mint_on_chain
                else None
            )
            return 200, {"result": {"value": value}}
        if "blob.vercel-storage.com" in url and method == "GET":
            if self.registry:
                return 200, {
                    "blobs": [
                        {
                            "pathname": tl.REGISTRY_PATHNAME,
                            "url": "https://store.public.blob.vercel-storage.com/"
                            + tl.REGISTRY_PATHNAME,
                        }
                    ]
                }
            return 200, {"blobs": []}
        if "api.telegram.org" in url:
            if self.telegram_ok:
                return 200, {"ok": True, "result": {"message_id": 4242}}
            return 403, {"ok": False, "description": "forbidden"}
        raise AssertionError("unexpected URL: " + url)

    def telegram_calls(self):
        return [c for c in self.calls if "api.telegram.org" in c["url"]]


def fake_get_text_factory(transport):
    def _get_text(url, timeout=20):
        return 200, json.dumps({"mints": transport.registry})

    return _get_text


def run(transport, *, headers=None, body=None, env=None):
    headers = headers if headers is not None else {"x-launch-secret": SECRET}
    body = body if body is not None else PAYLOAD
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return tl.process_request(
        headers,
        raw,
        dict(env if env is not None else ENV),
        transport=transport,
        get_text=fake_get_text_factory(transport),
    )


def test_bad_secret_rejected_and_nothing_posted():
    t = FakeTransport()
    status, resp = run(t, headers={"x-launch-secret": "wrong"})
    assert status == 401
    assert resp["error"] == "unauthorized"
    assert t.telegram_calls() == []


def test_missing_secret_header_rejected():
    t = FakeTransport()
    status, _ = run(t, headers={})
    assert status == 401
    assert t.telegram_calls() == []


def test_unknown_mint_rejected_and_nothing_posted():
    t = FakeTransport(mint_on_chain=False)
    status, resp = run(t)
    assert status == 422
    assert resp["error"] == "mint_not_found_on_chain"
    assert t.telegram_calls() == []


def test_repeat_mint_blocked():
    t = FakeTransport(registry=[MINT])
    status, resp = run(t)
    assert status == 409
    assert resp["error"] == "already_posted"
    assert t.telegram_calls() == []


def test_missing_config_fails_closed(monkeypatch):
    env = dict(ENV)
    del env["BLOB_READ_WRITE_TOKEN"]
    t = FakeTransport()
    status, resp = run(t, env=env)
    assert status == 503
    assert "BLOB_READ_WRITE_TOKEN" in resp["missing"]
    assert t.telegram_calls() == []


def test_happy_path_posts_facts_only_to_test_channel(monkeypatch):
    written = {}
    monkeypatch.setattr(
        tl, "registry_write", lambda env, mints: written.update(mints=mints) or True
    )
    t = FakeTransport()
    status, resp = run(t)
    assert status == 200
    assert resp["posted"] is True
    assert resp["channel"] == "test"
    calls = t.telegram_calls()
    assert len(calls) == 1
    sent = calls[0]["payload"]
    assert sent["chat_id"] == ENV["TELEGRAM_TEST_CHANNEL_ID"]
    caption = sent["caption"]
    for needle in ("Jose", "$JOSE", MINT, "pump.fun", "Jose Original", "$JOSEOG"):
        assert needle in caption
    for banned in ("100x", "buy now", "guaranteed", "moon", "profit"):
        assert banned not in caption.lower()
    assert written["mints"] == [MINT]


def test_public_channel_requires_explicit_selection():
    t = FakeTransport()
    body = dict(PAYLOAD)
    del body["channel"]
    monkeypatch = pytest.MonkeyPatch.context()
    with monkeypatch as mp:
        mp.setattr(tl, "registry_write", lambda env, mints: True)
        status, resp = run(t, body=body)
    assert status == 200
    assert resp["channel"] == "public"
    assert t.telegram_calls()[0]["payload"]["chat_id"] == ENV["TELEGRAM_CHANNEL_ID"]


def test_float_without_venue_url_rejected():
    t = FakeTransport()
    body = dict(PAYLOAD, venue="float")
    status, resp = run(t, body=body)
    assert status == 400
    assert "venue_url" in resp["error"]
    assert t.telegram_calls() == []


def test_invalid_json_rejected():
    t = FakeTransport()
    status, resp = run(t, body=b"{not json")
    assert status == 400
    assert t.telegram_calls() == []
