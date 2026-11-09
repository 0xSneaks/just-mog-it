"""Just Mog It — Telegram launch announcer (Vercel Python serverless function).

Posts exactly one message per confirmed launch to the Just Mog It Telegram
channel. Internal callers only: the launch pipeline authenticates with the
LAUNCH_POST_SECRET env var (header ``x-launch-secret``). There is no
user-triggered path that can post.

Safety properties:
- Fails closed. Missing config, unverified mint, or unavailable dedupe
  store => no post.
- The mint is confirmed on-chain (account exists and is owned by the SPL
  Token or Token-2022 program) before anything is posted.
- Dedupe registry lives in Vercel Blob (``telegram/posted-mints.json``);
  a mint already in the registry is rejected and never posted twice.
- Facts only. The caption carries the coin name, ticker, contract address,
  venue link, and the original coin it relaunches. No price claims, no
  calls to action, no promises.

Env vars (set on the Vercel project, never committed):
    TELEGRAM_BOT_TOKEN        Bot API token for @Justmogit_bot
    TELEGRAM_CHANNEL_ID       public channel, e.g. @justmogit
    TELEGRAM_TEST_CHANNEL_ID  private test channel id, e.g. -100xxxxxxxxxx
    LAUNCH_POST_SECRET        shared secret the pipeline must present
    BLOB_READ_WRITE_TOKEN     Vercel Blob token backing the dedupe registry
    SOLANA_RPC_URL            optional; defaults to public mainnet RPC

Request:  POST /api/telegram_launch
Headers:  x-launch-secret: <LAUNCH_POST_SECRET>
Body (JSON):
    {
      "mint": "<base58 mint address>",
      "name": "Coin Name",
      "ticker": "TICKER",
      "venue": "pumpfun" | "float",
      "venue_url": "https://..."        (optional for pumpfun, required for float),
      "image_url": "https://..."        (coin image),
      "original": {"name": "...", "ticker": "..."},
      "channel": "public" | "test"      (optional, default "public")
    }

Responses: 200 posted · 400 bad payload · 401 bad secret · 405 wrong method
           409 already posted · 422 mint not found on-chain · 503 not configured
"""

import hmac
import json
import os
import re
import urllib.request
from http.server import BaseHTTPRequestHandler

TOKEN_PROGRAMS = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   # SPL Token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",   # Token-2022
}
DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"
REGISTRY_PATHNAME = "telegram/posted-mints.json"
BLOB_API = "https://blob.vercel-storage.com"
MAX_BODY_BYTES = 16384
BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

REQUIRED_ENV = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHANNEL_ID",
    "TELEGRAM_TEST_CHANNEL_ID",
    "LAUNCH_POST_SECRET",
    "BLOB_READ_WRITE_TOKEN",
)


# --------------------------------------------------------------------------
# transport (injectable for tests)
# --------------------------------------------------------------------------

def http_json(method, url, payload=None, headers=None, timeout=20):
    """One HTTP call returning (status, parsed_json_or_text)."""
    data = None
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    if payload is not None:
        data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def http_get_text(url, timeout=20):
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


# --------------------------------------------------------------------------
# core logic
# --------------------------------------------------------------------------

def missing_config(env):
    return [name for name in REQUIRED_ENV if not env.get(name)]


def secret_ok(headers, env):
    presented = headers.get("x-launch-secret") or headers.get("X-Launch-Secret")
    expected = env.get("LAUNCH_POST_SECRET")
    if not presented or not expected:
        return False
    return hmac.compare_digest(str(presented), str(expected))


def validate_payload(body):
    """Return (fields, error). error is None on success."""
    if not isinstance(body, dict):
        return None, "body must be a JSON object"
    mint = str(body.get("mint") or "").strip()
    if not BASE58_RE.match(mint):
        return None, "mint must be a base58 address"
    name = str(body.get("name") or "").strip()
    ticker = str(body.get("ticker") or "").strip().lstrip("$")
    if not name or not ticker:
        return None, "name and ticker are required"
    venue = str(body.get("venue") or "").strip().lower()
    if venue not in ("pumpfun", "float"):
        return None, "venue must be 'pumpfun' or 'float'"
    venue_url = str(body.get("venue_url") or "").strip()
    if venue == "pumpfun" and not venue_url:
        venue_url = "https://pump.fun/coin/" + mint
    if venue == "float" and not venue_url:
        return None, "venue_url is required for float launches"
    if not venue_url.startswith("https://"):
        return None, "venue_url must be https"
    image_url = str(body.get("image_url") or "").strip()
    if not image_url.startswith("https://"):
        return None, "image_url must be an https URL"
    original = body.get("original") or {}
    orig_name = str(original.get("name") or "").strip()
    orig_ticker = str(original.get("ticker") or "").strip().lstrip("$")
    if not orig_name or not orig_ticker:
        return None, "original.name and original.ticker are required"
    channel = str(body.get("channel") or "public").strip().lower()
    if channel not in ("public", "test"):
        return None, "channel must be 'public' or 'test'"
    return {
        "mint": mint,
        "name": name,
        "ticker": ticker,
        "venue": venue,
        "venue_url": venue_url,
        "image_url": image_url,
        "original_name": orig_name,
        "original_ticker": orig_ticker,
        "channel": channel,
    }, None


def mint_exists_on_chain(mint, env, transport=http_json):
    """True only if the account exists and is a token-program mint account."""
    rpc_url = env.get("SOLANA_RPC_URL") or DEFAULT_RPC_URL
    status, resp = transport(
        "POST",
        rpc_url,
        payload={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [mint, {"encoding": "jsonParsed"}],
        },
    )
    if status != 200 or not isinstance(resp, dict):
        return False
    value = (resp.get("result") or {}).get("value")
    if not value:
        return False
    return value.get("owner") in TOKEN_PROGRAMS


def registry_read(env, transport=http_json, get_text=http_get_text):
    """Return the list of posted mints, or None if the store is unusable."""
    token = env.get("BLOB_READ_WRITE_TOKEN")
    if not token:
        return None
    status, resp = transport(
        "GET",
        BLOB_API + "/?prefix=" + REGISTRY_PATHNAME,
        headers={"Authorization": "Bearer " + token},
    )
    if status != 200 or not isinstance(resp, dict):
        return None
    blobs = resp.get("blobs") or []
    match = [b for b in blobs if b.get("pathname") == REGISTRY_PATHNAME]
    if not match:
        return []
    try:
        _, text = get_text(match[0]["url"])
        data = json.loads(text)
        return list(data.get("mints") or [])
    except Exception:
        return None


def registry_write(env, mints, transport=None):
    """Persist the registry. Returns True on success."""
    token = env.get("BLOB_READ_WRITE_TOKEN")
    if not token:
        return False
    body = json.dumps({"mints": mints}).encode()
    req = urllib.request.Request(
        BLOB_API + "/" + REGISTRY_PATHNAME,
        data=body,
        method="PUT",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "x-add-random-suffix": "0",
            "x-content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status == 200
    except Exception:
        return False


def build_caption(f):
    venue_label = "pump.fun" if f["venue"] == "pumpfun" else "Float"
    lines = [
        "New launch on Just Mog It",
        "",
        "%s ($%s)" % (f["name"], f["ticker"]),
        "CA: %s" % f["mint"],
        "Venue: %s — %s" % (venue_label, f["venue_url"]),
        "Relaunch of: %s ($%s)" % (f["original_name"], f["original_ticker"]),
    ]
    return "\n".join(lines)


def telegram_post(env, chat_id, image_url, caption, transport=http_json):
    token = env.get("TELEGRAM_BOT_TOKEN")
    status, resp = transport(
        "POST",
        "https://api.telegram.org/bot%s/sendPhoto" % token,
        payload={"chat_id": chat_id, "photo": image_url, "caption": caption},
    )
    if status == 200 and isinstance(resp, dict) and resp.get("ok"):
        return (resp.get("result") or {}).get("message_id")
    return None


def process_request(headers, raw_body, env, transport=http_json, get_text=http_get_text):
    """Core entry point. Returns (status_code, response_dict). Never raises."""
    if not secret_ok(headers, env):
        return 401, {"error": "unauthorized"}
    missing = missing_config(env)
    if missing:
        return 503, {"error": "not_configured", "missing": missing}
    try:
        body = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return 400, {"error": "invalid_json"}
    fields, error = validate_payload(body)
    if error:
        return 400, {"error": error}
    if not mint_exists_on_chain(fields["mint"], env, transport):
        return 422, {"error": "mint_not_found_on_chain"}
    mints = registry_read(env, transport, get_text)
    if mints is None:
        return 503, {"error": "dedupe_store_unavailable"}
    if fields["mint"] in mints:
        return 409, {"error": "already_posted", "mint": fields["mint"]}
    chat_id = (
        env["TELEGRAM_CHANNEL_ID"]
        if fields["channel"] == "public"
        else env["TELEGRAM_TEST_CHANNEL_ID"]
    )
    caption = build_caption(fields)
    message_id = telegram_post(env, chat_id, fields["image_url"], caption, transport)
    if message_id is None:
        return 502, {"error": "telegram_post_failed"}
    mints.append(fields["mint"])
    registry_ok = registry_write(env, mints)
    return 200, {
        "posted": True,
        "mint": fields["mint"],
        "channel": fields["channel"],
        "message_id": message_id,
        "registry_updated": registry_ok,
    }


# --------------------------------------------------------------------------
# Vercel handler
# --------------------------------------------------------------------------

class handler(BaseHTTPRequestHandler):
    def _reply(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            return self._reply(400, {"error": "bad_body_size"})
        raw = self.rfile.read(length)
        headers = {k: v for k, v in self.headers.items()}
        status, payload = process_request(headers, raw, dict(os.environ))
        return self._reply(status, payload)

    def do_GET(self):
        return self._reply(405, {"error": "method_not_allowed"})

    def log_message(self, *args):  # keep function logs free of request data
        pass
