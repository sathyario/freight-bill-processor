"""
Core decision logic tests — no LLM, no live DB required.
Tests the rules and scoring in isolation.
"""
import pytest
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from app.rules.charge_validator import validate_all
from app.agent.nodes.score import score_node, _score_validation


# ── Helpers ────────────────────────────────────────────────────────────────────

def make_rate_card(
    rate_per_kg=12.50,
    min_charge=5000.0,
    fuel_surcharge_pct=8.0,
    revised_on=None,
    revised_fuel_surcharge_pct=None,
    rate_per_unit=None,
):
    rc = MagicMock()
    rc.rate_per_kg = Decimal(str(rate_per_kg))
    rc.alternate_rate_per_kg = None
    rc.rate_per_unit = Decimal(str(rate_per_unit)) if rate_per_unit else None
    rc.min_charge = Decimal(str(min_charge))
    rc.fuel_surcharge_pct = fuel_surcharge_pct
    rc.revised_on = revised_on
    rc.revised_fuel_surcharge_pct = revised_fuel_surcharge_pct
    return rc


# ── Test 1: FB-2025-104 Over-billing detection ─────────────────────────────────

def test_fb104_overbilling_detected():
    """
    SHP-2025-001: 2000kg total.
    FB-2025-103 already billed 800kg (prior).
    FB-2025-104 bills 1500kg → total 2300kg > 2000kg → cumulative_weight rule FAILS.
    """
    bill = {
        "id": "FB-2025-104",
        "carrier_id": "CAR001",
        "bill_number": "SFX/2025/00267",
        "bill_date": "2025-03-15",
        "shipment_reference": "SHP-2025-001",
        "lane": "DEL-BOM",
        "billed_weight_kg": 1500,
        "rate_per_kg": 12.50,
        "billing_unit": "kg",
        "base_charge": 18750.00,
        "fuel_surcharge": 1500.00,
        "gst_amount": 3645.00,
        "total_amount": 23895.00,
    }
    rate_card = make_rate_card(rate_per_kg=12.50, min_charge=5000, fuel_surcharge_pct=8.0)

    results = validate_all(
        bill=bill,
        rate_card=rate_card,
        bol_actual_weight=1200.0,      # BOL-2025-001 actual = 1200kg
        shipment_total_weight=2000.0,  # SHP-2025-001 total = 2000kg
        cumulative_billed=800.0,       # FB-2025-103 already billed 800kg
        bill_date=date(2025, 3, 15),
    )

    cumulative_rule = next(r for r in results if r["rule"] == "cumulative_weight")
    assert cumulative_rule["passed"] is False, "Cumulative weight check should FAIL"
    assert cumulative_rule["actual"] == 2300.0, "Total billed should be 2300kg (800+1500)"
    assert cumulative_rule["expected"] == 2000.0
    assert cumulative_rule["overage_kg"] == 300.0

    # Score should be very low (< 0.40) → auto_dispute
    charge_score = _score_validation(results)
    assert charge_score == 0.0, f"Score should be 0.0 for critical over-billing, got {charge_score}"


# ── Test 2: FB-2025-108 Fuel surcharge revision ────────────────────────────────

def test_fb108_revised_fuel_surcharge_applied():
    """
    CC-2024-BDA-001 fuel surcharge revised from 12% → 18% on 2024-10-01.
    Bill date 2024-11-20 is AFTER revision.
    Bill correctly applies 18%: 21250 × 0.18 = 3825.
    Should PASS fuel surcharge check.
    """
    bill = {
        "id": "FB-2025-108",
        "carrier_id": "CAR004",
        "bill_number": "BDA/24-25/4567",
        "bill_date": "2024-11-20",
        "shipment_reference": "SHP-2025-006",
        "lane": "DEL-BOM-AIR",
        "billed_weight_kg": 250,
        "rate_per_kg": 85.00,
        "billing_unit": "kg",
        "base_charge": 21250.00,
        "fuel_surcharge": 3825.00,   # 18% of 21250
        "gst_amount": 4513.50,
        "total_amount": 29588.50,
    }
    rate_card = make_rate_card(
        rate_per_kg=85.00,
        min_charge=12000.0,
        fuel_surcharge_pct=12.0,
        revised_on=date(2024, 10, 1),
        revised_fuel_surcharge_pct=18.0,
    )

    results = validate_all(
        bill=bill,
        rate_card=rate_card,
        bol_actual_weight=250.0,
        shipment_total_weight=250.0,
        cumulative_billed=0.0,
        bill_date=date(2024, 11, 20),
    )

    fuel_rule = next(r for r in results if r["rule"] == "fuel_surcharge")
    assert fuel_rule["passed"] is True, f"Fuel surcharge should PASS. Got: {fuel_rule}"
    assert "revised_18.0pct" in fuel_rule["note"], f"Should use revised rate. Got note: {fuel_rule['note']}"
    assert abs(fuel_rule["expected"] - 3825.0) < 1.0


# ── Test 3: FB-2025-101 Clean match ───────────────────────────────────────────

def test_fb101_clean_match_passes_all_rules():
    """Clean bill — all rules should pass."""
    bill = {
        "id": "FB-2025-101",
        "carrier_id": "CAR001",
        "bill_number": "SFX/2025/00234",
        "bill_date": "2025-02-15",
        "shipment_reference": "SHP-2025-002",
        "lane": "DEL-BLR",
        "billed_weight_kg": 850,
        "rate_per_kg": 15.00,
        "billing_unit": "kg",
        "base_charge": 12750.00,
        "fuel_surcharge": 1020.00,
        "gst_amount": 2479.00,
        "total_amount": 16249.00,
    }
    rate_card = make_rate_card(rate_per_kg=15.00, min_charge=6000, fuel_surcharge_pct=8.0)

    results = validate_all(
        bill=bill,
        rate_card=rate_card,
        bol_actual_weight=850.0,
        shipment_total_weight=850.0,
        cumulative_billed=0.0,
        bill_date=date(2025, 2, 15),
    )

    failed = [r for r in results if r.get("passed") is False]
    assert not failed, f"All rules should pass for clean bill. Failed: {failed}"


# ── Test 4: FB-2025-105 Rate drift ────────────────────────────────────────────

def test_fb105_rate_drift_detected():
    """Billed ₹8.70/kg vs contracted ₹8.00/kg — 8.75% deviation should FAIL rate check."""
    bill = {
        "id": "FB-2025-105",
        "carrier_id": "CAR002",
        "bill_number": "DEL/25-26/1089",
        "bill_date": "2025-01-25",
        "shipment_reference": "SHP-2025-004",
        "lane": "BLR-CHN",
        "billed_weight_kg": 1200,
        "rate_per_kg": 8.70,
        "billing_unit": "kg",
        "base_charge": 10440.00,
        "fuel_surcharge": 730.80,
        "gst_amount": 2010.74,
        "total_amount": 13181.54,
    }
    rate_card = make_rate_card(rate_per_kg=8.00, min_charge=3500, fuel_surcharge_pct=7.0)

    results = validate_all(
        bill=bill,
        rate_card=rate_card,
        bol_actual_weight=1200.0,
        shipment_total_weight=1200.0,
        cumulative_billed=0.0,
        bill_date=date(2025, 1, 25),
    )

    rate_rule = next(r for r in results if r["rule"] == "rate_vs_contract")
    assert rate_rule["passed"] is False, "Rate drift should fail"
    assert rate_rule["deviation_pct"] > 8.0


# ── Test 5: FB-2025-107 FTL vs per-kg unit mismatch ──────────────────────────

def test_fb107_ftl_per_kg_more_expensive():
    """Per-kg billing (7800×6.50=50700) costs more than FTL (48000). Should flag."""
    bill = {
        "id": "FB-2025-107",
        "carrier_id": "CAR003",
        "bill_number": "TCI/2025/00052",
        "bill_date": "2025-03-01",
        "shipment_reference": "SHP-2025-005",
        "lane": "BOM-AHM",
        "billed_weight_kg": 7800,
        "rate_per_kg": 6.50,
        "billing_unit": "kg",
        "base_charge": 50700.00,
        "fuel_surcharge": 3042.00,
        "gst_amount": 9673.56,
        "total_amount": 63415.56,
    }
    rate_card = make_rate_card(
        rate_per_kg=6.50,
        min_charge=48000,
        fuel_surcharge_pct=6.0,
        rate_per_unit=48000.0,
    )
    rate_card.alternate_rate_per_kg = Decimal("6.50")

    results = validate_all(
        bill=bill,
        rate_card=rate_card,
        bol_actual_weight=7800.0,
        shipment_total_weight=8000.0,
        cumulative_billed=0.0,
        bill_date=date(2025, 3, 1),
    )

    unit_rule = next((r for r in results if r["rule"] == "unit_reconciliation"), None)
    assert unit_rule is not None, "Unit reconciliation rule should run for FTL contracts"
    assert unit_rule["passed"] is False, "Per-kg billing costs more than FTL — should flag"
    assert unit_rule["cost_difference"] > 0
