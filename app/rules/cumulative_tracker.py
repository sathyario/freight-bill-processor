"""
Tracks cumulative billed weight across all processed FreightBills for a shipment.
Queries Neo4j (mutable graph) so it always reflects current state.
"""
from app.db.neo4j_store import neo4j_store


def get_prior_billed_weight(shipment_id: str) -> float:
    """Return sum of billed_weight_kg for all FreightBill nodes referencing this shipment."""
    if not shipment_id:
        return 0.0
    return neo4j_store.get_cumulative_billed_weight(shipment_id)
