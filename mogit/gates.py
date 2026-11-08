"""Money-core hard gates (owner §0), venue-agnostic, all fail-closed.
Checked in flow._preflight (before build AND again under the ledger lock before sign) and on
the simulated spend in flow._render / flow._confirm.
A gate failure raises GateError (a LedgerError), which the flow turns into REFUSED."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .ledger import LedgerError

SOL_MINT = "So11111111111111111111111111111111111111112"
PER_LAUNCH_CAP_LAMPORTS = 25_000_000   # 0.025 SOL hard cap per launch
MAX_LAUNCHES_PER_DAY = 3


class GateError(LedgerError):
    pass


def utc_day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).date()


def launches_today(state, now):
    """Settled launches + pending reservations stamped today (UTC). An entry without a
    numeric ts counts as today (fail closed)."""
    today = utc_day(now)
    n = 0
    for e in list(state.get("launches", [])) + list(state.get("pending", [])):
        ts = e.get("ts") if isinstance(e, dict) else None
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            n += 1
            continue
        try:
            n += utc_day(ts) == today
        except (OverflowError, OSError, ValueError):
            n += 1
    return n


def check_quote(q):
    if not isinstance(q, dict):
        raise GateError("quote must be an object")
    if q.get("quote_mint") != SOL_MINT:
        raise GateError("SOL pairs only: quote mint is not wrapped SOL")
    fb = q.get("first_buy_lamports")
    if type(fb) is not int or fb != 0:
        raise GateError("first buy must be exactly 0 (no dev buy)")
    try:
        sol = Decimal(str(q["sol"]))
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise GateError("quote SOL amount is invalid") from None
    if not sol.is_finite() or sol < 0:
        raise GateError("quote SOL amount must be non-negative whole lamports")
    # Compare the original decimal before any context-sensitive arithmetic.
    if sol > Decimal("0.025"):
        raise GateError(f"quote {sol} SOL exceeds the 0.025 SOL per-launch cap")
    if sol.is_zero():
        return 0
    sign, digits, exponent = sol.as_tuple()
    # Tuple construction shifts the exponent exactly, independent of precision,
    # rounding mode, and the context's exponent limits (unlike multiplication).
    lamports = Decimal((sign, digits, exponent + 9))
    if lamports != lamports.to_integral_value():
        raise GateError("quote SOL amount must be non-negative whole lamports")
    return int(lamports)


def check_spend(spend_lamports):
    try:
        spend = Decimal(str(spend_lamports))
    except (TypeError, ValueError, InvalidOperation):
        raise GateError("simulated spend is invalid") from None
    if (not spend.is_finite() or spend < 0 or
            spend != spend.to_integral_value()):
        raise GateError("simulated spend must be non-negative whole lamports")
    if spend > PER_LAUNCH_CAP_LAMPORTS:
        raise GateError(f"simulated spend {spend_lamports} lamports exceeds the 0.025 SOL per-launch cap")
    return int(spend)


def payer_spend(delta):
    """Validate the simulated payer delta before converting it to an integer spend."""
    try:
        value = Decimal(str(delta))
    except (TypeError, ValueError, InvalidOperation):
        raise GateError("simulation delta is invalid") from None
    if not value.is_finite() or value != value.to_integral_value():
        raise GateError("simulation delta must be a whole number of lamports")
    if value > 0:
        raise GateError("simulation delta positive: payer would gain lamports")
    if value < -PER_LAUNCH_CAP_LAMPORTS:
        raise GateError("simulated spend exceeds the 0.025 SOL per-launch cap")
    return check_spend(value.copy_negate())


def check_preflight(ledger, state, q, now):
    """Kill switch, daily limit, quote gates. `state` is ledger.load() from the same read."""
    if ledger.kill_on():
        raise GateError("kill switch is ON")
    if launches_today(state, now) >= MAX_LAUNCHES_PER_DAY:
        raise GateError(f"daily limit: {MAX_LAUNCHES_PER_DAY} launches per UTC day reached")
    check_quote(q)
