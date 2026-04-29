"""
LangGraph stateful agent — freight bill processing workflow.

Flow:
  preflight → normalize → match → validate → score → decide → explain → END
                                                         ↓ (interrupt)
                                                    [human review]
                                                         ↓ (resume)
                                                       explain → END
"""
import structlog
import psycopg
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command

from app.agent.state import AgentState
from app.agent.nodes.preflight import preflight_node
from app.agent.nodes.normalize import normalize_node
from app.agent.nodes.match import match_node
from app.agent.nodes.validate import validate_node
from app.agent.nodes.score import score_node
from app.agent.nodes.decide import decide_node
from app.agent.nodes.explain import explain_node
from app.db.session import SessionLocal
from app.db.models import FreightBill, AuditLog
from app.config import settings

log = structlog.get_logger()


def _make_checkpointer() -> PostgresSaver:
    """Persistent PostgresSaver — checkpoints survive container restarts.
    psycopg3 connection; autocommit required by LangGraph PostgresSaver."""
    conn_str = settings.database_url.replace("postgresql+psycopg2://", "postgresql://")
    conn = psycopg.connect(conn_str, autocommit=True)
    saver = PostgresSaver(conn)
    saver.setup()  # Creates langgraph_checkpoints table if absent
    return saver


_checkpointer = _make_checkpointer()


def _route_after_preflight(state: AgentState) -> str:
    """Skip rest of pipeline if duplicate detected."""
    if state.get("decision") == "duplicate_reject":
        return "explain"
    return "normalize"


def _build_graph():
    workflow = StateGraph(AgentState)

    workflow.add_node("preflight", preflight_node)
    workflow.add_node("normalize", normalize_node)
    workflow.add_node("match", match_node)
    workflow.add_node("validate", validate_node)
    workflow.add_node("score", score_node)
    workflow.add_node("decide", decide_node)
    workflow.add_node("explain", explain_node)

    workflow.set_entry_point("preflight")

    workflow.add_conditional_edges(
        "preflight",
        _route_after_preflight,
        {"normalize": "normalize", "explain": "explain"},
    )
    workflow.add_edge("normalize", "match")
    workflow.add_edge("match", "validate")
    workflow.add_edge("validate", "score")
    workflow.add_edge("score", "decide")
    workflow.add_edge("decide", "explain")
    workflow.add_edge("explain", END)

    return workflow.compile(checkpointer=_checkpointer)


# Singleton compiled graph
_graph = _build_graph()


async def run_agent(bill_data: dict):
    """Entry point — called from background task after POST /freight-bills."""
    bill_id = bill_data.get("id")
    log.info("agent_started", bill_id=bill_id)

    _update_bill_status(bill_id, "processing")

    config = {"configurable": {"thread_id": bill_id}}

    initial_state: AgentState = {
        "freight_bill": bill_data,
        "carrier_normalized": None,
        "duplicate_of": None,
        "candidate_contracts": [],
        "matched_contract": None,
        "matched_shipment": None,
        "matched_bol": None,
        "validation_results": [],
        "confidence_score": 0.0,
        "confidence_breakdown": {},
        "decision": None,
        "evidence_summary": None,
        "human_decision": None,
    }

    try:
        result = _graph.invoke(initial_state, config=config)
        decision = result.get("decision", "unknown")
        log.info("agent_completed", bill_id=bill_id, decision=decision)

        # If interrupted (pending_review), update status
        if _graph.get_state(config).next:
            _update_bill_status(bill_id, "pending_review")
            log.info("agent_interrupted_awaiting_review", bill_id=bill_id)

    except Exception as e:
        log.error("agent_failed", bill_id=bill_id, error=str(e))
        _update_bill_status(bill_id, "disputed")
        _save_error_decision(bill_id, str(e))
        raise


async def resume_agent(bill_id: str, human_decision: dict):
    """Resume agent after human review — called from POST /review/{id}."""
    log.info("agent_resuming", bill_id=bill_id, human_decision=human_decision)

    config = {"configurable": {"thread_id": bill_id}}

    try:
        result = _graph.invoke(
            Command(resume=human_decision),
            config=config,
        )
        log.info("agent_resumed_completed", bill_id=bill_id, decision=result.get("decision"))
    except Exception as e:
        log.error("agent_resume_failed", bill_id=bill_id, error=str(e))
        raise


def _update_bill_status(bill_id: str, status: str):
    db = SessionLocal()
    try:
        fb = db.query(FreightBill).filter_by(id=bill_id).first()
        if fb:
            fb.status = status
            db.add(AuditLog(
                freight_bill_id=bill_id,
                event_type=f"status_changed_{status}",
                payload={"status": status},
            ))
            db.commit()
    except Exception as e:
        db.rollback()
        log.error("status_update_failed", bill_id=bill_id, error=str(e))
    finally:
        db.close()


def _save_error_decision(bill_id: str, error_msg: str):
    db = SessionLocal()
    try:
        from app.db.models import Decision
        existing = db.query(Decision).filter_by(freight_bill_id=bill_id).first()
        if not existing:
            db.add(Decision(
                freight_bill_id=bill_id,
                decision_type="auto_dispute",
                confidence_score=0.0,
                evidence={"error": error_msg},
                reasoning=f"Agent error: {error_msg}",
                decided_by="agent",
            ))
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
