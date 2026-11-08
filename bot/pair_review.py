"""Validate a proposed Pump pair decision for the engine's offline preview."""
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation

from .live.pairs import PairDataError


@dataclass(frozen=True)
class EvaluatorResult:
    action: str
    reason: str
    target_mint: str
    selected_quote_mint: str | None


class PairEvaluatorError(RuntimeError):
    """A categorized evaluator failure safe to record by kind."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind


@dataclass(frozen=True)
class QuoteCandidate:
    mint: str
    asset_class: str
    issuer: str | None = None
    listing_provider: str | None = None


@dataclass(frozen=True)
class LiquidityMeasurement:
    kind: str
    usd: Decimal | None


def measure_curve(reserves, decimals, usd_per_quote):
    """Value real quote reserves using raw token units and a matching price."""
    if reserves is None or decimals is None or usd_per_quote is None:
        return LiquidityMeasurement("curve_reserves", None)
    if type(reserves) is not int or reserves < 0 or type(decimals) is not int or not 0 <= decimals <= 255:
        raise PairDataError("invalid reserve units")
    price = _decimal(usd_per_quote)
    return LiquidityMeasurement("curve_reserves", Decimal(reserves).scaleb(-decimals) * price)


def _decimal(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise PairDataError("invalid liquidity value") from exc
    if not result.is_finite() or result < 0:
        raise PairDataError("invalid liquidity value")
    return result


@dataclass(frozen=True)
class PairContext:
    narrative: str
    underlying: str
    candidates: tuple[QuoteCandidate, ...]
    liquidity: LiquidityMeasurement | None = None
    floor_usd: Decimal | None = None


@dataclass(frozen=True)
class ReviewResult:
    action: str
    reason: str
    selected_quote_mint: str | None = None
    kind: str | None = None


def validate_decision(raw, pair, registry, context):
    if isinstance(raw, dict):
        try:
            raw = EvaluatorResult(**raw)
        except TypeError:
            return ReviewResult("reject", "invalid_decision")
    if not isinstance(raw, EvaluatorResult) or not isinstance(raw.reason, str):
        return ReviewResult("reject", "invalid_decision")
    if raw.target_mint != pair.base_mint:
        return ReviewResult("reject", "target_mismatch")
    if raw.action not in ("keep", "relaunch", "ask", "reject"):
        return ReviewResult("reject", "invalid_decision")
    quote = raw.selected_quote_mint
    if raw.action in ("ask", "reject"):
        if quote is not None:
            return ReviewResult("reject", "invalid_decision")
        if raw.action == "ask" and len({c.mint for c in context.candidates}) < 2:
            return ReviewResult("reject", "no_eligible_alternatives")
        return ReviewResult(raw.action, "ambiguous_quote" if raw.action == "ask" else "evaluator_rejected")
    if not isinstance(quote, str) or quote not in {c.mint for c in registry}:
        return ReviewResult("reject", "quote_not_supported")
    if quote not in {c.mint for c in context.candidates}:
        return ReviewResult("reject", "quote_not_approved_for_narrative")
    if raw.action == "keep":
        if quote != pair.quote_mint:
            return ReviewResult("reject", "keep_quote_mismatch")
        return ReviewResult("keep", "narrative_fit", quote)
    if quote == pair.quote_mint:
        return ReviewResult("reject", "same_quote")
    if context.floor_usd is None or _decimal(context.floor_usd) == 0:
        return ReviewResult("reject", "unset_floor")
    measure = context.liquidity
    expected_kind = "curve_reserves" if pair.venue == "bonding_curve" else "executable"
    if measure is None or measure.kind != expected_kind or measure.usd is None:
        return ReviewResult("reject", "missing_liquidity")
    if _decimal(measure.usd) < _decimal(context.floor_usd):
        return ReviewResult("reject", "low_liquidity")
    return ReviewResult("relaunch", "verified_quote_change", quote)


class PairPreview:
    """Timeout ruling 2026-11-07: warm up once at boot, then retry the evaluator exactly
    once on a timeout. A second timeout is a skip (reject, kind "timeout"), never a launch."""

    def __init__(self, resolver, registry, context, evaluator, warm_up=None):
        self.resolver = resolver
        self.registry = registry
        self.context = context
        self.evaluator = evaluator
        self._warm_up = warm_up

    def warm_up(self):
        """Run the boot warm-up hook once. Returns True/False, or None with no hook.
        A failed warm-up never blocks boot; the single timeout retry still applies."""
        if self._warm_up is None:
            return None
        try:
            return bool(self._warm_up())
        except Exception:
            return False

    def _evaluate(self, pair, context):
        try:
            return self.evaluator(pair, context)
        except PairEvaluatorError as exc:
            if exc.kind != "timeout":
                raise
        return self.evaluator(pair, context)  # exactly one retry; a second timeout propagates

    def review(self, base_mint):
        try:
            pair = self.resolver.resolve_pair(base_mint)
            if pair is None or pair.base_mint != base_mint:
                return ReviewResult("reject", "unresolved_pool"), None
        except (PairDataError, KeyError, RuntimeError, OSError):
            return ReviewResult("reject", "unresolved_pool"), None

        try:
            registry = self.registry.refresh()
            allowed = {row.mint for row in registry}
            context = self.context(pair)
            if not isinstance(context, PairContext):
                raise PairDataError("missing pair context")
            classification = next(
                (candidate.asset_class for candidate in context.candidates if candidate.mint == pair.quote_mint),
                None,
            )
            if classification is not None:
                pair = replace(pair, quote_class=classification)
            # Enforce eligibility before the model sees any candidate.
            context = PairContext(
                context.narrative, context.underlying,
                tuple(c for c in context.candidates if c.mint in allowed),
                context.liquidity, context.floor_usd,
            )
            raw = self._evaluate(pair, context)
            return validate_decision(raw, pair, registry, context), context
        except PairEvaluatorError as exc:
            return ReviewResult("reject", "pair_review_failed", kind=exc.kind), None
        except (PairDataError, KeyError, RuntimeError, OSError):
            return ReviewResult("reject", "pair_review_failed"), None
