"""SKILL.md flow as a state machine: preflight -> name -> ticker -> description -> image -> pinned card -> LAUNCH.
Adapters (injected, never constructed here): mogz (quote/build/simulate/submit), chain (balance),
vault (sign), images (verify). Gate 2: every tx is simulated and its cost shown before send."""
import hashlib, json, re, time
from decimal import Decimal
from .guard import inspect, GuardError
from .ledger import LedgerError

BANNED = {"trump", "elon", "musk", "biden", "nike", "disney"}  # supplemental; real-person/brand needs Sneaks yes
HYPE = re.compile(r"moon|100x|1000x|gem|pump|lfg|wagmi|[\U0001F300-\U0001FAFF]", re.I)
QUOTE_TTL = 90

class Session:
    def __init__(self, launcher, ledger, mogz, chain, vault, images, clock=time.time, owner_yes_names=()):
        self.launcher, self.ledger, self.mogz, self.chain, self.vault, self.images = launcher, ledger, mogz, chain, vault, images
        self.clock, self.yes = clock, {n.lower() for n in owner_yes_names}
        self.state, self.coin, self.card, self.tries = "idle", {}, None, 0

    def _refuse(self, why):
        self.state = "idle"; return f"REFUSED: {why}"

    def _preflight(self):
        q = self.mogz.quote()
        bal = Decimal(str(self.chain.balance_sol()))
        if bal < Decimal(str(q["sol"])): raise LedgerError(f"wallet underfunded: need {q['sol']}, have {bal}")
        st = self.ledger.check(q["usd"])
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
        if any(b in t.lower() for b in BANNED) and t.lower() not in self.yes:
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
        c = self.coin
        tx = self.mogz.build(c, q)                 # server builds the single-ix create
        try: g = inspect(tx, q.get("mint"))       # Gate 3 before anything else
        except GuardError as e: return self._refuse(f"guard: {e}")
        sim = self.mogz.simulate(tx)               # Gate 2: simulate, show cost
        if sim.get("err"): return self._refuse(f"simulation failed: {sim['err']}")
        card = {"coin.name": c["name"], "coin.ticker": c["ticker"], "coin.description": c["description"],
                "coin.image_sha256": c["image_sha256"], "cost.sol": str(q["sol"]), "cost.usd": str(q["usd"]),
                "sim.lamports_delta": sim["payer_lamports_delta"], "wallet.address": g["fee_payer"],
                "wallet.balance_sol": str(bal), "budget.spent_usd": str(st["spent_usd"]),
                "budget.remaining_usd": str(st["remaining_usd"] - Decimal(str(q["usd"]))),
                "mint": g["mint"], "quote.expires_at": self.clock() + QUOTE_TTL}
        card["terms_hash"] = hashlib.sha256(json.dumps(card, sort_keys=True).encode()).hexdigest()
        self.card, self.tx, self.state = card, tx, "confirm"
        return "CONFIRM CARD\n" + "\n".join(f"{k}: {v}" for k, v in card.items()) + "\nType LAUNCH to sign+send, anything else aborts."

    def _confirm(self, t):
        if t != "LAUNCH": return self._refuse("no LAUNCH; nothing sent")
        if self.clock() > self.card["quote.expires_at"]: return self._render() + "\n(quote expired: re-quoted)"
        h = dict(self.card); th = h.pop("terms_hash")
        if hashlib.sha256(json.dumps(h, sort_keys=True).encode()).hexdigest() != th: return self._refuse("terms changed")
        try: self._preflight(); inspect(self.tx, self.card["mint"])
        except (LedgerError, GuardError) as e: return self._refuse(e)
        res = self.mogz.submit(self.vault.sign(self.tx, self.card["terms_hash"]))
        if not res.get("ok"): return self._refuse(f"launch failed: {res.get('err')}; no budget spent")
        self.ledger.commit(self.card["cost.usd"], {"mint": self.card["mint"], "tx": res["sig"], "terms_hash": th})
        self.state = "idle"
        return f"LAUNCHED mint {self.card['mint']} tx {res['sig']}"
