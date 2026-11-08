"""Mogz callout step, post-launch (mogz.fun only; never on the website).

Route: POST https://mogz.fun/api/callouts {mint, text, image, link}
Verify: GET https://mogz.fun/api/coins/<CA>/callouts — a launch is not done until the
GET shows the callout.

Order, all fail closed:
  prepare()  story-only copy, https-only image/link -> AWAITING_APPROVAL (default stop; no network)
  approve()  per-item owner approval -> APPROVED
  post()     guard: posting wallet's on-chain balance of the mint (SPL Token AND Token-2022)
             must read as exactly 0; > 0 or a failed read -> REFUSED, nothing posted.
             Then POST -> POSTED, then verify() -> CONFIRMED.

http and rpc are injected. The defaults are unwired and raise, so nothing reaches the
network unless a caller wires them explicitly. Tests use mocks only.
UNVERIFIED: the POST auth scheme and the GET response shape. verify() accepts a JSON
list or {"callouts": [...]} of objects with a "text" field; any other shape means not confirmed."""
import re
import unicodedata
from urllib.parse import urlparse

CALLOUTS_URL = "https://mogz.fun/api/callouts"
SPL_TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PQnjYVfF4kU5PBy"
TOKEN_PROGRAMS = (SPL_TOKEN, TOKEN_2022)
MAX_TEXT = 280
RETIRED = re.compile(r"not the original dev|\bNFA\b", re.I)  # owner ruling 2026-11-07
_B58 = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")

AWAITING_APPROVAL, APPROVED, REFUSED, POSTED, FAILED, CONFIRMED = (
    "AWAITING_APPROVAL", "APPROVED", "REFUSED", "POSTED", "FAILED", "CONFIRMED")


class CalloutError(Exception):
    pass


def coin_callouts_url(mint):
    return f"https://mogz.fun/api/coins/{mint}/callouts"


def _unwired(*_a, **_k):
    raise CalloutError("adapter not wired")


def clean(text, limit):
    """Target-supplied text: drop control/format characters, collapse whitespace, cap length."""
    text = "".join(c for c in str(text or "") if unicodedata.category(c)[0] != "C")
    return " ".join(text.split())[:limit]


def _mint(value):
    if not isinstance(value, str) or not _B58.fullmatch(value):
        raise CalloutError("invalid mint")
    return value


def _https(url):
    if url is None:
        return None
    p = urlparse(str(url))
    if p.scheme != "https" or not p.hostname:
        raise CalloutError("image/link must be https")
    return str(url)


def story_text(ticker, old_quote, new_ca):
    """Story only: what was wrong with the pair -> what the bot did -> the new CA."""
    t = re.sub(r"[^A-Za-z0-9]", "", clean(ticker, 32))[:10]
    q = re.sub(r"[^A-Za-z0-9]", "", clean(old_quote, 32))[:16]
    if not t or not q:
        raise CalloutError("ticker and old quote are required")
    text = f"${t} was stuck on a {q} pair. JustMogIt relaunched it on a SOL pair. New CA: {_mint(new_ca)}"
    if RETIRED.search(text) or len(text) > MAX_TEXT:
        raise CalloutError("copy rule")
    return text


def wallet_mint_balance(rpc, owner, mint):
    """Raw on-chain balance of `mint` held by `owner`, across SPL Token and Token-2022.
    Any RPC error or unexpected shape raises CalloutError (the caller refuses)."""
    total = 0
    for program in TOKEN_PROGRAMS:
        try:
            res = rpc("getTokenAccountsByOwner",
                      [owner, {"programId": program}, {"encoding": "jsonParsed"}])
            for acct in res["value"]:
                info = acct["account"]["data"]["parsed"]["info"]
                if info["mint"] == mint:
                    total += int(info["tokenAmount"]["amount"])
        except CalloutError:
            raise
        except Exception as e:
            raise CalloutError(f"balance read failed ({program[:6]}…): {type(e).__name__}") from None
    return total


class CalloutFlow:
    def __init__(self, posting_wallet, *, http=_unwired, rpc=_unwired):
        self.wallet, self.http, self.rpc = posting_wallet, http, rpc

    def prepare(self, *, mint, ticker, old_quote, image=None, link=None):
        """Build the item and stop at AWAITING_APPROVAL. No network."""
        return {"state": AWAITING_APPROVAL, "approved_by": None, "refusal": None,
                "body": {"mint": _mint(mint), "text": story_text(ticker, old_quote, mint),
                         "image": _https(image), "link": _https(link)}}

    def approve(self, item, approved_by):
        if item["state"] != AWAITING_APPROVAL:
            raise CalloutError(f"cannot approve from {item['state']}")
        if not str(approved_by or "").strip():
            raise CalloutError("owner approval required")
        item.update(state=APPROVED, approved_by=str(approved_by).strip())
        return item

    def post(self, item):
        if item["state"] != APPROVED or not item.get("approved_by"):
            raise CalloutError("not approved: per-item owner approval required")
        mint = item["body"]["mint"]
        try:
            bal = wallet_mint_balance(self.rpc, self.wallet, mint)
        except CalloutError as e:
            item.update(state=REFUSED, refusal=f"balance guard: {e}")
            return item
        if bal != 0:
            item.update(state=REFUSED, refusal="balance guard: posting wallet holds this mint")
            return item
        try:
            status, _ = self.http("POST", CALLOUTS_URL, json=item["body"], timeout=10)
        except Exception as e:
            item.update(state=FAILED, refusal=f"post error: {type(e).__name__}")
            return item
        if not 200 <= int(status) < 300:
            item.update(state=FAILED, refusal=f"post HTTP {status}")
            return item
        item["state"] = POSTED
        return self.verify(item)

    def verify(self, item):
        if item["state"] not in (POSTED, CONFIRMED):
            raise CalloutError(f"cannot verify from {item['state']}")
        try:
            status, body = self.http("GET", coin_callouts_url(item["body"]["mint"]), timeout=10)
        except Exception:
            return item
        rows = body.get("callouts") if isinstance(body, dict) else body
        if int(status) == 200 and isinstance(rows, list) and any(
                isinstance(r, dict) and r.get("text") == item["body"]["text"]
                and r.get("mint", item["body"]["mint"]) == item["body"]["mint"] for r in rows):
            item["state"] = CONFIRMED
        return item


def launch_done(item):
    """A launch counts as done only once the GET has confirmed its callout."""
    return bool(item) and item.get("state") == CONFIRMED
