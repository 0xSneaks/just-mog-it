"""Is this coin mog-eligible? (owner brief 2026-11-07, thresholds PROPOSED, in policy.yaml)

A coin is eligible when it RUGGED or was LAUNCHED WRONG. Anything else is refused —
the bot is a rescue tool, not a copycat launcher.

rug.rule = strict (default): (LP pulled OR dev dumped) AND price drop.
           A price drop alone never qualifies — most pump.fun coins drop 70% in an hour.
rug.rule = any: any one signal qualifies (owner's original wording; flip in policy)."""
from dataclasses import dataclass
from decimal import Decimal

@dataclass
class CoinSnapshot:
    """Read-only facts about the ORIGINAL coin, gathered by the dossier adapters.
    None = unknown; unknown never counts as a signal."""
    quote_symbol: str = None            # e.g. "SOL", "USDC", "FARTCOIN"
    venue: str = None                   # e.g. "pump.fun", "mogz.fun", "raydium"
    venue_alive: bool = None            # venue still trades this coin
    graduated: bool = None              # off the bonding curve (LP exists)
    lp_pulled: bool = None              # LP removed (only meaningful when graduated / non-curve)
    dev_sold_pct_of_holdings: Decimal = None
    price_drop_pct_window: Decimal = None   # % drop within policy window from launch
    pair_liquidity_sol: Decimal = None

def evaluate(snap, policy):
    """Return (eligible: bool, reasons: list[str]). Reasons are the public-safe facts used."""
    r, w = policy.rug, policy.wrong_pair
    reasons = []
    curve_only = snap.graduated is False and snap.venue == "pump.fun"
    lp = bool(snap.lp_pulled) and not curve_only
    dump = snap.dev_sold_pct_of_holdings is not None and \
        Decimal(str(snap.dev_sold_pct_of_holdings)) > Decimal(str(r["dev_sold_pct_of_holdings"]))
    drop = snap.price_drop_pct_window is not None and \
        Decimal(str(snap.price_drop_pct_window)) >= Decimal(str(r["price_drop_pct"]))
    if lp: reasons.append("lp_pulled")
    if dump: reasons.append("dev_dumped")
    if drop: reasons.append("price_crashed")
    if r["rule"] == "strict":
        rugged = (lp or dump) and drop
    else:
        rugged = lp or dump or drop

    wrong = []
    if snap.quote_symbol is not None and snap.quote_symbol.upper() != "SOL":
        wrong.append("non_sol_pair")
    if snap.venue_alive is False:
        wrong.append("dead_venue")
    if snap.pair_liquidity_sol is not None and \
            Decimal(str(snap.pair_liquidity_sol)) < Decimal(str(w["min_pair_liquidity_sol"])):
        wrong.append("no_liquidity")
    eligible = rugged or bool(wrong)
    return eligible, (reasons if rugged else []) + wrong
