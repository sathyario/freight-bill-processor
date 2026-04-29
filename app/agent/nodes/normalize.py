"""
Normalize node — uses LLM to fuzzy-match carrier_name against known carriers.
Falls back gracefully if Gemini is unavailable or quota is exceeded.
"""
from app.agent.state import AgentState
from app.db.session import SessionLocal
from app.db.models import Carrier
from app.config import settings
import structlog

log = structlog.get_logger()


def normalize_node(state: AgentState) -> dict:
    bill = state["freight_bill"]
    carrier_id = bill.get("carrier_id")
    carrier_name = bill.get("carrier_name", "")

    db = SessionLocal()
    try:
        # Fast path: carrier_id provided and exists in DB
        if carrier_id:
            carrier = db.query(Carrier).filter(Carrier.id == carrier_id).first()
            if carrier:
                return {
                    "carrier_normalized": carrier.id,
                    "confidence_breakdown": {
                        **state.get("confidence_breakdown", {}),
                        "carrier_match": 1.0,
                        "carrier_match_note": f"exact_id_match:{carrier.id}",
                    },
                }

        # Slow path: fuzzy match by name via LLM
        all_carriers = db.query(Carrier).all()
        carrier_list = [f"{c.id}: {c.name} ({c.carrier_code})" for c in all_carriers]

        matched_id, score, note = _llm_match(carrier_name, carrier_list)

        return {
            "carrier_normalized": matched_id,
            "confidence_breakdown": {
                **state.get("confidence_breakdown", {}),
                "carrier_match": score,
                "carrier_match_note": note,
            },
        }
    finally:
        db.close()


def _llm_match(carrier_name: str, carrier_list: list[str]) -> tuple[str | None, float, str]:
    """Ask Gemini to match carrier_name to our known carriers."""
    if not settings.gemini_api_key or settings.gemini_api_key == "your_gemini_api_key_here":
        return _fallback_match(carrier_name, carrier_list)

    try:
        import google.generativeai as genai  # type: ignore[import]
        genai.configure(api_key=settings.gemini_api_key)
        model = genai.GenerativeModel("gemini-2.5-flash")

        prompt = f"""You are a logistics data normalizer. Match the carrier name to one of our registered carriers.

Carrier name from freight bill: "{carrier_name}"

Registered carriers:
{chr(10).join(carrier_list)}

Reply with ONLY the carrier ID (e.g. CAR001) if you find a match, or "UNKNOWN" if no match.
Do not explain. Just output the ID or UNKNOWN."""

        response = model.generate_content(prompt)
        result = response.text.strip().upper()

        if result == "UNKNOWN" or not result.startswith("CAR"):
            log.info("carrier_not_matched_by_llm", carrier_name=carrier_name)
            return None, 0.0, f"llm_no_match:{carrier_name}"

        log.info("carrier_matched_by_llm", carrier_name=carrier_name, matched_id=result)
        return result, 0.7, f"llm_fuzzy_match:{result}"

    except Exception as e:
        log.warning("llm_carrier_match_failed", error=str(e))
        return _fallback_match(carrier_name, carrier_list)


def _fallback_match(carrier_name: str, carrier_list: list[str]) -> tuple[str | None, float, str]:
    """Simple substring match when LLM is unavailable."""
    name_lower = carrier_name.lower()
    for entry in carrier_list:
        carrier_id = entry.split(":")[0].strip()
        if any(word in entry.lower() for word in name_lower.split() if len(word) > 3):
            return carrier_id, 0.6, f"fallback_substring_match:{carrier_id}"
    return None, 0.0, f"no_match_found:{carrier_name}"
