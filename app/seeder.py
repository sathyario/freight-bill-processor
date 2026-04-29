"""
Idempotent seed loader — reads seed_data.json and populates Postgres + Neo4j.
Safe to run multiple times; uses INSERT OR IGNORE / MERGE semantics.
"""
import json
from datetime import date
from pathlib import Path

import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.db.session import SessionLocal
from app.db.models import Carrier, Contract, RateCard, Shipment, BillOfLading
from app.db.neo4j_store import neo4j_store

log = structlog.get_logger()


def _parse_date(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


async def run_seed():
    seed_path = Path(settings.seed_data_path)
    if not seed_path.exists():
        # Try relative path (for local dev outside docker)
        seed_path = Path(__file__).parent.parent / "seed_data.json"
    if not seed_path.exists():
        log.warning("seed_file_not_found", path=str(seed_path))
        return

    with open(seed_path, encoding="utf-8") as f:
        data = json.load(f)

    _seed_postgres(data)
    _seed_neo4j(data)
    log.info("seed_complete")


def _seed_postgres(data: dict):
    db = SessionLocal()
    try:
        # ── Carriers ────────────────────────────────────────────────────────
        for c in data["carriers"]:
            stmt = pg_insert(Carrier).values(
                id=c["id"],
                name=c["name"],
                carrier_code=c["carrier_code"],
                gstin=c.get("gstin"),
                bank_account=c.get("bank_account"),
                status=c.get("status", "active"),
                onboarded_on=_parse_date(c.get("onboarded_on")),
            ).on_conflict_do_nothing(index_elements=["id"])
            db.execute(stmt)

        # ── Contracts + rate cards ───────────────────────────────────────────
        for co in data["carrier_contracts"]:
            stmt = pg_insert(Contract).values(
                id=co["id"],
                carrier_id=co["carrier_id"],
                effective_date=_parse_date(co["effective_date"]),
                expiry_date=_parse_date(co["expiry_date"]),
                status=co.get("status", "active"),
                notes=co.get("notes"),
            ).on_conflict_do_nothing(index_elements=["id"])
            db.execute(stmt)

            for rc in co.get("rate_card", []):
                stmt = pg_insert(RateCard).values(
                    contract_id=co["id"],
                    lane=rc["lane"],
                    description=rc.get("description"),
                    rate_per_kg=rc.get("rate_per_kg"),
                    rate_per_unit=rc.get("rate_per_unit"),
                    unit=rc.get("unit"),
                    unit_capacity_kg=rc.get("unit_capacity_kg"),
                    alternate_rate_per_kg=rc.get("alternate_rate_per_kg"),
                    min_charge=rc["min_charge"],
                    fuel_surcharge_pct=rc["fuel_surcharge_percent"],
                    revised_on=_parse_date(rc.get("revised_on")),
                    revised_fuel_surcharge_pct=rc.get("revised_fuel_surcharge_percent"),
                ).on_conflict_do_nothing()
                db.execute(stmt)

        # ── Shipments ────────────────────────────────────────────────────────
        for s in data["shipments"]:
            stmt = pg_insert(Shipment).values(
                id=s["id"],
                carrier_id=s["carrier_id"],
                contract_id=s["contract_id"],
                lane=s["lane"],
                shipment_date=_parse_date(s["shipment_date"]),
                status=s.get("status", "pending"),
                total_weight_kg=s["total_weight_kg"],
                notes=s.get("notes"),
            ).on_conflict_do_nothing(index_elements=["id"])
            db.execute(stmt)

        # ── Bills of Lading ──────────────────────────────────────────────────
        for b in data["bills_of_lading"]:
            stmt = pg_insert(BillOfLading).values(
                id=b["id"],
                shipment_id=b["shipment_id"],
                delivery_date=_parse_date(b["delivery_date"]),
                actual_weight_kg=b["actual_weight_kg"],
                notes=b.get("notes") or b.get("_note"),
            ).on_conflict_do_nothing(index_elements=["id"])
            db.execute(stmt)

        db.commit()
        log.info("postgres_seeded",
                 carriers=len(data["carriers"]),
                 contracts=len(data["carrier_contracts"]),
                 shipments=len(data["shipments"]),
                 bols=len(data["bills_of_lading"]))
    except Exception as e:
        db.rollback()
        log.error("postgres_seed_failed", error=str(e))
        raise
    finally:
        db.close()


def _seed_neo4j(data: dict):
    try:
        # Carriers
        for c in data["carriers"]:
            neo4j_store.upsert_carrier(c)

        # Contracts + lanes
        for co in data["carrier_contracts"]:
            lanes = {rc["lane"] for rc in co.get("rate_card", [])}
            for lane in lanes:
                neo4j_store.upsert_contract(co, lane)

        # Shipments
        for s in data["shipments"]:
            neo4j_store.upsert_shipment(s)

        # BOLs
        for b in data["bills_of_lading"]:
            neo4j_store.upsert_bol(b)

        log.info("neo4j_seeded",
                 carriers=len(data["carriers"]),
                 contracts=len(data["carrier_contracts"]),
                 shipments=len(data["shipments"]),
                 bols=len(data["bills_of_lading"]))
    except Exception as e:
        log.error("neo4j_seed_failed", error=str(e))
        raise
