from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from datetime import date
from typing import Optional

from app.db.session import get_db
from app.db.models import FreightBill, Decision, AuditLog
from app.db.neo4j_store import neo4j_store

router = APIRouter()


class FreightBillInput(BaseModel):
    id: str
    carrier_id: Optional[str] = None
    carrier_name: str
    bill_number: str
    bill_date: date
    shipment_reference: Optional[str] = None
    lane: str
    billed_weight_kg: float
    rate_per_kg: Optional[float] = None
    billing_unit: Optional[str] = "kg"
    base_charge: Optional[float] = None
    fuel_surcharge: Optional[float] = None
    gst_amount: Optional[float] = None
    total_amount: Optional[float] = None


@router.post("", status_code=202)
async def ingest_freight_bill(
    payload: FreightBillInput,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    # Check if already exists
    existing = db.query(FreightBill).filter(FreightBill.id == payload.id).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Freight bill {payload.id} already exists")

    # Persist to DB
    fb = FreightBill(
        id=payload.id,
        carrier_id=payload.carrier_id,
        carrier_name=payload.carrier_name,
        bill_number=payload.bill_number,
        bill_date=payload.bill_date,
        shipment_reference=payload.shipment_reference,
        lane=payload.lane,
        billed_weight_kg=payload.billed_weight_kg,
        rate_per_kg=payload.rate_per_kg,
        billing_unit=payload.billing_unit or "kg",
        base_charge=payload.base_charge,
        fuel_surcharge=payload.fuel_surcharge,
        gst_amount=payload.gst_amount,
        total_amount=payload.total_amount,
        status="pending",
    )
    db.add(fb)
    db.add(AuditLog(freight_bill_id=payload.id, event_type="ingested", payload=payload.model_dump(mode="json")))
    db.commit()

    # Trigger agent in background
    background_tasks.add_task(_run_agent, payload.model_dump(mode="json"))

    return {"id": payload.id, "status": "pending", "message": "Agent triggered"}


@router.get("/{bill_id}")
async def get_freight_bill(bill_id: str, db: Session = Depends(get_db)):
    fb = db.query(FreightBill).filter(FreightBill.id == bill_id).first()
    if not fb:
        raise HTTPException(status_code=404, detail="Freight bill not found")

    decision = db.query(Decision).filter(Decision.freight_bill_id == bill_id).first()
    graph_chain = neo4j_store.get_evidence_chain(bill_id)

    return {
        "id": fb.id,
        "carrier_name": fb.carrier_name,
        "bill_number": fb.bill_number,
        "bill_date": str(fb.bill_date),
        "lane": fb.lane,
        "billed_weight_kg": fb.billed_weight_kg,
        "total_amount": float(fb.total_amount) if fb.total_amount else None,
        "status": fb.status,
        "decision": {
            "type": decision.decision_type,
            "confidence_score": decision.confidence_score,
            "evidence": decision.evidence,
            "reasoning": decision.reasoning,
            "decided_by": decision.decided_by,
            "decided_at": str(decision.decided_at),
        } if decision else None,
        "graph_evidence": graph_chain,
    }


async def _run_agent(bill_data: dict):
    """Background task — runs LangGraph agent."""
    from app.agent.graph import run_agent
    await run_agent(bill_data)
