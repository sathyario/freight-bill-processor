import structlog
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db.session import init_db
from app.db.neo4j_store import neo4j_store
from app.api.routes.freight_bills import router as freight_bills_router
from app.api.routes.review import router as review_router
from app.api.routes.metrics import router as metrics_router

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("starting_up")

    # 1. Run Alembic migrations + init Postgres tables
    await init_db()
    log.info("postgres_ready")

    # 2. Connect Neo4j and seed graph
    await neo4j_store.connect()
    log.info("neo4j_ready")

    # 3. Load seed data into Postgres + Neo4j (idempotent)
    from app.seeder import run_seed
    await run_seed()
    log.info("seed_data_loaded")

    yield

    # Shutdown
    await neo4j_store.close()
    log.info("shutdown_complete")


app = FastAPI(
    title="Freight Bill Processor",
    description="Automated freight bill validation and matching system",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(freight_bills_router, prefix="/freight-bills", tags=["Freight Bills"])
app.include_router(review_router, prefix="", tags=["Review"])
app.include_router(metrics_router, prefix="/metrics", tags=["Metrics"])


@app.get("/health", tags=["Health"])
async def health():
    return {"status": "ok"}
