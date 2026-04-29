"""
Decide node — routes to auto_approve, auto_dispute, or interrupt() for human review.
This is where the real LangGraph interrupt/resume pattern lives.
"""
from langgraph.types import interrupt
from app.agent.state import AgentState
import structlog

log = structlog.get_logger()

AUTO_APPROVE_THRESHOLD = 0.80
AUTO_DISPUTE_THRESHOLD = 0.40


def decide_node(state: AgentState) -> dict:
    score = state.get("confidence_score", 0.0)
    bill_id = state["freight_bill"].get("id")
    validation_results = state.get("validation_results", [])

    # Already decided by preflight (duplicate)
    if state.get("decision") == "duplicate_reject":
        log.info("decision_duplicate_reject", bill_id=bill_id)
        return {"decision": "duplicate_reject"}

    # Hard override: cumulative over-billing is always auto_dispute regardless of overall score.
    # Even a clean carrier/contract/shipment match cannot save a bill that overbills the shipment.
    cumulative_rule = next((r for r in validation_results if r.get("rule") == "cumulative_weight"), None)
    if cumulative_rule and cumulative_rule.get("passed") is False:
        log.info("decision_auto_dispute_cumulative_overbilling",
                 bill_id=bill_id, score=score,
                 overage_kg=cumulative_rule.get("overage_kg"))
        return {"decision": "auto_dispute"}

    # Hard override: billing at an expired contract rate is always auto_dispute.
    # The billed rate matches a lapsed contract and no active contract covers it.
    contract_match_note = state.get("confidence_breakdown", {}).get("contract_match_note", "")
    if "billing_at_expired_contract_rate" in (contract_match_note or ""):
        log.info("decision_auto_dispute_expired_contract", bill_id=bill_id, note=contract_match_note)
        return {"decision": "auto_dispute"}

    # Soft override: flag for human review when rate drift > 5% or FTL/unit mismatch detected.
    # A strong carrier/shipment match can otherwise mask these into auto_approve territory.
    rate_rule = next((r for r in validation_results if r.get("rule") == "rate_vs_contract"), None)
    if rate_rule and rate_rule.get("passed") is False and rate_rule.get("deviation_pct", 0) > 5:
        log.info("decision_force_review_rate_drift",
                 bill_id=bill_id, deviation_pct=rate_rule.get("deviation_pct"))
        score = min(score, AUTO_APPROVE_THRESHOLD - 0.01)  # Cap below auto-approve

    unit_rule = next((r for r in validation_results if r.get("rule") == "unit_reconciliation"), None)
    if unit_rule and unit_rule.get("passed") is False:
        log.info("decision_force_review_unit_mismatch",
                 bill_id=bill_id, cost_diff=unit_rule.get("cost_difference"))
        score = min(score, AUTO_APPROVE_THRESHOLD - 0.01)  # Cap below auto-approve

    if score >= AUTO_APPROVE_THRESHOLD:
        log.info("decision_auto_approve", bill_id=bill_id, score=score)
        return {"decision": "auto_approve"}

    if score < AUTO_DISPUTE_THRESHOLD:
        log.info("decision_auto_dispute", bill_id=bill_id, score=score)
        return {"decision": "auto_dispute"}

    # Mid-range: pause for human review using LangGraph interrupt()
    log.info("decision_interrupt_for_review", bill_id=bill_id, score=score)

    interrupt_payload = {
        "bill_id": bill_id,
        "confidence_score": score,
        "confidence_breakdown": state.get("confidence_breakdown", {}),
        "validation_results": state.get("validation_results", []),
        "matched_contract": state.get("matched_contract"),
        "matched_shipment": state.get("matched_shipment"),
        "matched_bol": state.get("matched_bol"),
        "reason": _build_reason(state),
    }

    # This pauses the graph — state is checkpointed to Postgres.
    # Execution resumes when POST /review/{id} calls Command(resume=...).
    human_input = interrupt(interrupt_payload)

    # After resume — human_input is {"decision": "approve"|"dispute", "notes": "..."}
    decision_type = f"human_{human_input.get('decision', 'approve')}"
    log.info("agent_resumed", bill_id=bill_id, human_decision=decision_type)

    return {
        "decision": decision_type,
        "human_decision": human_input,
    }


def _build_reason(state: AgentState) -> str:
    breakdown = state.get("confidence_breakdown", {})
    score = state.get("confidence_score", 0.0)
    issues = []

    if breakdown.get("contract_match", 1.0) < 0.8:
        issues.append(f"contract_match={breakdown['contract_match']:.2f} ({breakdown.get('contract_match_note','')})")
    if breakdown.get("shipment_bol_match", 1.0) < 0.8:
        issues.append(f"shipment_bol={breakdown['shipment_bol_match']:.2f} ({breakdown.get('shipment_bol_note','')})")
    if breakdown.get("charge_validation", 1.0) < 0.8:
        for r in state.get("validation_results", []):
            if r.get("passed") is False:
                issues.append(f"{r['rule']}:deviation={r.get('deviation_pct','?')}%")

    return f"score={score:.2f} | issues: {'; '.join(issues) or 'ambiguous_match'}"
