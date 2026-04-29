"""
Score node — computes weighted confidence score from all dimensions.

Dimensions & weights:
  carrier_match       0.15
  contract_match      0.25
  shipment_bol_match  0.20
  charge_validation   0.25
  duplicate_check     0.15
"""
from app.agent.state import AgentState
import structlog

log = structlog.get_logger()

WEIGHTS = {
    "carrier_match": 0.15,
    "contract_match": 0.25,
    "shipment_bol_match": 0.20,
    "charge_validation": 0.25,
    "duplicate_check": 0.15,
}


def score_node(state: AgentState) -> dict:
    breakdown = dict(state.get("confidence_breakdown", {}))
    validation_results = state.get("validation_results", [])

    # Duplicate check dimension (set by preflight)
    if "duplicate_check" not in breakdown:
        breakdown["duplicate_check"] = 0.0 if state.get("duplicate_of") else 1.0

    # Charge validation dimension — computed from validation_results
    if validation_results:
        breakdown["charge_validation"] = _score_validation(validation_results)
    else:
        breakdown["charge_validation"] = 0.5  # no rules ran → neutral

    # Ensure all dimensions have a score (default to 0 if missing)
    for dim in WEIGHTS:
        if dim not in breakdown:
            breakdown[dim] = 0.0

    # Weighted sum
    score = sum(WEIGHTS[dim] * breakdown[dim] for dim in WEIGHTS)
    score = round(min(max(score, 0.0), 1.0), 4)

    log.info("confidence_scored",
             score=score,
             carrier=breakdown.get("carrier_match"),
             contract=breakdown.get("contract_match"),
             shipment=breakdown.get("shipment_bol_match"),
             charges=breakdown.get("charge_validation"),
             duplicate=breakdown.get("duplicate_check"))

    return {
        "confidence_score": score,
        "confidence_breakdown": breakdown,
    }


def _score_validation(results: list[dict]) -> float:
    """Convert validation rule results into a 0-1 score."""
    if not results:
        return 0.5

    # Critical rules that hard-fail the score
    critical_fails = [
        r for r in results
        if r.get("rule") in ("cumulative_weight", "weight_vs_bol")
        and r.get("passed") is False
        and r.get("deviation_pct", 0) > 10
    ]
    if critical_fails:
        return 0.0

    passed = [r for r in results if r.get("passed") is True]
    failed = [r for r in results if r.get("passed") is False]
    unknown = [r for r in results if r.get("passed") is None]

    if not failed:
        return 1.0

    # Partial scoring: weight rules by severity
    total = len(passed) + len(failed)
    base_score = len(passed) / total if total > 0 else 0.5

    # Extra penalty for rate drift
    rate_rule = next((r for r in results if r["rule"] == "rate_vs_contract"), None)
    if rate_rule and rate_rule.get("passed") is False:
        drift = rate_rule.get("deviation_pct", 0)
        if drift > 5:
            base_score = max(0.0, base_score - 0.2)

    return round(base_score, 4)
