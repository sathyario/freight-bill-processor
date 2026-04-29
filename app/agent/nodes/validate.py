"""
Validate node — runs all deterministic charge/weight/rate rules.
No LLM. Pure Python math against contracted values.
"""
from datetime import date
from app.agent.state import AgentState
from app.db.session import SessionLocal
from app.db.models import RateCard
from app.rules.charge_validator import validate_all
from app.rules.cumulative_tracker import get_prior_billed_weight
import structlog

log = structlog.get_logger()


def validate_node(state: AgentState) -> dict:
    bill = state["freight_bill"]
    matched_contract = state.get("matched_contract")
    matched_bol = state.get("matched_bol")
    matched_shipment = state.get("matched_shipment")

    bill_date_str = str(bill.get("bill_date", ""))
    bill_date = date.fromisoformat(bill_date_str) if bill_date_str else date.today()

    db = SessionLocal()
    try:
        # Fetch rate card
        rate_card = None
        if matched_contract:
            lane = bill.get("lane", "")
            rate_card = (
                db.query(RateCard)
                .filter(
                    RateCard.contract_id == matched_contract["id"],
                    RateCard.lane == lane,
                )
                .first()
            )

        # BOL actual weight
        bol_weight = None
        if matched_bol:
            bol_weight = matched_bol.get("actual_weight_kg")

        # Shipment total weight + cumulative billed
        shipment_total = None
        cumulative = 0.0
        shipment_ref = bill.get("shipment_reference")
        if matched_shipment:
            shipment_total = matched_shipment.get("total_weight_kg")
            cumulative = get_prior_billed_weight(matched_shipment.get("id") or shipment_ref or "")
        elif shipment_ref:
            # Even without a full match, check cumulative via Neo4j
            from app.db.models import Shipment
            shp = db.query(Shipment).filter_by(id=shipment_ref).first()
            if shp:
                shipment_total = shp.total_weight_kg
                cumulative = get_prior_billed_weight(shipment_ref)

        results = validate_all(
            bill=bill,
            rate_card=rate_card,
            bol_actual_weight=bol_weight,
            shipment_total_weight=shipment_total,
            cumulative_billed=cumulative,
            bill_date=bill_date,
        )

        log.info("validation_complete",
                 bill_id=bill.get("id"),
                 rules_run=len(results),
                 passed=sum(1 for r in results if r.get("passed") is True),
                 failed=sum(1 for r in results if r.get("passed") is False))

        return {"validation_results": results}
    finally:
        db.close()
