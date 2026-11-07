"""Public reply text. Rules: the CA goes out as PLAIN TEXT, never a link (URL post bills
$0.20 vs $0.015); no hype; rescue copy tells the story of the mog only — no dev
disclaimers, no NFA tag (owner ruling 2026-11-07).
Budget: CAP_REPLY_BUDGET per UTC day covers ALL replies (spec §7)."""
import re
from decimal import Decimal

URLISH = re.compile(r"https?://|www\.|\.(?:com|fun|io|xyz|net|org|app|it)\b", re.I)

class ReplyError(Exception):
    pass

def _check(text):
    if URLISH.search(text):
        raise ReplyError("reply contains a URL — would bill as a URL post")
    if len(text) > 280:
        raise ReplyError("reply over 280 chars")
    return text

def launched(d, mint):
    # "pump.fun" would auto-link on X and bill as a URL post — write "pumpfun"
    return _check(f"Mogged. ${d.ticker} was on the wrong pair — relaunched on a SOL pair, on pumpfun.\n"
                  f"CA: {mint}")

def payment_request(job_id, address, fee_sol):
    return _check(f"To mog it, send {fee_sol} SOL to {address} with memo {job_id} within 10 min. "
                  "Non-refundable.")

def wallet_request():
    return _check("Your mogger share accrues per coin. Reply with your Solana address to get paid "
                  "once it clears the minimum.")

REJECT = {
    "not_allowlisted": None,          # silent during pilot: no reply, no spend
    "hard_skip": "Can't mog that one. We skip coins about deaths, violence, politics, kids, "
                 "private people, or giveaway bait.",
    "duplicate": "Already mogged. CA: {mint}",
    "cooldown": None,
    "cap": "At capacity today (max {cap}/day). Try again tomorrow.",
    "bankroll": "Bankroll is low right now. Try again later.",
    "not_eligible": "That coin doesn't look rugged or mis-launched, so there's nothing to rescue.",
    "unresolved": "Couldn't find that coin with confidence. Quote the post with its CA.",
    "pair": "That pair isn't launchable right now; I can fire it on SOL instead. Say SOL.",
    "payment": "Fee not received in time, so this mog expired. Nothing launched.",
    "kill": None,
    "spelling_timeout": "No confirmation, so I didn't launch.",
}

def rejection(code, **kw):
    t = REJECT.get(code)
    return _check(t.format(**kw)) if t else None

def mog_me_done(handle):
    return _check(f"@{handle} mogged.")

def mog_me_failed():
    return _check("Couldn't find a face to mog in that pic. Try a clearer PFP.")

def alt_text(handle):
    return f"@{handle}'s profile picture wearing wraparound mog shades."

class Budget:
    """Per-day reply spend in USD, persisted in the store."""
    def __init__(self, store, policy, day_fn):
        self.store, self.policy, self.day = store, policy, day_fn

    def try_spend(self, url=False):
        key = f"reply_spend:{self.day()}"
        cost = Decimal(str(self.policy.x_prices_usd["url_post" if url else "plain_post"]))
        spent = Decimal(self.store.get(key, "0"))
        if spent + cost > Decimal(str(self.policy.reply_budget_usd_per_day)):
            return False
        self.store.put(key, str(spent + cost))
        return True
