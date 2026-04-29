# Freight Bill Processing System

An AI-powered freight bill auditing system using **FastAPI**, **LangGraph**, **PostgreSQL (Neon)**, and **Neo4j (AuraDB)**. Processes freight bills against contracts and shipments, auto-approves clean bills, disputes anomalies, and pauses for human review on ambiguous cases.

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
cp .env.example .env
# Edit .env: fill in GEMINI_API_KEY (free at https://aistudio.google.com)
docker-compose up
```

On startup the app automatically:
1. Runs Alembic migrations → all tables created in Postgres
2. Loads `seed_data.json` → 5 carriers, 8 contracts, 7 shipments, 7 BOLs into Postgres
3. Seeds Neo4j graph → same data as relationship nodes/edges

**Swagger UI:** http://localhost:8000/docs  
**Neo4j Browser:** http://localhost:7474 (user: `neo4j`, pass: `localpass`)

### Run tests

```bash
docker exec freight_app pytest tests/ -v
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/freight-bills` | Ingest a freight bill, trigger agent (async background task) |
| `GET` | `/freight-bills/{id}` | Status, decision type, confidence breakdown, graph evidence chain |
| `GET` | `/review-queue` | All bills currently waiting for human review |
| `POST` | `/review/{id}` | Submit human decision — resumes the paused LangGraph agent |
| `GET` | `/metrics` | Decision counts, avg confidence score, breakdown by outcome |
| `GET` | `/health` | Health check |

---

## End-to-End Walkthrough

### 1. Auto-approve — clean bill (FB-2025-101)

```bash
curl -X POST http://localhost:8000/freight-bills \
  -H "Content-Type: application/json" \
  -d '{
    "id": "FB-2025-101",
    "carrier_name": "Safexpress Logistics",
    "bill_number": "SFX/2025/00234",
    "bill_date": "2025-02-15",
    "shipment_reference": "SHP-2025-002",
    "lane": "DEL-BLR",
    "billed_weight_kg": 850,
    "rate_per_kg": 15.00,
    "base_charge": 12750.00,
    "fuel_surcharge": 1020.00,
    "gst_amount": 2479.00,
    "total_amount": 16249.00
  }'

curl http://localhost:8000/freight-bills/FB-2025-101
# → status: "approved", decision: "auto_approve", confidence: ~0.99
```

### 2. Auto-dispute — over-billing (FB-2025-103 then FB-2025-104)

FB-2025-103 bills 800kg for shipment SHP-2025-001. FB-2025-104 then bills another 1500kg for the same shipment. Cumulative = 2300kg > 2000kg shipment total.

```bash
# First bill (approved — 800kg of 2000kg)
curl -X POST http://localhost:8000/freight-bills \
  -H "Content-Type: application/json" \
  -d '{
    "id": "FB-2025-103",
    "carrier_name": "Safexpress Logistics",
    "bill_number": "SFX/2025/00245",
    "bill_date": "2025-02-25",
    "shipment_reference": "SHP-2025-001",
    "lane": "DEL-BOM",
    "billed_weight_kg": 800,
    "rate_per_kg": 12.50,
    "base_charge": 10000.00,
    "fuel_surcharge": 800.00,
    "gst_amount": 1944.00,
    "total_amount": 12744.00
  }'

# Second bill — over-billing, will be auto_dispute
curl -X POST http://localhost:8000/freight-bills \
  -H "Content-Type: application/json" \
  -d '{
    "id": "FB-2025-104",
    "carrier_name": "Safexpress Logistics",
    "bill_number": "SFX/2025/00267",
    "bill_date": "2025-03-15",
    "shipment_reference": "SHP-2025-001",
    "lane": "DEL-BOM",
    "billed_weight_kg": 1500,
    "rate_per_kg": 12.50,
    "base_charge": 18750.00,
    "fuel_surcharge": 1500.00,
    "gst_amount": 3645.00,
    "total_amount": 23895.00
  }'

curl http://localhost:8000/freight-bills/FB-2025-104
# → status: "disputed", decision: "auto_dispute"
# evidence shows: cumulative_billed=2300kg > shipment_total=2000kg
```

### 3. Human-in-the-loop — ambiguous contracts (FB-2025-102)

Safexpress has 3 active overlapping contracts on DEL-BOM. No shipment reference is provided. Agent cannot disambiguate with confidence → pauses and waits for human review.

```bash
# Step 1: Submit bill
curl -X POST http://localhost:8000/freight-bills \
  -H "Content-Type: application/json" \
  -d '{
    "id": "FB-2025-102",
    "carrier_name": "Safexpress Logistics",
    "bill_number": "SFX/2025/00241",
    "bill_date": "2025-02-28",
    "lane": "DEL-BOM",
    "billed_weight_kg": 1100,
    "rate_per_kg": 13.20,
    "base_charge": 14520.00,
    "fuel_surcharge": 1161.60,
    "gst_amount": 2826.29,
    "total_amount": 18507.89
  }'

# Step 2: See it in review queue with full evidence
curl http://localhost:8000/review-queue

# Step 3: Submit human decision (approve / dispute / modify)
curl -X POST http://localhost:8000/review/FB-2025-102 \
  -H "Content-Type: application/json" \
  -d '{"decision": "approve", "notes": "Rate matches CC-2025-SFX-003"}'

# Step 4: Check final status
curl http://localhost:8000/freight-bills/FB-2025-102
# → status: "approved", decision: "human_approve"
```

### 4. Duplicate rejection (FB-2025-109)

Same `bill_number` + `carrier_id` as FB-2025-101 — caught in preflight, never reaches the agent.

```bash
curl -X POST http://localhost:8000/freight-bills \
  -H "Content-Type: application/json" \
  -d '{
    "id": "FB-2025-109",
    "carrier_name": "Safexpress Logistics",
    "bill_number": "SFX/2025/00234",
    "bill_date": "2025-02-15",
    "lane": "DEL-BLR",
    "billed_weight_kg": 850,
    "rate_per_kg": 15.00,
    "base_charge": 12750.00,
    "fuel_surcharge": 1020.00,
    "gst_amount": 2479.00,
    "total_amount": 16249.00
  }'

curl http://localhost:8000/freight-bills/FB-2025-109
# → status: "duplicate", decision: "duplicate_reject"
```

---

## All 10 Scenarios

| Bill | Scenario | Expected Decision | Key Logic |
|------|----------|-------------------|-----------|
| FB-2025-101 | Clean match | `auto_approve` (~0.99) | All rules pass — exact carrier, contract, BOL match |
| FB-2025-102 | 3 overlapping active contracts, no shipment ref | `pending_review` (~0.63) | Rate matches CC-2025-SFX-003 uniquely but no reference to confirm |
| FB-2025-103 | 2nd truck partial delivery (800kg) | `auto_approve` (~0.99) | Cumulative: 0+800 ≤ 2000kg shipment total |
| FB-2025-104 | Over-billing (prior 800kg + this 1500kg = 2300 > 2000) | `auto_dispute` | Hard override: cumulative weight breached |
| FB-2025-105 | Rate drift ₹8.70 vs ₹8.00 (+8.75%) | `pending_review` (~0.79) | Hard cap: rate deviation >5% forces review |
| FB-2025-106 | Billing at expired contract rate | `auto_dispute` (~0.44) | Hard override: billed ₹7.50 exactly matches expired contract CC-2023-TCI-001 |
| FB-2025-107 | FTL contract billed per-kg (₹50,700 > FTL ₹48,000) | `pending_review` (~0.79) | Hard cap: unit_reconciliation fails |
| FB-2025-108 | Fuel surcharge revision applied correctly (12%→18%) | `auto_approve` (~0.99) | bill_date 2024-11-20 > revised_on 2024-10-01 → uses revised 18% |
| FB-2025-109 | Duplicate of FB-2025-101 | `duplicate_reject` (0.0) | Preflight: same bill_number+carrier already in Neo4j |
| FB-2025-110 | Unknown carrier "Gati KWE Logistics" | `pending_review` (~0.10) | LLM finds no DB match → no contract → confidence too low |

---

## Schema Design

### Why PostgreSQL + Neo4j (not one or the other)

Each database does what it is built for:

- **PostgreSQL** owns transactional state: agent checkpoints (ACID-durable across restarts), decisions, audit trail, and reference data. Every freight bill write is a transaction — partial writes roll back.
- **Neo4j** owns relationship traversal: finding active contracts for a (carrier, lane, date) triple, and summing billed weights across all bills referencing the same shipment. A 3-hop `Carrier→Contract→Lane→Shipment` traversal is one Cypher `MATCH`; in SQL it is a 4-table join with date-range predicates.

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

- `rate_cards.revised_on` + `revised_fuel_surcharge_pct`: mid-contract fuel surcharge revisions (FB-2025-108) are stored on the rate card row, not as a new contract. The validator checks `bill_date >= revised_on` to pick the correct percentage — no schema migration required for a revision.
- `freight_bills.status` state machine: `pending → processing → approved | disputed | pending_review | duplicate`. The `pending_review` state means the LangGraph agent is paused at an `interrupt()` — its checkpoint is live in Postgres.
- `decisions.evidence JSONB`: stores the full confidence breakdown and per-rule validation results so a reviewer can see everything without re-running the agent.

---

## Graph Model (Neo4j)

### Nodes
`Carrier`, `Contract`, `Lane`, `Shipment`, `BOL`, `FreightBill`

### Relationships

```
Carrier     ──[HAS_CONTRACT]────▶  Contract
Contract    ──[COVERS_LANE]─────▶  Lane
Shipment    ──[ON_LANE]──────────▶  Lane
Shipment    ──[UNDER_CONTRACT]──▶  Contract
Shipment    ──[HAS_BOL]──────────▶  BOL
FreightBill ──[REF_SHIPMENT]─────▶  Shipment    (added when bill has shipment_reference)
FreightBill ──[MATCHED_CONTRACT]─▶  Contract    (added after agent completes)
```

### The graph is mutable

`FreightBill` nodes are inserted into Neo4j as each bill is processed. This enables the cumulative over-billing check — the graph accumulates all prior bills referencing the same shipment and the query returns their sum in a single Cypher statement.

### Key Cypher queries

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

**Why Neo4j over Postgres JOINs:** The 3-contract overlap scenario (FB-2025-102: CAR001 has three active contracts on DEL-BOM simultaneously) is a graph query. In SQL it requires a self-join with date-range predicates that becomes unreadable and slow as contract history grows. In Cypher it is a single parameterised `MATCH`. The graph is also the natural structure for the evidence chain returned in `GET /freight-bills/{id}`.

**Why Neo4j over NetworkX:** Neo4j is persistent (survives restarts), accessible from Cloud Run via HTTPS, and the Neo4j Browser at port 7474 lets the interviewer visually explore the carrier-contract-shipment graph. NetworkX is in-memory only and adds no production signal.

---

## Confidence Scoring

Weighted sum across 5 dimensions, each scored 0–1:

| Dimension | Weight | 1.0 | Partial | 0.0 |
|-----------|--------|-----|---------|-----|
| `carrier_match` | 0.15 | Exact DB match | LLM fuzzy match (0.7) | Not found |
| `contract_match` | 0.25 | Unique rate match via shipment ref | Ambiguous overlapping (0.65) | None active / expired |
| `shipment_bol_match` | 0.20 | Ref present + BOL found | No BOL (0.5) | No ref (0.2) |
| `charge_validation` | 0.25 | All rules pass | Partial passes | Critical failure (0.0) |
| `duplicate_check` | 0.15 | No duplicate found | — | Duplicate found (0.0) |

**Final score** = sum of (dimension score × weight).

### Charge validation rules (deterministic — no LLM)

1. `weight_vs_bol` — billed weight vs BOL actual weight (±5% tolerance)
2. `rate_vs_contract` — billed rate vs contract `rate_per_kg` (±₹0.01)
3. `fuel_surcharge` — `base_charge × fuel_pct`; uses `revised_fuel_surcharge_pct` when `bill_date >= rate_card.revised_on`
4. `base_charge_calculation` — must equal `max(weight × rate, min_charge)`
5. `cumulative_weight` — sum of all prior billed weights for this shipment + this bill ≤ shipment total
6. `unit_reconciliation` — if FTL contract: per-kg total must not exceed FTL flat rate

### Hard decision overrides in `decide` node

Some signals override the weighted score entirely:

| Override | Trigger | Outcome |
|----------|---------|---------|
| Cumulative over-billing | `cumulative_weight` rule fails | Always `auto_dispute` |
| Expired contract rate | Billed rate matches an expired contract exactly | Always `auto_dispute` |
| Rate drift > 5% | `rate_vs_contract` deviation > 5% | Score capped at 0.79 → `pending_review` |
| FTL/unit mismatch | `unit_reconciliation` rule fails | Score capped at 0.79 → `pending_review` |

Without these overrides, FB-2025-104 would score ~0.74 (high carrier/shipment match dragging the number up) and go to human review instead of `auto_dispute`. The overrides prevent score dilution on business-critical violations.

### Decision thresholds

- **≥ 0.80** → `auto_approve`
- **0.40 – 0.79** → `interrupt()` → human review
- **< 0.40** → `auto_dispute`

---

## Human-in-the-Loop (HITL)

This uses LangGraph's real `interrupt()` / `Command(resume=...)` pattern — not a polling workaround or a separate workflow.

### Flow

```
POST /freight-bills
        │
        ▼
   decide_node  (score = 0.63 — below 0.80 threshold)
        │
        ▼  interrupt(payload={reason, evidence, confidence_breakdown})
   [Graph PAUSED — full AgentState checkpointed to Postgres via PostgresSaver]
        │
   freight_bills.status = "pending_review"
        │
   GET /review-queue  ← ops team sees bill + evidence + confidence score
        │
   POST /review/{id}  {"decision": "approve", "notes": "..."}
        │
        ▼  Command(resume=human_decision)
   [Graph RESUMES from Postgres checkpoint — no state re-derived]
        │
        ▼
   explain_node → writes final decision → END
```

### Why PostgresSaver, not MemorySaver

The checkpoint lives in Neon (cloud Postgres), not RAM. The API container can restart, scale to zero, or be replaced between `interrupt()` and `resume()` — the graph picks back up from exactly where it paused. This is the production-safe HITL pattern: the review queue and the agent state are in the same durable store.

The `interrupt()` payload contains the complete evidence — confidence breakdown, per-rule results, matched contract/shipment/BOL — so the reviewer has everything needed to make a decision without re-running anything.

---

## The Agent (LangGraph)

### Node sequence

```
preflight_check     ← Neo4j: duplicate bill_number + carrier? → immediate reject
normalize_carrier   ← Gemini LLM: fuzzy-match carrier_name to carriers table
match_entities      ← Neo4j Cypher: find contracts, shipments, BOLs
validate_charges    ← Deterministic rules only (charge_validator.py)
score_confidence    ← Weighted sum across 5 dimensions
decide              ← auto_approve / interrupt() / auto_dispute
explain             ← Gemini LLM: generate human-readable evidence summary
                      + persist FreightBill node to Neo4j
```

### LLM usage (Gemini Flash)

LLM is used in exactly two places — everything else is deterministic:

1. `normalize_carrier`: maps free-text carrier names to the carriers table (handles typos, alternate names, "Gati KWE Logistics" → no match)
2. `explain`: generates the `reasoning` text in the decision record from the structured evidence

All charge validation, contract selection, and confidence scoring are rule-based. The LLM cannot approve or dispute a bill on its own.

---

## Tests

Tests cover the core business logic in isolation — no live DB, no LLM calls required.

```bash
docker exec freight_app pytest tests/ -v
```

| Test | Scenario | What it verifies |
|------|----------|-----------------|
| `test_fb104_overbilling_detected` | FB-2025-104 | `cumulative_weight` rule fails; `overage_kg=300`; charge score = 0.0 |
| `test_fb108_revised_fuel_surcharge_applied` | FB-2025-108 | Validator picks `revised_fuel_surcharge_pct=18%` for bill_date after `revised_on` |
| `test_fb101_clean_match_passes_all_rules` | FB-2025-101 | All 6 validation rules pass for a clean bill |
| `test_fb105_rate_drift_detected` | FB-2025-105 | `rate_vs_contract` deviation >8% → rule fails |
| `test_fb107_ftl_per_kg_more_expensive` | FB-2025-107 | `unit_reconciliation` flags per-kg total (50,700) > FTL rate (48,000) |

---

## Deliberate Trade-offs

| Decision | Trade-off | Why |
|----------|-----------|-----|
| **PostgresSaver** instead of MemorySaver | Slightly more setup complexity | Checkpoints survive container restarts — required for real HITL on Cloud Run |
| **Neo4j + Postgres** (two databases) | More operational surface | Each does what it is built for; graph traversal in Postgres degrades badly at scale with overlapping date ranges |
| **Gemini Flash as LLM** | Free tier (1,000 req/day limit) | Normalization and explanation only — all validation is deterministic; 10 bills use <20 LLM calls |
| **Rate-first contract selection** | Assumes billed rate is an honest signal | Works correctly for overlapping contracts; date-first heuristic breaks when a carrier has standard and expedited contracts active simultaneously (the SFX case) |
| **Hard decision overrides** | Less elegant than pure scoring | Prevents score dilution — cumulative over-billing (FB-2025-104) reaches ~0.74 without the override because carrier and shipment dimensions score high |
| **No authentication** | Security risk | Out of scope; production would use JWT + role-based access (ops vs finance) |
| **Sync agent via background task** | Not truly async | Sufficient for this dataset; production would use Celery or Cloud Tasks with retry |

---

## What I'd Do With More Time

1. **PostgresSaver connection pool** — current implementation creates one connection per request; production needs `psycopg.ConnectionPool` for concurrent bill processing
2. **Contract amendment workflow** — currently a rate change requires a new contract row; a proper model would version contract terms with `amended_on` + `previous_version_id`
3. **Batch ingestion endpoint** — `POST /freight-bills/batch` with a background queue and per-bill status tracking, rather than one request per bill
4. **Webhook on review complete** — `POST /review/{id}` currently blocks on agent resume; production would fire a webhook to notify the ops team's ticketing system
5. **Confidence weight calibration** — current weights are hand-tuned; with historical approval data, logistic regression would optimise the per-dimension weights
6. **Neo4j AuraDB Pro** for production scale — AuraDB Free (50K nodes, 175K rels) is sufficient for this dataset, but a real logistics ledger (millions of shipments) needs a managed cluster
7. **Cloud SQL instead of Neon** for GCP-native managed Postgres with VPC peering and IAM-based auth

---

## GCP Deployment

```
Cloud Run  ←  Docker image in Artifact Registry
    │
    ├── Neon.tech (free Postgres)    — agent state, decisions, audit trail
    ├── Neo4j AuraDB Free            — graph (50K nodes / 175K rels limit — we use ~30/60)
    ├── GCP Secret Manager           — DATABASE_URL, NEO4J_URI, GEMINI_API_KEY, LANGCHAIN_API_KEY
    └── LangSmith free tier          — agent trace observability (5K traces/month)
```

**Why Neon over Cloud SQL:** Cloud SQL Postgres costs ~$10.60/month minimum. Neon is always-free (0.5GB, no time-based pausing), standard Postgres protocol, works with SQLAlchemy and `PostgresSaver` out of the box.

**Cloud Run + stateless containers:** Solved by `PostgresSaver`. Agent state (paused interrupt) lives in Neon, not the container. Cloud Run can scale to zero and restart freely — `resume()` still works because the checkpoint is in the DB.

**Deployed at:** https://freight-bill-processor-733950093150.us-central1.run.app

### Deploy commands

```bash
# 1. Build and push image
gcloud builds submit \
  --tag us-central1-docker.pkg.dev/$PROJECT/freight-app/app:latest

# 2. Store secrets (use printf or temp file — avoid Windows CMD echo which adds quotes)
printf "postgresql://..." | gcloud secrets create DATABASE_URL --data-file=-
printf "neo4j+s://..."    | gcloud secrets create NEO4J_URI --data-file=-
printf "your-user"        | gcloud secrets create NEO4J_USER --data-file=-
printf "your-password"    | gcloud secrets create NEO4J_PASSWORD --data-file=-
printf "AIza..."          | gcloud secrets create GEMINI_API_KEY --data-file=-
printf "lsv2_pt_..."      | gcloud secrets create LANGCHAIN_API_KEY --data-file=-

# 3. Grant Cloud Run service account access to secrets
gcloud projects add-iam-policy-binding $PROJECT \
  --member=serviceAccount:$PROJECT_NUMBER-compute@developer.gserviceaccount.com \
  --role=roles/secretmanager.secretAccessor

# 4. Deploy
gcloud run deploy freight-bill-processor \
  --image us-central1-docker.pkg.dev/$PROJECT/freight-app/app:latest \
  --platform managed --region us-central1 \
  --allow-unauthenticated --memory 1Gi --port 8000 \
  --set-secrets="DATABASE_URL=DATABASE_URL:latest,NEO4J_URI=NEO4J_URI:latest,NEO4J_USER=NEO4J_USER:latest,NEO4J_PASSWORD=NEO4J_PASSWORD:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest,LANGCHAIN_API_KEY=LANGCHAIN_API_KEY:latest" \
  --set-env-vars="LANGCHAIN_TRACING_V2=true,LANGCHAIN_PROJECT=freight-bill-processor,LOG_LEVEL=INFO,SEED_DATA_PATH=/app/seed_data.json"
```
