"""PingJob engine (BUILD-SPEC-v2 §1.3, §7, §8). Two-phase invariant: no money moves, no
signature, no CA posted until PRE-STAGE is complete and the job is ARMED.

RECEIVED -> DEDUPED -> GATED -> STAGED -> PAYMENT_PENDING -> QUEUED -> RESERVED -> ARMED -> SIGNED
  -> SENT -> CONFIRMED -> REPLIED -> LEDGERED
Branches: SPELLING_PENDING, REJECTED, FAILED, UNKNOWN, EXPIRED, CANCELLED, REPLY_ESCALATED.

Everything outside this file that touches the network or keys is an injected adapter; an
unwired adapter raises AdapterUnwired and the job stops before any money moves."""
import hashlib
import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone

from . import fees, metadata, parse as P, replies, rug, screen, spelling
from .adapters import AdapterUnwired
from .store import LIVE

PRE_SEND = ("RECEIVED", "DEDUPED", "GATED", "SPELLING_PENDING", "PAYMENT_PENDING", "STAGED",
            "QUEUED", "RESERVED", "ARMED")

class BootRefused(RuntimeError):
    pass

class Refused(Exception):
    def __init__(self, code, **kw):
        super().__init__(code)
        self.code, self.kw = code, kw

class Engine:
    def __init__(self, store, policy, adapters, clock=time.time, public=False, *, pair_preview=None):
        if public and pair_preview is not None:
            raise BootRefused("pair review is preview-only")
        self.s, self.p, self.a, self.clock = store, policy, adapters, clock
        self.budget = replies.Budget(store, policy, self.day)
        self.cap = fees.lamports(policy.cost_cap_sol)
        self.public = public
        self.pair_preview = pair_preview

    # ---------------------------------------------------------------- boot / kill
    def boot(self):
        """Kill switch ON at boot (§0.9). Public boot refused without X's written approval
        reference (§0.11) — fails closed."""
        if self.p.kill_switch_on_at_boot:
            self.s.put("kill", True)
        if self.public and not str(self.p.x_reply_approval_ref).strip():
            raise BootRefused("X automated-reply approval reference missing (policy x_reply_approval_ref)")
        if self.s.get("bankroll_available") is None:
            self.s.put("bankroll_available", 0)
        return {"kill": self.s.get("kill"), "public": self.public}

    def killed(self):
        return bool(self.s.get("kill", True))

    def set_kill(self, on, by="owner"):
        """Kill ON: every pre-SENT job is CANCELLED and its reservation released.
        SENT/UNKNOWN jobs are left alone (their money may already be on-chain)."""
        self.s.put("kill", bool(on))
        self.s.ledger_row("kill_switch", {"on": bool(on), "by": by})
        if on:
            for j in self.s.jobs(*PRE_SEND):
                self._finish(j["job_id"], "CANCELLED", "kill")

    def day(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y-%m-%d")

    # ---------------------------------------------------------------- transitions
    def _move(self, job_id, allowed, to, note="", db=None, **fields):
        db = db or self.s.db
        cur = db.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if cur is None or cur["state"] not in allowed:
            raise RuntimeError(f"illegal transition {cur and cur['state']} -> {to} for {job_id}")
        sets = ", ".join(["state=?", "updated_at=?"] + [f"{k}=?" for k in fields])
        db.execute(f"UPDATE jobs SET {sets} WHERE job_id=?",
                   (to, self.clock(), *fields.values(), job_id))
        self.s.audit(job_id, cur["state"], to, note, db)

    def _release(self, job_id, db):
        r = db.execute("SELECT * FROM reservations WHERE job_id=? AND state='HELD'", (job_id,)).fetchone()
        if r:
            db.execute("UPDATE reservations SET state='RELEASED' WHERE rid=?", (r["rid"],))
            self.s.put("bankroll_available", self.s.get("bankroll_available", 0) + r["lamports"], db)

    def _finish(self, job_id, to, code=None):
        """Terminal non-success: release any held reservation, record why."""
        with self.s.tx() as db:
            st = db.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()["state"]
            if st in ("SENT", "UNKNOWN", "SIGNED") and to != "FAILED":
                raise RuntimeError("never release money that may be on-chain")
            self._release(job_id, db)
            self._move(job_id, LIVE, to, code or "", db, rejection_code=code)
        return to

    def _reject(self, job_id, code, reply=True, **kw):
        self._finish(job_id, "REJECTED", code)
        if reply:
            self._reply_text(job_id, replies.rejection(code, **kw))
        return "REJECTED", code

    # ---------------------------------------------------------------- ingress
    def ingest(self, m):
        """m = {id, author_id, handle, text, quoted_tweet_id?, quoted_text?}. Returns a tuple
        describing what happened; never raises on bad input."""
        trig = P.parse(m.get("text", ""), m.get("quoted_tweet_id"))
        if trig.kind == "mog_me":
            return self.mog_me(m)
        if trig.kind in ("yes", "stop"):
            return self.spelling_answer(m["author_id"], trig.kind, m)
        if trig.kind != "mog_coin":
            return ("ignored", None)
        if self.killed():
            return ("ignored", "kill")
        if self.pair_preview is not None:
            count = m.get("followers_count")
            if (m.get("verified_type") not in ("blue", "business", "government")
                    or type(count) is not int or count < 60):
                return ("ignored", "author_gate")
        job_id = uuid.uuid4().hex[:16]
        now = self.clock()
        try:
            self.s.db.execute(
                "INSERT INTO jobs(job_id,kind,source_tweet_id,requester_id,state,created_at,updated_at,ttl_at,payload) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (job_id, "mog_coin", str(m["id"]), str(m["author_id"]), "RECEIVED", now, now,
                 now + 3600 * int(self.p.limits["job_ttl_hours"]),
                 json.dumps({"text": m.get("text"), "quoted_text": m.get("quoted_text"),
                             "handle": m.get("handle"), "cas": trig.cas, "tickers": trig.tickers,
                             "quoted_tweet_id": trig.quoted_tweet_id})))
        except sqlite3.IntegrityError:
            return ("duplicate_tweet", None)          # same source tweet seen twice
        cas = trig.cas or P.parse(m.get("quoted_text") or "").cas
        if not cas:
            return self._reject(job_id, "unresolved")
        ca = cas[0]
        self.s.set_payload(job_id, ca=ca)
        try:
            self._move(job_id, ("RECEIVED",), "DEDUPED", dedupe_key=f"ca:{ca}")
        except sqlite3.IntegrityError:
            other = self.s.db.execute("SELECT job_id FROM jobs WHERE dedupe_key=? AND job_id!=?",
                                      (f"ca:{ca}", job_id)).fetchone()
            self.s.set_payload(job_id, joined=other and other["job_id"])
            return self._reject(job_id, "duplicate", reply=False)
        return self.gate(job_id)

    # ---------------------------------------------------------------- gates (§7 order)
    def gate(self, job_id):
        j, pl = self.s.job(job_id), self.s.payload(job_id)
        req = j["requester_id"]
        if self.killed():
            return self._reject(job_id, "kill", reply=False)
        if self.p.trigger_policy == "allowlist" and req not in [str(x) for x in self.p.allowlist]:
            return self._reject(job_id, "not_allowlisted", reply=False)
        ok, cat = screen.screen(pl.get("text"), pl.get("quoted_text"))
        if not ok:
            self.s.set_payload(job_id, skip_category=cat)
            return self._reject(job_id, "hard_skip")
        prior = self.s.db.execute("SELECT mint FROM ledger WHERE kind='launch' AND json_extract(body,'$.orig_ca')=?",
                                  (pl["ca"],)).fetchone()
        if prior:
            return self._reject(job_id, "duplicate", mint=prior["mint"])
        cool = 60 * int(self.p.limits["cooldown_per_user_min"])
        recent = self.s.db.execute(
            "SELECT 1 FROM jobs WHERE requester_id=? AND job_id!=? AND created_at>? AND state NOT IN "
            "('REJECTED','EXPIRED','CANCELLED')", (req, job_id, self.clock() - cool)).fetchone()
        if recent:
            return self._reject(job_id, "cooldown", reply=False)
        if self.pair_preview is not None:
            self._move(job_id, ("DEDUPED",), "GATED")
            return self._preview_pair(job_id, pl["ca"])
        if self._launches_today() >= int(self.p.launches_per_day):
            return self._reject(job_id, "cap", cap=self.p.launches_per_day)
        if self.s.get("bankroll_available", 0) < self.cap:
            return self._reject(job_id, "bankroll")
        self._move(job_id, ("DEDUPED",), "GATED")
        return self.stage(job_id)

    def _preview_pair(self, job_id, ca):
        decision, context = self.pair_preview.review(ca)
        reply = "Keeping this one. The current pair fits." if decision.action == "keep" else None
        with self.s.tx() as db:
            job = self.s.job(job_id)
            if job["state"] != "GATED":
                return (job["state"], None)
            self._move(job_id, ("GATED",), "PAIR_REVIEWED", decision.reason, db)
            self.s.ledger_row("pair_preview", {
                "mention_id": job["source_tweet_id"],
                "target_mint": ca,
                "action": decision.action,
                "reason": decision.reason,
                **({"kind": decision.kind} if decision.kind is not None else {}),
                "selected_quote_mint": decision.selected_quote_mint,
                "reply_preview": reply,
            }, job_id=job_id, db=db)
            if decision.action == "ask":
                self.s.ledger_row("pair_review_request", {
                    "mention_id": job["source_tweet_id"],
                    "target_mint": ca,
                    "narrative": context.narrative,
                    "underlying": context.underlying,
                    "venue": "pump.fun",
                    "candidates": [c.__dict__ for c in context.candidates],
                }, job_id=job_id, db=db)
        return ("PAIR_REVIEWED", decision.action)

    def _launches_today(self):
        """Holds/spends today PLUS jobs already queued toward a launch, so nobody is asked to
        pay a fee for a slot that cannot exist."""
        r = self.s.db.execute("SELECT COUNT(*) c FROM reservations WHERE day=? AND state IN ('HELD','SPENT')",
                              (self.day(),)).fetchone()["c"]
        q = self.s.db.execute("SELECT COUNT(*) c FROM jobs WHERE state IN "
                              "('GATED','SPELLING_PENDING','STAGED','PAYMENT_PENDING','QUEUED')").fetchone()["c"]
        return r + q

    def request_payment(self, job_id):
        """Only a coin that passed every check is asked for the fee (spec §0.3)."""
        self._move(job_id, ("STAGED",), "PAYMENT_PENDING")
        addr = self.a.payments.job_address(job_id)
        self.s.set_payload(job_id, pay_address=addr)
        self._reply_text(job_id, replies.payment_request(job_id, addr, self.p.launch_fee_sol))
        return ("PAYMENT_PENDING", addr)

    def payment_check(self, job_id):
        """§0.3 rail: requester's 0.02 SOL must land (job address, memo=job_id) before RESERVED."""
        j = self.s.job(job_id)
        if j["state"] != "PAYMENT_PENDING":
            return j["state"]
        res = self.a.payments.verify(job_id, fees.lamports(self.p.launch_fee_sol))
        if not res or not res.get("ok"):
            return "PAYMENT_PENDING"
        self._move(job_id, ("PAYMENT_PENDING",), "QUEUED", payment_tx=res["signature"])
        self.s.ledger_row("launch_fee", {"from": res.get("payer")}, job_id=job_id,
                          lamports=fees.lamports(self.p.launch_fee_sol))
        out = self.reserve_safe(job_id)
        if out[0] == "REJECTED":                       # paid but no slot: owner decides refund
            self.s.ledger_row("fee_refund_owed", {"reason": out[1], "to": res.get("payer")}, job_id=job_id,
                              lamports=fees.lamports(self.p.launch_fee_sol))
        return out

    # ---------------------------------------------------------------- stage (§3–§6)
    def stage(self, job_id):
        pl = self.s.payload(job_id)
        ca = pl["ca"]
        d = self.a.dossier
        try:
            das, coin = d.das_asset(ca), d.pump_coin(ca)
            snap = d.snapshot(ca)
            inspection = d.inspect_image(coin.get("image_uri") if coin else None)
        except AdapterUnwired:
            raise
        except Exception as e:
            self.s.set_payload(job_id, stage_error=str(e))
            return self._reject(job_id, "unresolved")
        eligible, reasons = rug.evaluate(snap, self.p)
        if not eligible:
            return self._reject(job_id, "not_eligible")
        try:
            dos = metadata.resolve(self.p, ca=ca, das_asset=das, pump_coin=coin)
        except metadata.DossierError as e:
            self.s.set_payload(job_id, stage_error=str(e))
            return self._reject(job_id, "unresolved")
        ok, cat = screen.screen(dos.name, dos.ticker, (coin or {}).get("description"), inspection=inspection)
        if not ok:
            self.s.set_payload(job_id, skip_category=cat)
            return self._reject(job_id, "hard_skip")
        self.s.set_payload(job_id, dossier={**dos.__dict__, "spelling": None, "reasons": reasons})
        if not dos.spelling.fireable:
            self._move(job_id, ("GATED",), "SPELLING_PENDING")
            self._reply_text(job_id, spelling.question(dos.spelling))
            return ("SPELLING_PENDING", None)
        self._move(job_id, ("GATED",), "STAGED")
        return self.request_payment(job_id)

    def spelling_answer(self, requester, kind, m):
        j = next((x for x in self.s.jobs("SPELLING_PENDING") if x["requester_id"] == str(requester)), None)
        if not j:
            return ("ignored", None)
        if kind == "stop":
            return (self._finish(j["job_id"], "CANCELLED", "requester_stop"), None)
        self.s.set_payload(j["job_id"], spelling_confirmed_by="requester_explicit")
        self._move(j["job_id"], ("SPELLING_PENDING",), "STAGED")
        return self.request_payment(j["job_id"])

    # ---------------------------------------------------------------- reserve (§7)
    def reserve(self, job_id):
        """Atomic: kill check + cap re-check + bankroll check + hold 0.025 SOL, one tx."""
        now = self.clock()
        with self.s.tx() as db:
            if self.killed():
                raise Refused("kill")
            c = db.execute("SELECT COUNT(*) c FROM reservations WHERE day=? AND state IN ('HELD','SPENT')",
                           (self.day(),)).fetchone()["c"]
            if c >= int(self.p.launches_per_day):
                raise Refused("cap")
            bal = self.s.get("bankroll_available", 0)
            if bal < self.cap:
                raise Refused("bankroll")
            db.execute("INSERT INTO reservations(job_id,lamports,state,day,created_at,expires_at) "
                       "VALUES(?,?,?,?,?,?)", (job_id, self.cap, "HELD", self.day(), now,
                                               now + 60 * int(self.p.limits["reservation_ttl_min"])))
            self.s.put("bankroll_available", bal - self.cap, db)
            self._move(job_id, ("QUEUED",), "RESERVED", db=db)
        return ("RESERVED", "awaiting owner approval" if self.p.owner_approval_per_item else None)

    def reserve_safe(self, job_id):
        try:
            return self.reserve(job_id)
        except Refused as r:
            return self._reject(job_id, r.code, reply=r.code != "kill", cap=self.p.launches_per_day)

    # ---------------------------------------------------------------- arm + fire (§5, §8)
    def approve(self, job_id, by):
        """Per-launch owner approval (§0.10). Approving a launch approves its CA reply."""
        if self.s.job(job_id)["state"] != "RESERVED":
            raise RuntimeError("only a RESERVED job can be approved")
        self.s.db.execute("UPDATE jobs SET approved_by=? WHERE job_id=?", (by, job_id))
        self.s.audit(job_id, "RESERVED", "RESERVED", f"approved by {by}")

    def arm(self, job_id):
        j = self.s.job(job_id)
        if self.p.owner_approval_per_item and not j["approved_by"]:
            raise Refused("needs_owner_approval")
        if self.killed():
            self._finish(job_id, "CANCELLED", "kill"); raise Refused("kill")
        probe = self.a.pair.probe("SOL")
        if not probe.get("launchable") or probe.get("expires_at", 0) <= self.clock():
            self._reject(job_id, "pair"); raise Refused("pair")
        mint = self.a.vault.new_mint(job_id)          # keypair stays in the vault; we keep the pubkey
        self._move(job_id, ("RESERVED",), "ARMED", mint=mint)
        return mint

    def fire(self, job_id):
        """ARMED -> SIGNED -> SENT -> CONFIRMED | UNKNOWN. Guard + simulation before signing.
        Any failure BEFORE a signature exists rejects the job and releases its hold.
        Once signed, money may be on-chain: never release, go UNKNOWN and reconcile."""
        if self.killed():
            self._finish(job_id, "CANCELLED", "kill"); raise Refused("kill")
        try:
            signed, sig, cost = self._prepare(job_id)
        except Refused as r:
            self._reject(job_id, r.code if r.code in replies.REJECT else "pair", reply=False)
            raise
        except Exception as e:                                # guard mismatch, unwired adapter, RPC error
            self.s.set_payload(job_id, fire_error=f"{type(e).__name__}: {e}")
            self._reject(job_id, "pair", reply=False)
            raise Refused("pre_sign_failure", error=str(e)) from e
        j = self.s.job(job_id)
        self._move(job_id, ("ARMED",), "SIGNED", tx_sig=sig, attempt_no=j["attempt_no"] + 1)
        self.s.set_payload(job_id, sim_cost=cost, signed=signed)
        try:
            self.a.rpc.send(signed)
        except Exception as e:                                # may still have reached a leader
            self.s.set_payload(job_id, send_error=str(e))
        self._move(job_id, ("SIGNED",), "SENT")
        return self.confirm(job_id)

    def _prepare(self, job_id):
        j, pl = self.s.job(job_id), self.s.payload(job_id)
        d = pl["dossier"]
        probe = self.a.pair.probe("SOL")
        if not probe.get("launchable") or probe.get("expires_at", 0) <= self.clock():
            raise Refused("pair")
        tx = self.a.pump.build_create(name=d["name"], symbol=d["ticker"], description=d["description"],
                                      image_url=d["image_url"], socials=d["socials"], mint=j["mint"],
                                      dev_buy_sol="0", priority_cap_sol=self.p.priority_fee_cap_sol)
        self.a.guard.inspect(tx, expected_mint=j["mint"], expected_name=d["name"],
                             expected_symbol=d["ticker"], dev_buy_lamports=0)   # raises on any mismatch
        sim = self.a.rpc.simulate(tx)
        if not sim.get("ok"):
            raise Refused("sim_failed")
        cost = int(sim["payer_lamports_delta"])
        if cost > self.cap:
            raise Refused("over_cost_cap", cost=cost)
        signed = self.a.vault.sign(tx, job_id)
        sig = self.a.vault.signature_of(signed)
        if not sig:
            raise Refused("no_signature")                     # reconcile would have nothing to look up
        return signed, sig, cost

    def confirm(self, job_id):
        j = self.s.job(job_id)
        st = self.a.rpc.status(j["tx_sig"], timeout_s=30)
        if st in ("confirmed", "finalized"):
            return self._confirmed(job_id)
        if st == "failed":
            return self._failed(job_id)
        self._move(job_id, ("SENT",), "UNKNOWN", "no confirmation in 30s; reservation kept")
        return ("UNKNOWN", None)

    def reconcile(self, job_id):
        """UNKNOWN: re-query the OLD signature first; rebuild same mint only if it is null
        AND its blockhash expired. Never a new mint; never release while unresolved."""
        j = self.s.job(job_id)
        st = self.a.rpc.status(j["tx_sig"], timeout_s=0)
        if st in ("confirmed", "finalized"):
            return self._confirmed(job_id)
        if st == "failed":
            return self._failed(job_id)
        if st is None and self.a.rpc.blockhash_expired(j["tx_sig"]):
            self._move(job_id, ("UNKNOWN",), "ARMED", "same-mint rebuild")
            return self.fire(job_id)
        return ("UNKNOWN", None)

    def _confirmed(self, job_id):
        j, pl = self.s.job(job_id), self.s.payload(job_id)
        cost = int(pl.get("sim_cost", self.cap))
        with self.s.tx() as db:
            r = db.execute("SELECT * FROM reservations WHERE job_id=?", (job_id,)).fetchone()
            spent = max(cost, 0)
            db.execute("UPDATE reservations SET state='SPENT' WHERE rid=?", (r["rid"],))
            refund = r["lamports"] - min(spent, r["lamports"])
            self.s.put("bankroll_available", self.s.get("bankroll_available", 0) + refund, db)
            self._move(job_id, ("SENT", "UNKNOWN"), "CONFIRMED", db=db)
            self.s.ledger_row("launch", {"orig_ca": pl["ca"], "name": pl["dossier"]["name"],
                                         "ticker": pl["dossier"]["ticker"], "sig": j["tx_sig"],
                                         "requester": j["requester_id"], "source_tweet": j["source_tweet_id"],
                                         "approved_by": j["approved_by"], "reasons": pl["dossier"].get("reasons")},
                              job_id=job_id, mint=j["mint"], lamports=spent, db=db)
        return self.reply_launch(job_id)

    def _failed(self, job_id):
        with self.s.tx() as db:
            self._release(job_id, db)
            self._move(job_id, ("SENT", "UNKNOWN", "SIGNED"), "FAILED", rejection_code="tx_failed", db=db)
        return ("FAILED", None)

    # ---------------------------------------------------------------- replies
    def _reply_text(self, job_id, text):
        if not text:
            return None
        j = self.s.job(job_id)
        if not self.budget.try_spend(url=False):
            self.s.ledger_row("reply_skipped_budget", {"text": text}, job_id=job_id)
            return None
        return self.a.x.reply(j["source_tweet_id"], text, idempotency_key=f"{job_id}:{hashlib.sha256(text.encode()).hexdigest()[:12]}")

    def reply_launch(self, job_id):
        j, pl = self.s.job(job_id), self.s.payload(job_id)
        d = type("D", (), pl["dossier"])
        text = replies.launched(d, j["mint"])
        try:
            if not self.budget.try_spend(url=False):
                raise RuntimeError("reply budget exhausted")
            rid = self.a.x.reply(j["source_tweet_id"], text, idempotency_key=f"{job_id}:reply:1")
        except AdapterUnwired:
            raise
        except Exception:
            n = j["reply_failures"] + 1
            self.s.db.execute("UPDATE jobs SET reply_failures=? WHERE job_id=?", (n, job_id))
            if n >= 3:
                self._move(job_id, ("CONFIRMED",), "REPLY_ESCALATED", "3 reply failures; owner alert")
                return ("REPLY_ESCALATED", None)
            return ("CONFIRMED", "reply retry pending")
        self._move(job_id, ("CONFIRMED",), "REPLIED", reply_tweet_id=str(rid))
        self._move(job_id, ("REPLIED",), "LEDGERED")
        return ("LEDGERED", j["mint"])

    # ---------------------------------------------------------------- housekeeping
    def expire(self):
        now, out = self.clock(), []
        for j in self.s.jobs(*PRE_SEND):
            r = self.s.db.execute("SELECT expires_at FROM reservations WHERE job_id=? AND state='HELD'",
                                  (j["job_id"],)).fetchone()
            spell = j["state"] == "SPELLING_PENDING" and \
                now > j["updated_at"] + 60 * int(self.p.limits["spelling_ttl_min"])
            pay = j["state"] == "PAYMENT_PENDING" and \
                now > j["updated_at"] + 60 * int(self.p.limits["reservation_ttl_min"])
            if now > j["ttl_at"] or (r and now > r["expires_at"] and j["state"] != "ARMED") or spell or pay:
                self._finish(j["job_id"], "EXPIRED", "payment" if pay else "ttl")
                out.append(j["job_id"])
        return out

    # ---------------------------------------------------------------- fee loop
    def sweep_fees(self):
        """pump.fun pools ALL of a creator's fees in one vault and claims them at once, so:
        claim the vault when worth it (§9), measure what actually landed (simulation delta),
        then attribute it to coins by volume weight and book 25/25/50 per coin.
        Weightless remainder (no volume data) goes to the bankroll, ledgered as unattributed."""
        launched = self.s.ledger("launch")
        if not launched or self.killed():
            return []
        total = int(self.a.pump.claimable())
        if not fees.should_claim(self.p, total, self.a.pump.claim_cost()):
            return []
        tx = self.a.pump.build_claim()
        self.a.guard.inspect_claim(tx)                         # only collect_creator_fee, bot signs alone
        sim = self.a.rpc.simulate(tx)
        gained = -int(sim.get("payer_lamports_delta", 0)) if sim.get("ok") else 0
        if gained <= 0:
            return []
        signed = self.a.vault.sign(tx, "claim")
        sig = self.a.vault.signature_of(signed)
        self.a.rpc.send(signed)
        if self.a.rpc.status(sig, timeout_s=30) not in ("confirmed", "finalized"):
            self.s.ledger_row("claim_unconfirmed", {"sig": sig, "expected": gained})
            return []
        weights = {r["mint"]: max(float(self.a.dossier.fee_weight(r["mint"]) or 0), 0.0) for r in launched}
        wsum = sum(weights.values())
        booked, used = [], 0
        with self.s.tx() as db:
            for r in launched:
                share = int(gained * weights[r["mint"]] / wsum) if wsum else 0
                if share <= 0:
                    continue
                used += share
                booked.append((r["mint"], fees.record_claim(self.s, self.p, r["mint"], self.s.job(r["job_id"]), share, db)))
            rest = gained - used
            if rest > 0:
                self.s.put("bankroll_available", self.s.get("bankroll_available", 0) + rest, db)
                self.s.ledger_row("fee_unattributed", {"sig": sig}, lamports=rest, db=db)
            self.s.ledger_row("fee_claim_tx", {"sig": sig, "gained": gained}, lamports=gained, db=db)
        return booked

    # ---------------------------------------------------------------- mog me
    def mog_me(self, m):
        from . import mogme
        if not self.p.mog_me_enabled:
            return ("ignored", "mog_me_disabled")
        if self.killed():
            return ("ignored", "kill")
        uid = str(m["author_id"])
        if self.p.trigger_policy == "allowlist" and uid not in [str(x) for x in self.p.allowlist]:
            return ("ignored", "not_allowlisted")
        key = f"mogme:{self.day()}:{uid}"
        if self.s.get(key, 0) >= int(self.p.limits["mog_me_per_user_per_day"]):
            return ("ignored", "rate_limited")
        self.s.put(key, self.s.get(key, 0) + 1)
        pfp = self.a.x.profile_image(uid)                  # bytes of the 400x400 pfp
        eyes = self.a.faces.eyes(pfp)                       # None or ((lx,ly),(rx,ry)) in px
        if not self.budget.try_spend(url=False):
            return ("ignored", "budget")
        if not eyes:
            self.a.x.reply(m["id"], replies.mog_me_failed(), idempotency_key=f"mogme:{m['id']}")
            return ("mog_me_failed", "no_face")
        png = mogme.render(pfp, eyes)
        media_id = self.a.x.upload_media(png, alt_text=replies.alt_text(m.get("handle", "you")))
        self.a.x.reply(m["id"], replies.mog_me_done(m.get("handle", "you")), media_ids=[media_id],
                       idempotency_key=f"mogme:{m['id']}")
        return ("mog_me_done", media_id)
