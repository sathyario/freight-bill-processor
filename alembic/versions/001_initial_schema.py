"""initial schema

Revision ID: 001
Revises:
Create Date: 2025-04-29
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "carriers",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("carrier_code", sa.String(), nullable=False),
        sa.Column("gstin", sa.String()),
        sa.Column("bank_account", sa.String()),
        sa.Column("status", sa.String(), default="active"),
        sa.Column("onboarded_on", sa.Date()),
    )

    op.create_table(
        "contracts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("carrier_id", sa.String(), sa.ForeignKey("carriers.id"), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("expiry_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(), default="active"),
        sa.Column("notes", sa.Text()),
    )

    op.create_table(
        "rate_cards",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("contract_id", sa.String(), sa.ForeignKey("contracts.id"), nullable=False),
        sa.Column("lane", sa.String(), nullable=False),
        sa.Column("description", sa.String()),
        sa.Column("rate_per_kg", sa.Numeric(10, 2)),
        sa.Column("rate_per_unit", sa.Numeric(10, 2)),
        sa.Column("unit", sa.String()),
        sa.Column("unit_capacity_kg", sa.Integer()),
        sa.Column("alternate_rate_per_kg", sa.Numeric(10, 2)),
        sa.Column("min_charge", sa.Numeric(10, 2), nullable=False),
        sa.Column("fuel_surcharge_pct", sa.Float(), nullable=False),
        sa.Column("revised_on", sa.Date()),
        sa.Column("revised_fuel_surcharge_pct", sa.Float()),
    )

    op.create_table(
        "shipments",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("carrier_id", sa.String(), sa.ForeignKey("carriers.id"), nullable=False),
        sa.Column("contract_id", sa.String(), sa.ForeignKey("contracts.id"), nullable=False),
        sa.Column("lane", sa.String(), nullable=False),
        sa.Column("shipment_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(), default="pending"),
        sa.Column("total_weight_kg", sa.Float(), nullable=False),
        sa.Column("notes", sa.Text()),
    )

    op.create_table(
        "bills_of_lading",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("shipment_id", sa.String(), sa.ForeignKey("shipments.id"), nullable=False),
        sa.Column("delivery_date", sa.Date(), nullable=False),
        sa.Column("actual_weight_kg", sa.Float(), nullable=False),
        sa.Column("notes", sa.Text()),
    )

    op.create_table(
        "freight_bills",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("carrier_id", sa.String(), sa.ForeignKey("carriers.id"), nullable=True),
        sa.Column("carrier_name", sa.String(), nullable=False),
        sa.Column("bill_number", sa.String(), nullable=False),
        sa.Column("bill_date", sa.Date(), nullable=False),
        sa.Column("shipment_reference", sa.String(), nullable=True),
        sa.Column("lane", sa.String(), nullable=False),
        sa.Column("billed_weight_kg", sa.Float(), nullable=False),
        sa.Column("rate_per_kg", sa.Numeric(10, 2)),
        sa.Column("billing_unit", sa.String(), default="kg"),
        sa.Column("base_charge", sa.Numeric(10, 2)),
        sa.Column("fuel_surcharge", sa.Numeric(10, 2)),
        sa.Column("gst_amount", sa.Numeric(10, 2)),
        sa.Column("total_amount", sa.Numeric(10, 2)),
        sa.Column("status", sa.String(), default="pending"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_freight_bills_bill_number", "freight_bills", ["bill_number", "carrier_id"])

    op.create_table(
        "decisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("freight_bill_id", sa.String(), sa.ForeignKey("freight_bills.id"), nullable=False, unique=True),
        sa.Column("decision_type", sa.String(), nullable=False),
        sa.Column("confidence_score", sa.Float()),
        sa.Column("evidence", sa.JSON()),
        sa.Column("reasoning", sa.Text()),
        sa.Column("decided_by", sa.String(), default="agent"),
        sa.Column("decided_at", sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("freight_bill_id", sa.String(), sa.ForeignKey("freight_bills.id"), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON()),
        sa.Column("ts", sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_table(
        "agent_checkpoints",
        sa.Column("thread_id", sa.String(), primary_key=True),
        sa.Column("freight_bill_id", sa.String(), sa.ForeignKey("freight_bills.id"), nullable=False, unique=True),
        sa.Column("checkpoint_data", sa.JSON()),
        sa.Column("status", sa.String(), default="active"),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("agent_checkpoints")
    op.drop_table("audit_log")
    op.drop_table("decisions")
    op.drop_index("ix_freight_bills_bill_number", "freight_bills")
    op.drop_table("freight_bills")
    op.drop_table("bills_of_lading")
    op.drop_table("shipments")
    op.drop_table("rate_cards")
    op.drop_table("contracts")
    op.drop_table("carriers")
