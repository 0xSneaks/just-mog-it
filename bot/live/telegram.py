"""Pipeline-side client for the Telegram launch announcer.

The ONLY sanctioned caller of /api/telegram_launch. The bot calls this once a
launch reaches CONFIRMED; the server function re-verifies everything
(secret, on-chain mint, dedupe) before posting, so a call from anywhere else
— or a duplicate call from here — cannot produce a post.

Env vars (bot runtime, never committed):
    JUSTMOGIT_LAUNCH_POST_URL   e.g. https://justmogit.vercel.app/api/telegram_launch
    LAUNCH_POST_SECRET          must match the Vercel project's value

This module is intentionally NOT wired into the launch engine yet: the hook
lands with the money-path review (Codex) like every other engine change.
"""

import json
import os
import urllib.request

TIMEOUT_SECONDS = 30


def announce_launch(
    *,
    mint,
    name,
    ticker,
    venue,
    image_url,
    original_name,
    original_ticker,
    venue_url=None,
    channel="public",
    env=None,
    opener=None,
):
    """Ask the announcer to post one launch. Returns (ok, status, response).

    ok is True only on a 200 'posted' response. A 409 means the mint was
    already announced — that is a safe no-op for the pipeline, not an error
    worth retrying.
    """
    env = env if env is not None else os.environ
    url = env.get("JUSTMOGIT_LAUNCH_POST_URL")
    secret = env.get("LAUNCH_POST_SECRET")
    if not url or not secret:
        return False, 0, {"error": "announcer_not_configured"}
    payload = {
        "mint": mint,
        "name": name,
        "ticker": ticker,
        "venue": venue,
        "image_url": image_url,
        "original": {"name": original_name, "ticker": original_ticker},
        "channel": channel,
    }
    if venue_url:
        payload["venue_url"] = venue_url
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-launch-secret": secret,
        },
    )
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(req, timeout=TIMEOUT_SECONDS) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
            return resp.status == 200, resp.status, body
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            body = {"error": "unreadable_response"}
        return False, e.code, body
    except Exception as e:  # network down, DNS, timeout — never raise into the pipeline
        return False, 0, {"error": "transport_error", "detail": type(e).__name__}
