"""SKILL.md flow as a state machine: preflight -> name -> ticker -> description -> image -> pinned card -> LAUNCH.
Adapters (injected, never constructed here): mogz (quote/build/simulate/submit), chain (balance),
vault (sign), images (verify). Gate 2: every tx is simulated and its cost shown before send."""
import base64, hashlib, json, re, time
from decimal import Decimal
from .guard import inspect, GuardError, b58, _cu16
from .ledger import LedgerError, validate_usd
from . import gates

BANNED = {"trump", "elon", "musk", "biden", "nike", "disney"}  # supplemental; real-person/brand needs Sneaks yes
FEE_TOLERANCE_LAMPORTS = 100_000  # FIX 2 slack over the quote: 2 signatures of base fee plus priority-fee jitter

def _banned_hit(text):
    """HOLE-2: word-boundary match — 'elon' must not block 'Melon'."""
    t = text.lower()
    return any(re.search(r"\b" + re.escape(b) + r"\b", t) for b in BANNED)
HYPE = re.compile(r"moon|100x|1000x|gem|pump|lfg|wagmi|[\U0001F300-\U0001FAFF]", re.I)
QUOTE_TTL = 90

def _first_sig(signed):
    """C2: base58 of the signed tx's first signature, or None if the signed payload
    does not parse. Recorded in the pending entry BEFORE submit so a reconcile step
    has a signature to look up on-chain."""
    try:
        raw = base64.b64decode(signed) if isinstance(signed, str) else bytes(signed)
        n, i = _cu16(raw, 0)
        if n < 1 or i + 64 > len(raw): return None
        return b58(raw[i:i + 64])
    except Exception:
        return None

class Session:
    def __init__(self, launcher, ledger, mogz, chain, vault, images, clock=time.time, owner_yes_names=()):
        self.launcher, self.ledger, self.mogz, self.chain, self.vault, self.images = launcher, ledger, mogz, chain, vault, images
        self.clock, self.yes = clock, {n.lower() for n in owner_yes_names}
        self.state, self.coin, self.card, self.tries = "idle", {}, None, 0

    def _refuse(self, why):
        self.state = "idle"; return f"REFUSED: {why}"

    def _preflight(self):
        q = self.mogz.quote()
        gates.check_quote(q)  # validate the SOL amount before parsing or comparing it
        validate_usd(q.get("usd"))  # refuse missing/invalid USD before build or sign
        bal = Decimal(str(self.chain.balance_sol()))
        if bal < Decimal(str(q["sol"])): raise LedgerError(f"wallet underfunded: need {q['sol']}, have {bal}")
        st = self.ledger.check(q["usd"])
        gates.check_preflight(self.ledger, st, q, self.clock())  # kill · 3/day · SOL quote · first buy 0 · quote cap
        return q, bal, st

    def handle(self, text):
        t = text.strip()
        if t.lower() in ("cancel", "quit") and self.state != "idle":
            self.state = "idle"; return "aborted. no money moved."
        h = getattr(self, "_" + self.state)
        return h(t)

    def _idle(self, t):
        if t != "/mogit": return "type /mogit to launch on Mogz.fun"
        try: self._preflight()
        except LedgerError as e: return self._refuse(e)
        self.state, self.coin, self.tries = "name", {}, 0
        return "Name? (1-32 chars)"

    def _retry(self, rule):
        self.tries += 1
        if self.tries > 2: return self._refuse(rule)
        return f"invalid: {rule}. try again"

    def _ok(self, nxt, prompt):
        self.state, self.tries = nxt, 0; return prompt

    def _name(self, t):
        if not 1 <= len(t) <= 32: return self._retry("name must be 1-32 chars")
        if _banned_hit(t) and t.lower() not in self.yes:
            return self._retry("real person/brand needs Sneaks's explicit yes")
        self.coin["name"] = t
        d = re.sub(r"[^A-Z0-9]", "", t.upper())[:6]
        self.coin["_default_ticker"] = d
        return self._ok("ticker", f"Ticker? (default {d} - type it or your own)")

    def _ticker(self, t):
        if not re.fullmatch(r"[A-Z][A-Z0-9]{0,9}", t): return self._retry("ticker A-Z0-9, 1-10, starts with letter")
        self.coin["ticker"] = t
        return self._ok("description", f"Description? e.g. '{self.coin['name']}. Launched by {self.launcher}.'")

    def _description(self, t):
        sents = [s for s in re.split(r"[.!?]+", t) if s.strip()]
        if HYPE.search(t) or len(sents) > 2 or self.launcher.lower() not in t.lower():
            return self._retry("simple rule: <=2 plain sentences, no hype/emoji, credit the launcher")
        self.coin["description"] = t
        return self._ok("image", "Image path, or none")

    def _image(self, t):
        sha = None
        if t.lower() != "none":
            sha = self.images.verify(t)  # opens + visually verifies; returns sha256 of verified bytes or None
            if not sha: return self._retry("unverified image (a path is never proof)")
        self.coin["image_sha256"] = sha
        return self._render()

    def _render(self):
        try: q, bal, st = self._preflight()
        except LedgerError as e: return self._refuse(e)
        try: quote_lamports = gates.check_quote(q)
        except LedgerError as e: return self._refuse(e)
        c = self.coin
        tx = self.mogz.build(c, q)                 # server builds the single-ix create
        try: g = inspect(tx, q.get("mint"))       # Gate 3 before anything else
        except GuardError as e: return self._refuse(f"guard: {e}")
        sim = self.mogz.simulate(tx)               # Gate 2: simulate, show cost
        if sim.get("err"): return self._refuse(f"simulation failed: {sim['err']}")
        delta = sim.get("payer_lamports_delta")
        if delta is None: return self._refuse("simulation missing payer_lamports_delta")  # HOLE-1: refuse, never KeyError
        try: spend = gates.payer_spend(delta)      # validate whole lamports before conversion/cap check
        except LedgerError as e: return self._refuse(e)
        if spend > quote_lamports + FEE_TOLERANCE_LAMPORTS:  # FIX 2: simulated spend checked against the quote
            return self._refuse(f"simulated cost {spend} lamports exceeds quote {quote_lamports} + fee tolerance")
        actual_usd = (Decimal(spend) * Decimal(str(q["usd"])) / Decimal(quote_lamports)
                      if quote_lamports else Decimal(str(q["usd"])))  # FIX 2: the simulated cost
        cost = max(actual_usd, Decimal(str(q["usd"])))  # C1: commit the larger of quote and simulated —
        # a sim that under-reports spend (delta 0, stale, adversarial adapter) must not under-charge the cap
        tx_bytes = base64.b64decode(tx) if isinstance(tx, str) else bytes(tx)
        card = {"coin.name": c["name"], "coin.ticker": c["ticker"], "coin.description": c["description"],
                "coin.image_sha256": c["image_sha256"], "tx.name": g["name"], "tx.symbol": g["symbol"],
                "tx.uri": g["uri"], "tx.sha256": hashlib.sha256(tx_bytes).hexdigest(),
                "cost.sol": str(q["sol"]), "cost.usd": str(cost), "cost.quoted_usd": str(q["usd"]),
                "sim.lamports_delta": delta, "wallet.address": g["fee_payer"],
                "wallet.balance_sol": str(bal), "budget.spent_usd": str(st["spent_usd"]),
                "budget.remaining_usd": str(st["remaining_usd"] - cost),
                "mint": g["mint"], "quote.expires_at": self.clock() + QUOTE_TTL}
        card["terms_hash"] = hashlib.sha256(json.dumps(card, sort_keys=True).encode()).hexdigest()
        self.card, self.tx, self.state = card, tx, "confirm"
        return "CONFIRM CARD\n" + "\n".join(f"{k}: {v}" for k, v in card.items()) + "\nType LAUNCH to sign+send, anything else aborts."

    def _confirm(self, t):
        if t != "LAUNCH": return self._refuse("no LAUNCH; nothing sent")
        if self.clock() > self.card["quote.expires_at"]: return self._render() + "\n(quote expired: re-quoted)"
        h = dict(self.card); th = h.pop("terms_hash")
        if hashlib.sha256(json.dumps(h, sort_keys=True).encode()).hexdigest() != th: return self._refuse("terms changed")
        tx_bytes = base64.b64decode(self.tx) if isinstance(self.tx, str) else bytes(self.tx)
        if hashlib.sha256(tx_bytes).hexdigest() != self.card["tx.sha256"]: return self._refuse("terms changed")
        with self.ledger.locked():  # FIX 4: check -> reserve -> submit -> settle, one session at a time
            try:
                self._preflight(); inspect(self.tx, self.card["mint"])  # gates re-run under the lock, before sign
                gates.payer_spend(self.card["sim.lamports_delta"])
                cost = self.card["cost.usd"]
                self.ledger.check(cost)  # pinned quote + simulated fee cost must fit before signing
            except (LedgerError, GuardError) as e: return self._refuse(e)
            try: signed = self.vault.sign(self.tx, th)
            except Exception as e:  # nothing signed, nothing sent: no reservation needed, no budget spent
                return self._refuse(f"sign error: {e}; nothing submitted, no budget spent")
            sig = _first_sig(signed)  # C2: first signature of the signed tx, recorded before submit
            try: rid = self.ledger.reserve(cost, {"mint": self.card["mint"], "terms_hash": th, "tx_sig": sig,
                                                  "ts": self.clock()})  # ts feeds the 3/day gate  # FIX 3: pending BEFORE submit
            except LedgerError as e: return self._refuse(e)
            try: res = self.mogz.submit(signed)
            except Exception as e:  # outcome unknown (e.g. timeout after send): keep the reservation against the cap
                return self._refuse(f"submit error: {e}; outcome unknown, reservation {rid} held against the cap")
            if not res.get("ok"):
                if res.get("pre_send"):  # C2: definitive pre-send rejection (preflight/simulation refused,
                    self.ledger.release(rid)  # never broadcast) — the only ok:false path that may release
                    return self._refuse(f"launch failed: {res.get('err')}; no budget spent")
                # C2: a send reported as failed can still land on-chain (the original FIX 3 case).
                # Keep the reservation as unconfirmed; only reconcile (expired blockhash + no
                # signature found) may release it. A held reservation fails safe: the cap shrinks.
                self.ledger.mark_unconfirmed(rid)
                return self._refuse(f"launch unconfirmed: {res.get('err')}; reservation {rid} held against the cap until reconcile")
            record = {"mint": self.card["mint"], "tx": res["sig"], "terms_hash": th, "cost_usd": cost,
                      "ts": self.clock()}
            try: self.ledger.settle(rid, record)
            except Exception as e:  # FIX 3: a settle failure after a successful send must not lose the spend record
                self.state = "idle"
                return (f"LAUNCHED mint {self.card['mint']} tx {res['sig']} "
                        f"(WARNING: ledger settle failed: {e}; pending reservation retained and still counts against the cap)")
            self.state = "idle"
            return f"LAUNCHED mint {self.card['mint']} tx {res['sig']}"
