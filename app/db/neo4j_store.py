from neo4j import GraphDatabase
from app.config import settings
import structlog

log = structlog.get_logger()


class Neo4jStore:
    def __init__(self):
        self._driver = None

    async def connect(self):
        self._driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
        self._driver.verify_connectivity()
        self._create_constraints()
        log.info("neo4j_connected", uri=settings.neo4j_uri)

    async def close(self):
        if self._driver:
            self._driver.close()

    def _create_constraints(self):
        with self._driver.session() as s:
            constraints = [
                "CREATE CONSTRAINT carrier_id IF NOT EXISTS FOR (n:Carrier) REQUIRE n.id IS UNIQUE",
                "CREATE CONSTRAINT contract_id IF NOT EXISTS FOR (n:Contract) REQUIRE n.id IS UNIQUE",
                "CREATE CONSTRAINT lane_code IF NOT EXISTS FOR (n:Lane) REQUIRE n.code IS UNIQUE",
                "CREATE CONSTRAINT shipment_id IF NOT EXISTS FOR (n:Shipment) REQUIRE n.id IS UNIQUE",
                "CREATE CONSTRAINT bol_id IF NOT EXISTS FOR (n:BOL) REQUIRE n.id IS UNIQUE",
                "CREATE CONSTRAINT fb_id IF NOT EXISTS FOR (n:FreightBill) REQUIRE n.id IS UNIQUE",
            ]
            for c in constraints:
                s.run(c)

    # ── Seed helpers ─────────────────────────────────────────────────────────

    def upsert_carrier(self, carrier: dict):
        with self._driver.session() as s:
            s.run(
                "MERGE (n:Carrier {id: $id}) SET n += $props",
                id=carrier["id"],
                props={k: v for k, v in carrier.items() if v is not None},
            )

    def upsert_contract(self, contract: dict, lane: str):
        with self._driver.session() as s:
            # Create/merge contract node
            s.run(
                "MERGE (n:Contract {id: $id}) SET n += $props",
                id=contract["id"],
                props={
                    "id": contract["id"],
                    "carrier_id": contract["carrier_id"],
                    "effective_date": contract["effective_date"],
                    "expiry_date": contract["expiry_date"],
                    "status": contract["status"],
                },
            )
            # Link carrier → contract
            s.run(
                """
                MATCH (ca:Carrier {id: $carrier_id})
                MATCH (co:Contract {id: $contract_id})
                MERGE (ca)-[:HAS_CONTRACT]->(co)
                """,
                carrier_id=contract["carrier_id"],
                contract_id=contract["id"],
            )
            # Create lane node and link contract → lane
            s.run(
                """
                MERGE (l:Lane {code: $lane})
                WITH l
                MATCH (co:Contract {id: $contract_id})
                MERGE (co)-[:COVERS_LANE]->(l)
                """,
                lane=lane,
                contract_id=contract["id"],
            )

    def upsert_shipment(self, shipment: dict):
        with self._driver.session() as s:
            s.run(
                "MERGE (n:Shipment {id: $id}) SET n += $props",
                id=shipment["id"],
                props={k: v for k, v in shipment.items() if v is not None and k != "notes"},
            )
            # Link shipment → contract
            s.run(
                """
                MATCH (s:Shipment {id: $s_id})
                MATCH (co:Contract {id: $co_id})
                MERGE (s)-[:UNDER_CONTRACT]->(co)
                """,
                s_id=shipment["id"],
                co_id=shipment["contract_id"],
            )
            # Link shipment → lane
            s.run(
                """
                MATCH (s:Shipment {id: $s_id})
                MATCH (l:Lane {code: $lane})
                MERGE (s)-[:ON_LANE]->(l)
                """,
                s_id=shipment["id"],
                lane=shipment["lane"],
            )

    def upsert_bol(self, bol: dict):
        with self._driver.session() as s:
            s.run(
                "MERGE (n:BOL {id: $id}) SET n += $props",
                id=bol["id"],
                props={k: v for k, v in bol.items() if v is not None},
            )
            s.run(
                """
                MATCH (b:BOL {id: $bol_id})
                MATCH (s:Shipment {id: $s_id})
                MERGE (s)-[:HAS_BOL]->(b)
                """,
                bol_id=bol["id"],
                s_id=bol["shipment_id"],
            )

    # ── Agent query helpers ───────────────────────────────────────────────────

    def find_duplicate(self, bill_number: str, carrier_id: str) -> dict | None:
        with self._driver.session() as s:
            result = s.run(
                """
                MATCH (fb:FreightBill {bill_number: $bill_number, carrier_id: $carrier_id})
                RETURN fb.id AS id LIMIT 1
                """,
                bill_number=bill_number,
                carrier_id=carrier_id or "",
            )
            record = result.single()
            return {"id": record["id"]} if record else None

    def get_active_contracts(self, carrier_id: str, lane: str, bill_date: str) -> list[dict]:
        """Find all active contracts for (carrier, lane) valid on bill_date."""
        with self._driver.session() as s:
            result = s.run(
                """
                MATCH (ca:Carrier {id: $carrier_id})-[:HAS_CONTRACT]->(co:Contract)-[:COVERS_LANE]->(l:Lane {code: $lane})
                WHERE co.effective_date <= $bill_date <= co.expiry_date
                  AND co.status = 'active'
                RETURN co
                ORDER BY co.effective_date DESC
                """,
                carrier_id=carrier_id,
                lane=lane,
                bill_date=bill_date,
            )
            return [dict(r["co"]) for r in result]

    def get_all_contracts_for_lane(self, carrier_id: str, lane: str) -> list[dict]:
        """Including expired — used to check if carrier ever had a contract."""
        with self._driver.session() as s:
            result = s.run(
                """
                MATCH (ca:Carrier {id: $carrier_id})-[:HAS_CONTRACT]->(co:Contract)-[:COVERS_LANE]->(l:Lane {code: $lane})
                RETURN co ORDER BY co.expiry_date DESC
                """,
                carrier_id=carrier_id,
                lane=lane,
            )
            return [dict(r["co"]) for r in result]

    def get_shipment_with_bol(self, shipment_id: str) -> dict | None:
        with self._driver.session() as s:
            result = s.run(
                """
                MATCH (s:Shipment {id: $id})
                OPTIONAL MATCH (s)-[:HAS_BOL]->(b:BOL)
                RETURN s, collect(b) AS bols
                """,
                id=shipment_id,
            )
            record = result.single()
            if not record:
                return None
            return {
                "shipment": dict(record["s"]),
                "bols": [dict(b) for b in record["bols"]],
            }

    def get_cumulative_billed_weight(self, shipment_id: str) -> float:
        """Sum billed_weight_kg across all FreightBill nodes referencing this shipment."""
        with self._driver.session() as s:
            result = s.run(
                """
                MATCH (fb:FreightBill)-[:REFERENCES]->(s:Shipment {id: $shipment_id})
                RETURN coalesce(sum(fb.billed_weight_kg), 0) AS total_billed
                """,
                shipment_id=shipment_id,
            )
            record = result.single()
            return float(record["total_billed"]) if record else 0.0

    def add_freight_bill_node(self, bill: dict, shipment_id: str | None, contract_id: str | None):
        """Insert FreightBill node and relationships after decision."""
        with self._driver.session() as s:
            s.run(
                "MERGE (fb:FreightBill {id: $id}) SET fb += $props",
                id=bill["id"],
                props={
                    "id": bill["id"],
                    "bill_number": bill["bill_number"],
                    "carrier_id": bill.get("carrier_id") or "",
                    "billed_weight_kg": bill["billed_weight_kg"],
                    "bill_date": bill["bill_date"],
                    "lane": bill["lane"],
                },
            )
            if shipment_id:
                s.run(
                    """
                    MATCH (fb:FreightBill {id: $fb_id})
                    MATCH (s:Shipment {id: $s_id})
                    MERGE (fb)-[:REFERENCES]->(s)
                    """,
                    fb_id=bill["id"],
                    s_id=shipment_id,
                )
            if contract_id:
                s.run(
                    """
                    MATCH (fb:FreightBill {id: $fb_id})
                    MATCH (co:Contract {id: $co_id})
                    MERGE (fb)-[:MATCHED_CONTRACT]->(co)
                    """,
                    fb_id=bill["id"],
                    co_id=contract_id,
                )

    def get_evidence_chain(self, freight_bill_id: str) -> dict:
        """Full graph path for evidence display."""
        with self._driver.session() as s:
            result = s.run(
                """
                MATCH (fb:FreightBill {id: $id})
                OPTIONAL MATCH (fb)-[:MATCHED_CONTRACT]->(co:Contract)<-[:HAS_CONTRACT]-(ca:Carrier)
                OPTIONAL MATCH (fb)-[:REFERENCES]->(s:Shipment)-[:HAS_BOL]->(bol:BOL)
                RETURN fb, co, ca, s, bol
                """,
                id=freight_bill_id,
            )
            record = result.single()
            if not record:
                return {}
            return {
                "freight_bill": dict(record["fb"]) if record["fb"] else None,
                "contract": dict(record["co"]) if record["co"] else None,
                "carrier": dict(record["ca"]) if record["ca"] else None,
                "shipment": dict(record["s"]) if record["s"] else None,
                "bol": dict(record["bol"]) if record["bol"] else None,
            }


# Singleton
neo4j_store = Neo4jStore()
