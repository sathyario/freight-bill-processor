# Freight Bill Processing System

An AI-powered freight bill auditing system using **FastAPI**, **LangGraph**, **PostgreSQL**, and **Neo4j**. Processes freight bills against contracts and shipments, auto-approves clean bills, flags anomalies, and pauses for human review on ambiguous cases.

**Live API:** https://freight-bill-processor-733950093150.us-central1.run.app  
**Swagger UI:** https://freight-bill-processor-733950093150.us-central1.run.app/docs  
**GitHub:** https://github.com/sathyario/freight-bill-processor

---

## How to Run

### Prerequisites
- Docker Desktop running
- Ports 5432, 7474, 7687, 8000 free

### Start

```bash
git clone https://github.com/sathyario/freight-bill-processor.git
cd freight-bill-processor
cp .env.example .env   # edit GEMINI_API_KEY if you have one (optional)
docker-compose up
```

On startup:
1. Alembic migrations run → all tables created
2. Seed data loaded from `seed_data.json` → 5 carriers, 8 contracts, 7 shipments, 7 BOLs
3. Neo4j graph seeded → same data, relationship-aware

**Swagger UI:** http://localhost:8000/docs  
**Neo4j Browser:** http://localhost:7474 (user: `neo4j`, pass: `localpass`)

### Verify with seed scenarios

```bash
# 1. Clean bill — auto_approve
curl -X POST http://localhost:8000/freight-bills \
  -H "Content-Type: application/json" \
  -d @tests/fixtures/FB-2025-101.json

# 2. Check result
curl http://localhost:8000/freight-bills/FB-2025-101

# 3. Over-billing — auto_dispute (must POST FB-2025-103 first)
curl -X POST http://localhost:8000/freight-bills -H "Content-Type: application/json" \
  -d '{"id":"FB-2025-103","carrier_id":"CAR001","carrier_name":"Safexpress Logistics","bill_number":"SFX/2025/00245","bill_date":"2025-02-25","shipment_reference":"SHP-2025-001","lane":"DEL-BOM","billed_weight_kg":800,"rate_per_kg":12.50,"billing_unit":"kg","base_charge":10000.00,"fuel_surcharge":800.00,"gst_amount":1944.00,"total_amount":12744.00}'
# then...
curl -X POST http://localhost:8000/freight-bills -H "Content-Type: application/json" \
  -d '{"id":"FB-2025-104","carrier_id":"CAR001","carrier_name":"Safexpress Logistics","bill_number":"SFX/2025/00267","bill_date":"2025-03-15","shipment_reference":"SHP-2025-001","lane":"DEL-BOM","billed_weight_kg":1500,"rate_per_kg":12.50,"billing_unit":"kg","base_charge":18750.00,"fuel_surcharge":1500.00,"gst_amount":3645.00,"total_amount":23895.00}'
curl http://localhost:8000/freight-bills/FB-2025-104  # → auto_dispute

# 4. Ambiguous contracts — pending_review → human resume
curl -X POST http://localhost:8000/freight-bills -H "Content-Type: application/json" \
  -d '{"id":"FB-2025-102","carrier_id":"CAR001","carrier_name":"Safexpress Logistics","bill_number":"SFX/2025/00241","bill_date":"2025-02-28","lane":"DEL-BOM","billed_weight_kg":1100,"rate_per_kg":13.20,"billing_unit":"kg","base_charge":14520.00,"fuel_surcharge":1161.60,"gst_amount":2826.29,"total_amount":18507.89}'
curl http://localhost:8000/review-queue
curl -X POST http://localhost:8000/review/FB-2025-102 \
  -H "Content-Type: application/json" \
  -d '{"decision":"approve","notes":"Rate matches CC-2025-SFX-003"}'
curl http://localhost:8000/freight-bills/FB-2025-102  # → human_approve
```

### Run unit tests

```bash
docker exec freight_app pytest tests/ -v
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/freight-bills` | Ingest a freight bill, trigger agent (async) |
| GET | `/freight-bills/{id}` | Status, decision, confidence breakdown, graph evidence |
| GET | `/review-queue` | Bills waiting for human review |
| POST | `/review/{id}` | Submit human decision, resumes LangGraph agent |
| GET | `/metrics` | Decision counts, avg confidence, throughput |
| GET | `/health` | Health check |

---

## Schema Design

### Why PostgreSQL + Neo4j (not one or the other)

Each database does what it's built for:

- **PostgreSQL** handles transactional state: agent checkpoints (ACID-durable across restarts), decisions, audit trail, reference data. Every freight bill write is an ACID transaction — partial writes must roll back cleanly.
- **Neo4j** handles relationship traversal: finding the right contract for a (carrier, lane, date) triple, cumulative weight across all bills referencing a shipment. A 3-hop `Carrier→Contract→Lane→Shipment` traversal is one Cypher MATCH; in SQL it's a 4-table join with date-range predicates that breaks down at scale.

### PostgreSQL tables

```
carriers           (id PK, name, carrier_code, gstin, bank_account, status, onboarded_on)
contracts          (id PK, carrier_id FK, effective_date, expiry_date, status)
rate_cards         (id PK, contract_id FK, lane, rate_per_kg, rate_per_unit, unit,
                    unit_capacity_kg, min_charge, fuel_surcharge_pct,
                    revised_on NULLABLE, revised_fuel_surcharge_pct NULLABLE)
shipments          (id PK, carrier_id FK, contract_id FK, lane, shipment_date, total_weight_kg)
bills_of_lading    (id PK, shipment_id FK, delivery_date, actual_weight_kg)

freight_bills      (id PK, carrier_id FK NULLABLE, carrier_name, bill_number, bill_date,
                    shipment_reference, lane, billed_weight_kg, rate_per_kg,
                    base_charge, fuel_surcharge, gst_amount, total_amount, status)
                   -- status: pending | processing | pending_review | approved |
                   --         disputed | duplicate

decisions          (id PK, freight_bill_id FK, decision_type, confidence_score,
                    evidence JSONB, reasoning TEXT, decided_by, decided_at)

audit_log          (id PK, freight_bill_id FK, event_type, payload JSONB, ts TIMESTAMPTZ)
```

**Key design choices:**
- `rate_cards.revised_on` + `revised_fuel_surcharge_pct`: mid-contract fuel surcharge revisions (FB-2025-108 scenario) are stored on the rate card, not as a new contract. The validator checks `bill_date >= revised_on` to select the correct percentage.
- `freight_bills.status` state machine: `pending → processing → approved|disputed|pending_review|duplicate`. `pending_review` means the LangGraph graph is paused at an `interrupt()` — the checkpoint is alive in Postgres.
- `decisions.evidence JSONB`: stores the full confidence breakdown + per-rule validation results, so the reviewer has complete evidence without re-running the agent.

---

## Graph Model (Neo4j)

### Nodes
`Carrier`, `Contract`, `Lane`, `Shipment`, `BOL`, `FreightBill`

### Relationships
```
Carrier   ──[HAS_CONTRACT]──▶  Contract
Contract  ──[COVERS_LANE]───▶  Lane
Shipment  ──[ON_LANE]───────▶  Lane
Shipment  ──[UNDER_CONTRACT]▶  Contract
Shipment  ──[HAS_BOL]───────▶  BOL
FreightBill ──[REF_SHIPMENT]▶  Shipment    (added when bill has shipment_reference)
FreightBill ──[MATCHED_CONTRACT]▶ Contract (added after agent completes)
```

### The graph is mutable

`FreightBill` nodes are added to Neo4j by the `explain` node *after each bill is processed*. This enables the cumulative over-billing check:

```cypher
-- Active contracts for (carrier, lane, bill_date)
MATCH (ca:Carrier {id: $carrier_id})-[:HAS_CONTRACT]->(co:Contract)-[:COVERS_LANE]->(l:Lane {code: $lane})
WHERE co.effective_date <= date($bill_date) <= co.expiry_date AND co.status = 'active'
RETURN co ORDER BY co.effective_date DESC

-- Cumulative billed weight for a shipment (over-billing check)
MATCH (fb:FreightBill)-[:REF_SHIPMENT]->(s:Shipment {id: $shipment_id})
RETURN sum(fb.billed_weight_kg) AS total_billed

-- Full evidence chain for a freight bill
MATCH path = (fb:FreightBill {id: $id})-[:MATCHED_CONTRACT]->(co:Contract)<-[:HAS_CONTRACT]-(ca:Carrier)
OPTIONAL MATCH (fb)-[:REF_SHIPMENT]->(s:Shipment)-[:HAS_BOL]->(bol:BOL)
RETURN path, s, bol
```

**Why Neo4j over Postgres JOINs:** The 3-contract overlap scenario (FB-2025-102: CAR001 has three active contracts on DEL-BOM simultaneously) is a graph query — find all `Carrier→Contract→Lane` paths where the contract date range overlaps the bill date. In SQL this requires a self-join with date range predicates. In Cypher it's a single parameterised MATCH. The graph is also the natural structure for the evidence chain returned in `GET /freight-bills/{id}`.

---

## Confidence Scoring

Weighted sum across 5 dimensions, each 0–1:

| Dimension | Weight | 1.0 (full score) | Partial | 0.0 |
|-----------|--------|-----------------|---------|-----|
| `carrier_match` | 0.15 | Exact DB match | LLM fuzzy (0.7) | Not found |
| `contract_match` | 0.25 | Unique rate match | Ambiguous (0.65) | None active / expired |
| `shipment_bol_match` | 0.20 | Ref present + BOL found | No BOL (0.5) | No ref (0.2) |
| `charge_validation` | 0.25 | All rules pass | Partial passes | Critical failure |
| `duplicate_check` | 0.15 | No duplicate found | — | Duplicate exists |

### Charge validation rules

All deterministic — no LLM:

1. `weight_vs_bol` — billed weight vs BOL actual weight (±5% tolerance; partial deliveries pass if within shipment remaining)
2. `rate_vs_contract` — billed rate vs contract rate_per_kg (±₹0.01)
3. `fuel_surcharge` — billed fuel vs `base_charge × fuel_pct`; uses `revised_fuel_surcharge_pct` when `bill_date >= rate_card.revised_on`
4. `base_charge_calculation` — `max(weight × rate, min_charge)`
5. `cumulative_weight` — sum of all prior billed weights for this shipment + this bill ≤ shipment total
6. `unit_reconciliation` — if FTL contract, flag when per-kg billing costs more than FTL rate

### Hard decision overrides in decide node

Some signals are ground-truth regardless of overall score:

| Override | Trigger | Decision |
|----------|---------|----------|
| Cumulative over-billing | `cumulative_weight` rule fails | `auto_dispute` |
| Expired contract rate | Billed rate matches expired contract exactly | `auto_dispute` |
| Rate drift > 5% | `rate_vs_contract` deviation > 5% | Cap score → `pending_review` |
| FTL/unit mismatch | `unit_reconciliation` rule fails | Cap score → `pending_review` |

### Thresholds

- **≥ 0.80** → `auto_approve`
- **0.40 – 0.79** → `interrupt()` → human review
- **< 0.40** → `auto_dispute`

---

## Human-in-the-Loop (HITL)

This uses LangGraph's real `interrupt()` pattern — not a polling workaround.

### How it works

```
POST /freight-bills
        │
        ▼
   decide_node (score = 0.63)
        │
        ▼  interrupt(payload)
   [Graph PAUSED — state checkpointed to Postgres via PostgresSaver]
        │
   freight_bills.status = "pending_review"
        │
   GET /review-queue  ← ops team sees bill + evidence + score
        │
   POST /review/{id}  {"decision": "approve", "notes": "..."}
        │
        ▼  Command(resume=human_decision)
   [Graph RESUMES from checkpoint — decide_node returns human_decision]
        │
        ▼
   explain_node → persists final decision → END
```

### Why this matters

The checkpoint is durable in Postgres (`PostgresSaver`, not `MemorySaver`). The API server can restart between the `interrupt()` and the `resume()` — the graph state is never held in memory across the pause. This is production-safe: Cloud Run can scale to zero and back without losing in-flight reviews.

The `interrupt()` payload contains the full evidence: confidence breakdown, per-rule validation results, matched contract/shipment/BOL. The reviewer has everything needed to make a decision without digging into raw data.

---

## 10 Scenario Decision Map

| Bill | Scenario | Decision | Key logic |
|------|----------|----------|-----------|
| FB-2025-101 | Clean match | `auto_approve` (0.99) | All rules pass, exact contract + BOL match |
| FB-2025-102 | 3 overlapping active contracts | `pending_review` (0.63) | Rate matches CC-2025-SFX-003 uniquely, but no shipment ref to confirm |
| FB-2025-103 | 2nd truck partial delivery 800kg | `auto_approve` (0.99) | Partial delivery check: 0+800 ≤ 2000kg shipment total |
| FB-2025-104 | Over-billing 1500kg (prior 800kg, BOL=1200kg) | `auto_dispute` | Hard override: 800+1500=2300 > 2000kg cumulative |
| FB-2025-105 | Rate drift +8.75% (₹8.70 vs ₹8.00) | `pending_review` (0.79*) | Hard cap: rate drift >5% forces review |
| FB-2025-106 | Billing at expired contract rate | `auto_dispute` (0.44) | Hard override: billed ₹7.50 matches expired CC-2023-TCI-001 exactly |
| FB-2025-107 | Per-kg billing costs more than FTL | `pending_review` (0.79*) | Hard cap: unit_reconciliation fails (50700 > 48000) |
| FB-2025-108 | Post fuel surcharge revision (12%→18%) | `auto_approve` (0.99) | bill_date 2024-11-20 > revised_on 2024-10-01 → uses 18% |
| FB-2025-109 | Duplicate of FB-2025-101 | `duplicate_reject` (0.0) | Preflight: same bill_number+carrier exists in Neo4j |
| FB-2025-110 | Unknown carrier "Gati KWE" | `pending_review` | LLM normalization: no DB match → carrier_match=0.6, contract=0 |

*Score capped at 0.79 by hard override rule.

---

## Deliberate Trade-offs

| Decision | Trade-off | Why |
|----------|-----------|-----|
| **MemorySaver → PostgresSaver** | Slightly more setup | Checkpoints survive container restarts; required for real HITL |
| **Neo4j + Postgres** (two DBs) | More operational complexity | Each does what it's best at; graph traversal in SQL degrades badly at scale |
| **Gemini Flash as LLM** | Free tier (1K req/day) | Carrier normalization and explanation generation only — deterministic rules for all validation |
| **Rate-first contract selection** | Assumes billed rate is honest signal | Works correctly for overlapping contracts; date-first heuristic breaks when carrier has standard + expedited contracts active simultaneously |
| **Hard decision overrides** | Less elegant than pure scoring | Prevents score dilution — cumulative over-billing scores 0.74 (high carrier/shipment match) without the override, which would send it to human review instead of auto_dispute |
| **No auth** | Security risk | Out of scope for this assignment; production would use JWT + role-based access (ops vs finance) |
| **Sync agent via background task** | Not truly async | Sufficient for 10 bills/demo; production would use Celery or Cloud Tasks with proper retry |

---

## What I'd Do With More Time

1. **PostgresSaver with connection pool** — current `psycopg.connect()` creates one connection; production needs `ConnectionPool` for concurrency
2. **Real LangSmith tracing** — add API key to see per-node latency, token counts, full trace DAG
3. **Contract amendment workflow** — currently a new contract row is added when rates change; a proper amendment model would version contract terms with effective dates
4. **Batch ingestion** — `POST /freight-bills/batch` with background queue draining and per-bill status tracking
5. **Webhook on review complete** — `POST /review/{id}` currently blocks; production would fire a webhook to notify the ops team's ticketing system
6. **Neo4j AuraDB Pro** on GCP — AuraDB Free (50K nodes) is enough for this dataset, but production logistics data (millions of shipments) needs a managed cluster
7. **Confidence calibration** — the current weights (carrier 0.15, contract 0.25, etc.) are hand-tuned; with historical approval data, logistic regression would optimise them

---

## GCP Deployment (Bonus)

```
Cloud Run  ←  Docker image in Artifact Registry
    │
    ├── Neon.tech (free Postgres)   — agent state, decisions, audit trail
    ├── Neo4j AuraDB Free            — graph (50K nodes / 175K rels — well within limit)
    ├── GCP Secret Manager           — DATABASE_URL, NEO4J_URI, GEMINI_API_KEY
    └── LangSmith free tier          — agent trace observability (5K traces/month)
```

**Why Neon over Cloud SQL:** Cloud SQL Postgres is ~$10.60/month minimum. Neon is always-free (0.5GB, no time-based pausing), standard Postgres protocol, works with SQLAlchemy + LangGraph `PostgresSaver`. For this dataset (~10MB), Neon is sufficient.

**Cloud Run + stateless containers:** Solved by `PostgresSaver`. Agent state (paused interrupt) lives in Neon, not the container. Cloud Run can scale to zero and restart — resume still works because the checkpoint is in the DB.

**Deployed at:** https://freight-bill-processor-733950093150.us-central1.run.app

```bash
# Build and push image to Artifact Registry
gcloud builds submit --tag us-central1-docker.pkg.dev/$PROJECT/freight-app/app:latest

# Store secrets (no quotes — use printf or temp files on Windows)
printf "your-neon-url" | gcloud secrets create DATABASE_URL --data-file=-
printf "neo4j+s://your-aura-uri" | gcloud secrets create NEO4J_URI --data-file=-
printf "your-gemini-key" | gcloud secrets create GEMINI_API_KEY --data-file=-

# Grant Cloud Run access to secrets
gcloud projects add-iam-policy-binding $PROJECT \
  --member=serviceAccount:$PROJECT_NUMBER-compute@developer.gserviceaccount.com \
  --role=roles/secretmanager.secretAccessor

# Deploy
gcloud run deploy freight-bill-processor \
  --image us-central1-docker.pkg.dev/$PROJECT/freight-app/app:latest \
  --platform managed --region us-central1 \
  --allow-unauthenticated --memory 1Gi --port 8000 \
  --set-secrets="DATABASE_URL=DATABASE_URL:latest,NEO4J_URI=NEO4J_URI:latest,NEO4J_USER=NEO4J_USER:latest,NEO4J_PASSWORD=NEO4J_PASSWORD:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest" \
  --set-env-vars="LANGCHAIN_TRACING_V2=true,LANGCHAIN_PROJECT=freight-bill-processor,LOG_LEVEL=INFO,SEED_DATA_PATH=/app/seed_data.json"
```
