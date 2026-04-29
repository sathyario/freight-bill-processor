"""
Preflight node — fast-path duplicate detection before agent runs.
Checks Neo4j for existing FreightBill with same bill_number + carrier_id.
"""
from app.agent.state import AgentState
from app.db.neo4j_store import neo4j_store
import structlog

log = structlog.get_logger()


def preflight_node(state: AgentState) -> dict:
    bill = state["freight_bill"]
    bill_number = bill.get("bill_number", "")
    carrier_id = bill.get("carrier_id") or ""

    duplicate = neo4j_store.find_duplicate(bill_number, carrier_id)

    if duplicate:
        log.info("duplicate_detected", bill_id=bill["id"], duplicate_of=duplicate["id"])
        return {
            "duplicate_of": duplicate["id"],
            "decision": "duplicate_reject",
            "confidence_score": 0.0,
            "confidence_breakdown": {"duplicate_check": 0.0},
        }

    return {"duplicate_of": None}
