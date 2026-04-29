"""
Contract selection algorithm.
Primary discriminator: billed rate proximity.
"Newest contract" heuristics break when a carrier has overlapping standard + expedited contracts.
"""
from datetime import date
from decimal import Decimal
from sqlalchemy.orm import Session

from app.db.models import RateCard
from app.db.neo4j_store import neo4j_store


RATE_EPSILON = Decimal("0.01")  # ₹0.01 tolerance


def select_best_contract(
    carrier_id: str,
    lane: str,
    bill_date: date,
    billed_rate: float | None,
    db: Session,
) -> tuple[dict | None, float, str]:
    """
    Returns (contract_dict, confidence, reason).
    confidence is for the contract-match dimension only.
    """
    bill_date_str = str(bill_date)

    # Step 1: Get active contracts from Neo4j
    active = neo4j_store.get_active_contracts(carrier_id, lane, bill_date_str)

    if not active:
        all_contracts = neo4j_store.get_all_contracts_for_lane(carrier_id, lane)
        expired = [c for c in all_contracts if c.get("status") == "expired"]
        if expired:
            return None, 0.0, f"contract_expired:last_expired_{expired[0].get('expiry_date')}"
        return None, 0.0, "no_contract_found"

    # Step 2: Enrich with rate card from Postgres
    enriched = []
    for co in active:
        rc = (
            db.query(RateCard)
            .filter(RateCard.contract_id == co["id"], RateCard.lane == lane)
            .first()
        )
        if rc:
            enriched.append({"contract": co, "rate_card": rc})

    if not enriched:
        return active[0], 0.50, "contract_found_no_rate_card"

    if billed_rate is None:
        return enriched[0]["contract"], 0.55, "no_billed_rate_ambiguous"

    # Step 3: Score each by |billed_rate - contract_rate|
    billed = Decimal(str(billed_rate))
    scored = []
    for item in enriched:
        rc = item["rate_card"]
        contract_rate = rc.rate_per_kg or rc.alternate_rate_per_kg
        if contract_rate is None:
            continue
        diff = abs(billed - Decimal(str(contract_rate)))
        scored.append((item, diff))

    if not scored:
        return enriched[0]["contract"], 0.50, "rate_card_no_per_kg_rate"

    scored.sort(key=lambda x: x[1])
    best_item, best_diff = scored[0]
    best_contract = best_item["contract"]

    exact_matches = [s for s in scored if s[1] <= RATE_EPSILON]

    if len(exact_matches) == 1:
        # Multiple overlapping active contracts exist — rate narrows to one but no shipment ref to confirm
        if len(active) > 1:
            return best_contract, 0.65, f"unique_rate_match_but_{len(active)}_overlapping_contracts"
        return best_contract, 0.95, "unique_rate_match"
    elif len(exact_matches) > 1:
        return best_contract, 0.65, f"ambiguous_{len(exact_matches)}_contracts_same_rate"

    # No active contract exactly matches the billed rate.
    # Check whether the billed rate matches an EXPIRED contract — classic expired-contract billing.
    all_contracts = neo4j_store.get_all_contracts_for_lane(carrier_id, lane)
    for ec in all_contracts:
        if ec.get("status") != "expired":
            continue
        ec_rc = (
            db.query(RateCard)
            .filter(RateCard.contract_id == ec["id"], RateCard.lane == lane)
            .first()
        )
        if ec_rc:
            expired_rate = ec_rc.rate_per_kg or ec_rc.alternate_rate_per_kg
            if expired_rate and abs(billed - Decimal(str(expired_rate))) <= RATE_EPSILON:
                return best_contract, 0.10, f"billing_at_expired_contract_rate_{ec['id']}"

    if best_diff <= Decimal("1.00"):
        return best_contract, 0.70, f"close_rate_match_diff_{best_diff}"
    else:
        return best_contract, 0.40, f"rate_mismatch_diff_{best_diff}"