"""Creator-fee loop (owner model): the bot is creator of every coin, claims the creator fee,
splits it 25 mogger / 25 holder pool / 50 bankroll (Split A, §0.4), recycles the bankroll.

- Integer lamports; any rounding remainder goes to the bankroll (never invented money).
- Holder pool is a LIABILITY until a distributor runs: tracked, never spent, not revenue.
- Tiers (owner rule 2026-11-04): within the holder pool, a holder's weight =
  pct_of_supply x tier multiplier. Hold more, earn more; sell down, drop a tier.
  The bot's own position is excluded. Thresholds/multipliers live in policy.yaml."""
from decimal import Decimal, ROUND_DOWN

from .store import LAMPORTS

def lamports(sol):
    return int((Decimal(str(sol)) * LAMPORTS).to_integral_value(ROUND_DOWN))

def should_claim(policy, claimable_lamports, claim_tx_cost_lamports):
    floor = max(lamports(policy.claim_threshold_sol), 20 * int(claim_tx_cost_lamports))
    return int(claimable_lamports) >= floor

def split(policy, amount):
    amount = int(amount)
    if amount < 0:
        raise ValueError("negative fee")
    s = policy.fee_split
    mog = amount * int(s["mogger"]) // 100
    hold = amount * int(s["holders"]) // 100
    return {"mogger": mog, "holders": hold, "bankroll": amount - mog - hold}

def multiplier(policy, pct_supply):
    m = Decimal("1")
    for t in sorted(policy.tiers, key=lambda t: Decimal(str(t["min_pct_supply"]))):
        if Decimal(str(pct_supply)) >= Decimal(str(t["min_pct_supply"])):
            m = Decimal(str(t["multiplier"]))
    return m

def distribute_holders(policy, pool_lamports, snapshot, exclude=()):
    """snapshot: {wallet: pct_of_supply}. Returns {wallet: lamports}; the undistributed
    remainder (rounding / nobody eligible) stays in the pool."""
    w = {a: Decimal(str(p)) * multiplier(policy, p) for a, p in snapshot.items()
         if a not in exclude and Decimal(str(p)) > 0}
    total = sum(w.values())
    if total == 0 or pool_lamports <= 0:
        return {}
    out = {a: int((Decimal(pool_lamports) * v / total).to_integral_value(ROUND_DOWN)) for a, v in w.items()}
    return {a: v for a, v in out.items() if v > 0}

def record_claim(store, policy, mint, job, amount, db):
    """Book one creator-fee claim into accruals + ledger. Caller holds the tx."""
    parts = split(policy, amount)
    for party, pid in (("mogger", job["requester_id"]), ("holders", "pool"), ("bankroll", "bot")):
        db.execute("INSERT INTO accruals(mint,party,party_id,accrued) VALUES(?,?,?,?) "
                   "ON CONFLICT(mint,party,party_id) DO UPDATE SET accrued=accrued+excluded.accrued",
                   (mint, party, pid, parts[party]))
    bal = store.get("bankroll_available", 0)
    store.put("bankroll_available", bal + parts["bankroll"], db)
    store.ledger_row("fee_claim", {"split": parts, "holders_liability": parts["holders"]},
                     job_id=job["job_id"], mint=mint, lamports=amount, db=db)
    return parts

def mogger_payable(store, policy, mint, mogger_id, wallet):
    """Lamports payable now to a mogger, or 0 (no wallet / under the dust minimum)."""
    if not wallet:
        return 0
    r = store.db.execute("SELECT accrued, paid FROM accruals WHERE mint=? AND party='mogger' AND party_id=?",
                         (mint, mogger_id)).fetchone()
    if not r:
        return 0
    due = r["accrued"] - r["paid"]
    return due if due >= lamports(policy.mogger["min_payout_sol"]) else 0

def holder_liability(store):
    r = store.db.execute("SELECT COALESCE(SUM(accrued-paid),0) s FROM accruals WHERE party='holders'").fetchone()
    return int(r["s"])
