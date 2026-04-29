from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.db.session import get_db
from app.db.models import FreightBill, Decision

router = APIRouter()


@router.get("")
async def get_metrics(db: Session = Depends(get_db)):
    total = db.query(func.count(FreightBill.id)).scalar()
    by_status = (
        db.query(FreightBill.status, func.count(FreightBill.id))
        .group_by(FreightBill.status)
        .all()
    )
    avg_confidence = db.query(func.avg(Decision.confidence_score)).scalar()
    by_decision = (
        db.query(Decision.decision_type, func.count(Decision.id))
        .group_by(Decision.decision_type)
        .all()
    )

    return {
        "total_bills": total,
        "by_status": {row[0]: row[1] for row in by_status},
        "avg_confidence_score": round(float(avg_confidence), 3) if avg_confidence else None,
        "by_decision_type": {row[0]: row[1] for row in by_decision},
    }
