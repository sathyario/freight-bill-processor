"""
Deterministic charge validation rules — no LLM involved.
Each rule returns a dict: {rule, passed, expected, actual, deviation_pct, note}
"""
from datetime import date
from decimal import Decimal, ROUND_HALF_UP


GST_RATE = Decimal("0.18")
WEIGHT_PASS_PCT = 2.0    # ±2% → pass
WEIGHT_WARN_PCT = 5.0    # ±5% → warn
RATE_EPSILON = Decimal("0.01")


def _pct_diff(actual: Decimal, expected: Decimal) -> float:
    if expected == 0:
        return 0.0
    return float(abs(actual - expected) / expected * 100)


def validate_all(
    bill: dict,
    rate_card,           # RateCard ORM object
    bol_actual_weight: float | None,
    shipment_total_weight: float | None,
    cumulative_billed: float,
    bill_date: date,
) -> list[dict]:
    results = []

    billed_weight = Decimal(str(bill["billed_weight_kg"]))
    billed_rate = Decimal(str(bill.get("rate_per_kg") or 0))
    billed_base = Decimal(str(bill.get("base_charge") or 0))
    billed_fuel = Decimal(str(bill.get("fuel_surcharge") or 0))
    billed_gst = Decimal(str(bill.get("gst_amount") or 0))
    billing_unit = bill.get("billing_unit", "kg")

    # ── Rule 1: Weight vs BOL ─────────────────────────────────────────────────
    if bol_actual_weight is not None:
        bol_weight = Decimal(str(bol_actual_weight))
        weight_diff_pct = _pct_diff(billed_weight, bol_weight)

        # Multi-truck partial delivery: billed < BOL but fits within remaining shipment capacity.
        # The seed data has one BOL per shipment even for split deliveries; treat this as OK.
        is_partial_delivery = (
            billed_weight < bol_weight
            and shipment_total_weight is not None
            and (cumulative_billed + float(billed_weight)) <= (shipment_total_weight + 0.5)
        )

        passed = weight_diff_pct <= WEIGHT_WARN_PCT or is_partial_delivery
        note = (
            "partial_delivery_ok"
            if is_partial_delivery
            else ("pass" if weight_diff_pct <= WEIGHT_PASS_PCT
                  else ("warn" if weight_diff_pct <= WEIGHT_WARN_PCT else "fail"))
        )
        results.append({
            "rule": "weight_vs_bol",
            "passed": passed,
            "expected": float(bol_weight),
            "actual": float(billed_weight),
            "deviation_pct": round(weight_diff_pct, 2),
            "note": note,
        })
    else:
        results.append({"rule": "weight_vs_bol", "passed": None, "note": "no_bol_found"})

    # ── Rule 2: Rate vs Contract ──────────────────────────────────────────────
    if rate_card:
        contract_rate = rate_card.rate_per_kg or rate_card.alternate_rate_per_kg
        if contract_rate:
            contract_rate = Decimal(str(contract_rate))
            rate_diff = abs(billed_rate - contract_rate)
            rate_diff_pct = _pct_diff(billed_rate, contract_rate)
            results.append({
                "rule": "rate_vs_contract",
                "passed": rate_diff <= RATE_EPSILON,
                "expected": float(contract_rate),
                "actual": float(billed_rate),
                "deviation_pct": round(rate_diff_pct, 2),
                "note": f"diff={rate_diff}",
            })
        else:
            results.append({"rule": "rate_vs_contract", "passed": None, "note": "no_per_kg_rate_in_contract"})
    else:
        results.append({"rule": "rate_vs_contract", "passed": False, "note": "no_rate_card"})

    # ── Rule 3: Fuel surcharge (with revision check) ──────────────────────────
    if rate_card:
        # Determine which fuel surcharge % applies on bill_date
        if (rate_card.revised_on and rate_card.revised_fuel_surcharge_pct
                and bill_date >= rate_card.revised_on):
            applicable_fuel_pct = Decimal(str(rate_card.revised_fuel_surcharge_pct))
            revision_note = f"using_revised_{rate_card.revised_fuel_surcharge_pct}pct_from_{rate_card.revised_on}"
        else:
            applicable_fuel_pct = Decimal(str(rate_card.fuel_surcharge_pct))
            revision_note = f"using_original_{rate_card.fuel_surcharge_pct}pct"

        expected_fuel = (billed_base * applicable_fuel_pct / 100).quantize(Decimal("0.01"), ROUND_HALF_UP)
        fuel_diff_pct = _pct_diff(billed_fuel, expected_fuel)
        results.append({
            "rule": "fuel_surcharge",
            "passed": fuel_diff_pct <= 2.0,
            "expected": float(expected_fuel),
            "actual": float(billed_fuel),
            "deviation_pct": round(fuel_diff_pct, 2),
            "note": revision_note,
        })

    # ── Rule 4: Base charge calculation ──────────────────────────────────────
    if rate_card and billed_rate > 0:
        min_charge = Decimal(str(rate_card.min_charge))
        expected_base = max(billed_weight * billed_rate, min_charge).quantize(Decimal("0.01"), ROUND_HALF_UP)
        base_diff_pct = _pct_diff(billed_base, expected_base)
        results.append({
            "rule": "base_charge_calculation",
            "passed": base_diff_pct <= 2.0,
            "expected": float(expected_base),
            "actual": float(billed_base),
            "deviation_pct": round(base_diff_pct, 2),
            "note": f"min_charge={min_charge}",
        })

    # ── Rule 5: Cumulative weight check (over-billing detection) ─────────────
    if shipment_total_weight is not None:
        total_after = cumulative_billed + float(billed_weight)
        overage = total_after - shipment_total_weight
        results.append({
            "rule": "cumulative_weight",
            "passed": total_after <= shipment_total_weight,
            "expected": shipment_total_weight,
            "actual": total_after,
            "cumulative_prior": cumulative_billed,
            "overage_kg": round(overage, 2) if overage > 0 else 0,
            "note": f"prior_billed={cumulative_billed}kg + this={float(billed_weight)}kg = {total_after}kg",
        })

    # ── Rule 6: Unit reconciliation (FTL vs per-kg) ───────────────────────────
    if rate_card and rate_card.rate_per_unit and billing_unit == "kg":
        ftl_rate = Decimal(str(rate_card.rate_per_unit))
        per_kg_total = billed_base
        savings_if_ftl = per_kg_total - ftl_rate
        results.append({
            "rule": "unit_reconciliation",
            "passed": savings_if_ftl <= 0,  # fail if per-kg costs MORE than FTL
            "ftl_price": float(ftl_rate),
            "per_kg_total": float(per_kg_total),
            "cost_difference": float(savings_if_ftl),
            "note": (
                f"per_kg_billing_costs_{float(savings_if_ftl):.2f}_more_than_ftl"
                if savings_if_ftl > 0
                else "per_kg_billing_cheaper_than_ftl"
            ),
        })

    return results
