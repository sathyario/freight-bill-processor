from datetime import datetime, date
from sqlalchemy import (
    Column, String, Float, Integer, Date, DateTime,
    ForeignKey, Text, Boolean, JSON, Numeric
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db.session import Base


# ── Reference data (loaded from seed_data.json) ──────────────────────────────

class Carrier(Base):
    __tablename__ = "carriers"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    carrier_code = Column(String, nullable=False)
    gstin = Column(String)
    bank_account = Column(String)
    status = Column(String, default="active")
    onboarded_on = Column(Date)

    contracts = relationship("Contract", back_populates="carrier")
    shipments = relationship("Shipment", back_populates="carrier")


class Contract(Base):
    __tablename__ = "contracts"

    id = Column(String, primary_key=True)
    carrier_id = Column(String, ForeignKey("carriers.id"), nullable=False)
    effective_date = Column(Date, nullable=False)
    expiry_date = Column(Date, nullable=False)
    status = Column(String, default="active")
    notes = Column(Text)

    carrier = relationship("Carrier", back_populates="contracts")
    rate_cards = relationship("RateCard", back_populates="contract")
    shipments = relationship("Shipment", back_populates="contract")


class RateCard(Base):
    __tablename__ = "rate_cards"

    id = Column(Integer, primary_key=True, autoincrement=True)
    contract_id = Column(String, ForeignKey("contracts.id"), nullable=False)
    lane = Column(String, nullable=False)
    description = Column(String)

    # Per-kg billing
    rate_per_kg = Column(Numeric(10, 2))

    # Per-unit billing (FTL)
    rate_per_unit = Column(Numeric(10, 2))
    unit = Column(String)                    # e.g. "FTL"
    unit_capacity_kg = Column(Integer)       # e.g. 8000
    alternate_rate_per_kg = Column(Numeric(10, 2))

    min_charge = Column(Numeric(10, 2), nullable=False)
    fuel_surcharge_pct = Column(Float, nullable=False)

    # Mid-contract revision
    revised_on = Column(Date)
    revised_fuel_surcharge_pct = Column(Float)

    contract = relationship("Contract", back_populates="rate_cards")


class Shipment(Base):
    __tablename__ = "shipments"

    id = Column(String, primary_key=True)
    carrier_id = Column(String, ForeignKey("carriers.id"), nullable=False)
    contract_id = Column(String, ForeignKey("contracts.id"), nullable=False)
    lane = Column(String, nullable=False)
    shipment_date = Column(Date, nullable=False)
    status = Column(String, default="pending")
    total_weight_kg = Column(Float, nullable=False)
    notes = Column(Text)

    carrier = relationship("Carrier", back_populates="shipments")
    contract = relationship("Contract", back_populates="shipments")
    bols = relationship("BillOfLading", back_populates="shipment")


class BillOfLading(Base):
    __tablename__ = "bills_of_lading"

    id = Column(String, primary_key=True)
    shipment_id = Column(String, ForeignKey("shipments.id"), nullable=False)
    delivery_date = Column(Date, nullable=False)
    actual_weight_kg = Column(Float, nullable=False)
    notes = Column(Text)

    shipment = relationship("Shipment", back_populates="bols")


# ── Agent state ───────────────────────────────────────────────────────────────

class FreightBill(Base):
    __tablename__ = "freight_bills"

    id = Column(String, primary_key=True)
    carrier_id = Column(String, ForeignKey("carriers.id"), nullable=True)
    carrier_name = Column(String, nullable=False)
    bill_number = Column(String, nullable=False)
    bill_date = Column(Date, nullable=False)
    shipment_reference = Column(String, nullable=True)
    lane = Column(String, nullable=False)
    billed_weight_kg = Column(Float, nullable=False)
    rate_per_kg = Column(Numeric(10, 2))
    billing_unit = Column(String, default="kg")
    base_charge = Column(Numeric(10, 2))
    fuel_surcharge = Column(Numeric(10, 2))
    gst_amount = Column(Numeric(10, 2))
    total_amount = Column(Numeric(10, 2))
    # pending | processing | pending_review | approved | disputed | duplicate
    status = Column(String, default="pending")
    created_at = Column(DateTime, server_default=func.now())

    decision = relationship("Decision", back_populates="freight_bill", uselist=False)
    audit_entries = relationship("AuditLog", back_populates="freight_bill")


class Decision(Base):
    __tablename__ = "decisions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    freight_bill_id = Column(String, ForeignKey("freight_bills.id"), nullable=False, unique=True)
    # auto_approve | auto_dispute | duplicate_reject | human_approve | human_dispute | human_modify
    decision_type = Column(String, nullable=False)
    confidence_score = Column(Float)
    evidence = Column(JSON)          # per-dimension breakdown
    reasoning = Column(Text)         # LLM-generated explanation
    decided_by = Column(String, default="agent")
    decided_at = Column(DateTime, server_default=func.now())

    freight_bill = relationship("FreightBill", back_populates="decision")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    freight_bill_id = Column(String, ForeignKey("freight_bills.id"), nullable=False)
    event_type = Column(String, nullable=False)
    payload = Column(JSON)
    ts = Column(DateTime, server_default=func.now())

    freight_bill = relationship("FreightBill", back_populates="audit_entries")


class AgentCheckpoint(Base):
    __tablename__ = "agent_checkpoints"

    thread_id = Column(String, primary_key=True)
    freight_bill_id = Column(String, ForeignKey("freight_bills.id"), nullable=False, unique=True)
    checkpoint_data = Column(JSON)
    status = Column(String, default="active")
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
