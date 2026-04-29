from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from app.db.session import get_db
from app.db.models import FreightBill, Decision, AuditLog

router = APIRouter()


class ReviewDecision(BaseModel):
    decision: str          # approve | dispute | modify
    notes: Optional[str] = None


@router.get("/review-queue")
async def get_review_queue(db: Session = Depends(get_db)):
    bills = (
        db.query(FreightBill)
        .filter(FreightBill.status == "pending_review")
        .order_by(FreightBill.created_at)
        .all()
    )
    result = []
    for fb in bills:
        decision = db.query(Decision).filter(Decision.freight_bill_id == fb.id).first()
        result.append({
            "id": fb.id,
            "carrier_name": fb.carrier_name,
            "bill_number": fb.bill_number,
            "bill_date": str(fb.bill_date),
            "lane": fb.lane,
            "billed_weight_kg": fb.billed_weight_kg,
            "total_amount": float(fb.total_amount) if fb.total_amount else None,
            "confidence_score": decision.confidence_score if decision else None,
            "evidence": decision.evidence if decision else None,
            "reasoning": decision.reasoning if decision else None,
        })
    return {"count": len(result), "items": result}


@router.post("/review/{bill_id}")
async def submit_review(
    bill_id: str,
    payload: ReviewDecision,
    db: Session = Depends(get_db),
):
    fb = db.query(FreightBill).filter(FreightBill.id == bill_id).first()
    if not fb:
        raise HTTPException(status_code=404, detail="Freight bill not found")
    if fb.status != "pending_review":
        raise HTTPException(status_code=400, detail=f"Bill is not pending review (status: {fb.status})")

    decision_type = f"human_{payload.decision}"
    final_status = "approved" if payload.decision == "approve" else "disputed"

    # Update or create decision record
    decision = db.query(Decision).filter(Decision.freight_bill_id == bill_id).first()
    if decision:
        decision.decision_type = decision_type
        decision.decided_by = "human"
        decision.reasoning = payload.notes or decision.reasoning
    else:
        decision = Decision(
            freight_bill_id=bill_id,
            decision_type=decision_type,
            decided_by="human",
            reasoning=payload.notes,
        )
        db.add(decision)

    fb.status = final_status
    db.add(AuditLog(
        freight_bill_id=bill_id,
        event_type="human_review_submitted",
        payload={"decision": payload.decision, "notes": payload.notes},
    ))
    db.commit()

    # Resume LangGraph agent
    from app.agent.graph import resume_agent
    await resume_agent(bill_id, {"decision": payload.decision, "notes": payload.notes})

    return {"id": bill_id, "status": final_status, "decision": decision_type}
