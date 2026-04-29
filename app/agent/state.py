from typing import TypedDict, Optional


class AgentState(TypedDict):
    freight_bill: dict
    carrier_normalized: Optional[str]
    duplicate_of: Optional[str]
    candidate_contracts: list
    matched_contract: Optional[dict]
    matched_shipment: Optional[dict]
    matched_bol: Optional[dict]
    validation_results: list
    confidence_score: float
    confidence_breakdown: dict
    decision: Optional[str]
    evidence_summary: Optional[str]
    human_decision: Optional[dict]
