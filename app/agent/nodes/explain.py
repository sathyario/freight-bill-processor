"""
Explain node — LLM generates a human-readable evidence summary.
Persists decision + evidence to Postgres and adds FreightBill node to Neo4j.
"""
from app.agent.state import AgentState
from app.db.session import SessionLocal
from app.db.models import FreightBill, Decision, AuditLog
from app.db.neo4j_store import neo4j_store
from app.config import settings
import structlog

log = structlog.get_logger()

DECISION_TO_STATUS = {
    "auto_approve": "approved",
    "auto_dispute": "disputed",
    "duplicate_reject": "duplicate",
    "human_approve": "approved",
    "human_dispute": "disputed",
    "human_modify": "approved",
}


def explain_node(state: AgentState) -> dict:
    bill = state["freight_bill"]
    decision = state.get("decision", "auto_dispute")
    score = state.get("confidence_score", 0.0)
    breakdown = state.get("confidence_breakdown", {})
    validation_results = state.get("validation_results", [])
    matched_contract = state.get("matched_contract")
    matched_shipment = state.get("matched_shipment")
    matched_bol = state.get("matched_bol")
    human_decision = state.get("human_decision")

    # Generate LLM explanation
    summary = _generate_summary(
        bill, decision, score, breakdown, validation_results,
        matched_contract, matched_shipment, matched_bol, human_decision
    )

    # Build evidence dict for storage
    evidence = {
        "confidence_breakdown": breakdown,
        "validation_results": validation_results,
        "matched_contract_id": matched_contract.get("id") if matched_contract else None,
        "matched_shipment_id": matched_shipment.get("id") if matched_shipment else None,
        "matched_bol_id": matched_bol.get("id") if matched_bol else None,
        "duplicate_of": state.get("duplicate_of"),
    }

    # Persist to Postgres
    _save_to_db(bill, decision, score, evidence, summary, human_decision)

    # Add FreightBill node to Neo4j (enables future cumulative checks)
    neo4j_store.add_freight_bill_node(
        bill=bill,
        shipment_id=matched_shipment.get("id") if matched_shipment else bill.get("shipment_reference"),
        contract_id=matched_contract.get("id") if matched_contract else None,
    )

    log.info("decision_finalized",
             bill_id=bill.get("id"), decision=decision, score=score)

    return {"evidence_summary": summary}


def _generate_summary(bill, decision, score, breakdown, validation_results,
                      contract, shipment, bol, human_decision) -> str:
    if not settings.gemini_api_key or settings.gemini_api_key == "your_gemini_api_key_here":
        return _fallback_summary(bill, decision, score, breakdown, validation_results)

    try:
        import google.generativeai as genai  # type: ignore[import]
        genai.configure(api_key=settings.gemini_api_key)
        model = genai.GenerativeModel("gemini-2.5-flash")

        failed_rules = [r for r in validation_results if r.get("passed") is False]
        passed_rules = [r for r in validation_results if r.get("passed") is True]

        prompt = f"""You are a logistics billing auditor. Write a brief 2-3 sentence explanation of this freight bill decision.

Bill: {bill.get('id')} | Carrier: {bill.get('carrier_name')} | Lane: {bill.get('lane')}
Billed: {bill.get('billed_weight_kg')}kg @ ₹{bill.get('rate_per_kg')}/kg = ₹{bill.get('total_amount')}
Decision: {decision.upper()} (confidence: {score:.0%})
Contract matched: {contract.get('id') if contract else 'None'}
Shipment matched: {shipment.get('id') if shipment else 'None'}
BOL matched: {bol.get('id') if bol else 'None'}
Rules passed: {[r['rule'] for r in passed_rules]}
Rules failed: {[f"{r['rule']}(deviation={r.get('deviation_pct','?')}%)" for r in failed_rules]}
Human override: {human_decision}

Write a concise explanation for the ops team. Be specific about what matched or failed."""

        response = model.generate_content(prompt)
        return response.text.strip()

    except Exception as e:
        log.warning("llm_explain_failed", error=str(e))
        return _fallback_summary(bill, decision, score, breakdown, validation_results)


def _fallback_summary(bill, decision, score, breakdown, validation_results) -> str:
    failed = [r["rule"] for r in validation_results if r.get("passed") is False]
    passed = [r["rule"] for r in validation_results if r.get("passed") is True]
    parts = [
        f"Decision: {decision} (confidence: {score:.0%}).",
        f"Passed checks: {', '.join(passed) or 'none'}.",
    ]
    if failed:
        parts.append(f"Failed checks: {', '.join(failed)}.")
    return " ".join(parts)


def _save_to_db(bill, decision, score, evidence, summary, human_decision):
    db = SessionLocal()
    try:
        status = DECISION_TO_STATUS.get(decision, "disputed")
        decided_by = "human" if human_decision else "agent"

        fb = db.query(FreightBill).filter_by(id=bill["id"]).first()
        if fb:
            fb.status = status

        existing = db.query(Decision).filter_by(freight_bill_id=bill["id"]).first()
        if existing:
            existing.decision_type = decision
            existing.confidence_score = score
            existing.evidence = evidence
            existing.reasoning = summary
            existing.decided_by = decided_by
        else:
            db.add(Decision(
                freight_bill_id=bill["id"],
                decision_type=decision,
                confidence_score=score,
                evidence=evidence,
                reasoning=summary,
                decided_by=decided_by,
            ))

        db.add(AuditLog(
            freight_bill_id=bill["id"],
            event_type=f"decision_{decision}",
            payload={"score": score, "decided_by": decided_by},
        ))
        db.commit()
    except Exception as e:
        db.rollback()
        log.error("save_decision_failed", bill_id=bill.get("id"), error=str(e))
        raise
    finally:
        db.close()
