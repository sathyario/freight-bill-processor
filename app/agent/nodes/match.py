"""
Match node — Neo4j graph traversal to find Contract, Shipment, BOL.
Two paths:
  PATH 1 (high confidence): shipment_reference present → direct graph walk
  PATH 2 (lower confidence): no reference → contract selection by rate proximity
"""
from app.agent.state import AgentState
from app.db.session import SessionLocal
from app.db.models import RateCard
from app.db.neo4j_store import neo4j_store
from app.rules.contract_selector import select_best_contract
import structlog

log = structlog.get_logger()


def match_node(state: AgentState) -> dict:
    bill = state["freight_bill"]
    carrier_id = state.get("carrier_normalized") or bill.get("carrier_id")
    shipment_ref = bill.get("shipment_reference")
    lane = bill.get("lane", "")
    bill_date_str = str(bill.get("bill_date", ""))
    billed_rate = bill.get("rate_per_kg")

    db = SessionLocal()
    try:
        if shipment_ref:
            return _path1_with_shipment(bill, carrier_id, shipment_ref, lane, bill_date_str, billed_rate, db, state)
        else:
            return _path2_without_shipment(bill, carrier_id, lane, bill_date_str, billed_rate, db, state)
    finally:
        db.close()


def _path1_with_shipment(bill, carrier_id, shipment_ref, lane, bill_date_str, billed_rate, db, state):
    """Follow shipment reference directly through Neo4j graph."""
    result = neo4j_store.get_shipment_with_bol(shipment_ref)

    if not result:
        log.warning("shipment_not_found", shipment_ref=shipment_ref)
        return _path2_without_shipment(bill, carrier_id, lane, bill_date_str, billed_rate, db, state)

    shipment = result["shipment"]
    bols = result["bols"]
    best_bol = bols[0] if bols else None

    # Get contract via Postgres (shipment has contract_id)
    contract_id = shipment.get("contract_id")
    matched_contract = None
    contract_confidence = 0.5

    if contract_id:
        from app.db.models import Contract
        contract = db.query(Contract).filter_by(id=contract_id).first()
        if contract:
            matched_contract = {
                "id": contract.id,
                "carrier_id": contract.carrier_id,
                "effective_date": str(contract.effective_date),
                "expiry_date": str(contract.expiry_date),
                "status": contract.status,
            }
            contract_confidence = 0.95

    log.info("path1_match", shipment_id=shipment_ref, contract_id=contract_id, bols=len(bols))

    return {
        "matched_shipment": shipment,
        "matched_bol": best_bol,
        "matched_contract": matched_contract,
        "candidate_contracts": [matched_contract] if matched_contract else [],
        "confidence_breakdown": {
            **state.get("confidence_breakdown", {}),
            "contract_match": contract_confidence,
            "contract_match_note": f"via_shipment_ref:{shipment_ref}",
            "shipment_bol_match": 1.0 if best_bol else 0.2,
            "shipment_bol_note": f"bol_found:{best_bol['id'] if best_bol else 'none'}",
        },
    }


def _path2_without_shipment(bill, carrier_id, lane, bill_date_str, billed_rate, db, state):
    """No shipment reference — use contract selection algorithm."""
    if not carrier_id:
        log.warning("no_carrier_id_no_shipment_ref", bill_id=bill.get("id"))
        return {
            "matched_shipment": None,
            "matched_bol": None,
            "matched_contract": None,
            "candidate_contracts": [],
            "confidence_breakdown": {
                **state.get("confidence_breakdown", {}),
                "contract_match": 0.0,
                "contract_match_note": "no_carrier_id",
                "shipment_bol_match": 0.0,
                "shipment_bol_note": "no_shipment_ref",
            },
        }

    from datetime import date
    bill_date = date.fromisoformat(bill_date_str) if bill_date_str else date.today()

    contract, contract_conf, reason = select_best_contract(
        carrier_id, lane, bill_date, billed_rate, db
    )

    log.info("path2_contract_selected",
             carrier_id=carrier_id, lane=lane,
             contract_id=contract.get("id") if contract else None,
             confidence=contract_conf, reason=reason)

    return {
        "matched_shipment": None,
        "matched_bol": None,
        "matched_contract": contract,
        "candidate_contracts": [contract] if contract else [],
        "confidence_breakdown": {
            **state.get("confidence_breakdown", {}),
            "contract_match": contract_conf,
            "contract_match_note": reason,
            "shipment_bol_match": 0.2,
            "shipment_bol_note": "no_shipment_ref_provided",
        },
    }
