import os
from datetime import datetime
from typing import Optional, List, Dict, Any
from neo4j import GraphDatabase
from src.configs.schema import GraphData


class Neo4jLoader:
    """
    추출된 지식 그래프 데이터(Entities, Relations)를
    로컬 또는 원격 Neo4j 데이터베이스에 적재하는 클래스입니다.

    검색어(Keyword)별로 처리된 기사(Article)를 추적하여
    재검색 시 마지막 기사 이후의 새 기사만 증분(Incremental) 업데이트합니다.
    """

    def __init__(self):
        self.uri = os.getenv("NEO4J_URI", "neo4j://localhost:7687")
        self.user = os.getenv("NEO4J_USER", "neo4j")
        self.password = os.getenv("NEO4J_PASSWORD", "testtest")

        try:
            self.driver = GraphDatabase.driver(self.uri, auth=(self.user, self.password))
            self.driver.verify_connectivity()
            print("🔌 Neo4j 데이터베이스 연결 성공!")
        except Exception as e:
            print(f"❌ Neo4j 연결 실패: {e}")
            self.driver = None

    def close(self):
        """데이터베이스 드라이버 연결을 안전하게 종료합니다."""
        if self.driver:
            self.driver.close()

    # ──────────────────────────────────────────
    # 증분 업데이트: Keyword / Article 트래킹
    # ──────────────────────────────────────────

    def get_last_article_date(self, keyword: str) -> Optional[datetime]:
        """
        해당 키워드로 마지막으로 처리된 기사의 published_at을 반환합니다.
        처음 검색하는 키워드이면 None을 반환합니다.
        """
        if not self.driver:
            return None
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (:Keyword {name: $keyword})-[:HAS_ARTICLE]->(a:Article)
                RETURN max(a.published_at) AS last_date
                """,
                keyword=keyword,
            )
            record = result.single()
            if record and record["last_date"]:
                # Neo4j datetime → Python datetime 변환
                neo4j_dt = record["last_date"]
                try:
                    return neo4j_dt.to_native()
                except Exception:
                    return None
        return None

    def upsert_keyword(self, keyword: str):
        """Keyword 노드를 생성하거나 last_updated를 갱신합니다."""
        if not self.driver:
            return
        with self.driver.session() as session:
            session.run(
                """
                MERGE (k:Keyword {name: $keyword})
                SET k.last_updated = datetime()
                """,
                keyword=keyword,
            )

    def upsert_articles(self, keyword: str, articles: List[Dict[str, Any]]):
        """
        처리된 기사 목록을 Article 노드로 저장하고 Keyword와 연결합니다.
        이미 존재하는 URL은 MERGE로 중복 생성을 방지합니다.
        Keyword.last_updated는 현재 시각이 아닌 실제 기사의 최신 발행일로 기록됩니다.

        articles: [{"url": str, "title": str, "published_at": datetime}, ...]
        """
        if not self.driver or not articles:
            return

        # 실제 기사 발행일 중 가장 최신 날짜 산출 (Keyword.last_updated 기준값)
        valid_dates = [a["published_at"] for a in articles if a.get("published_at")]
        latest_pub_date = max(valid_dates) if valid_dates else None

        with self.driver.session() as session:
            # 1. Article 노드 및 Keyword-Article 관계 적재
            for article in articles:
                session.run(
                    """
                    MERGE (k:Keyword {name: $keyword})
                    MERGE (a:Article {url: $url})
                    ON CREATE SET
                        a.title        = $title,
                        a.published_at = $published_at,
                        a.keyword      = $keyword
                    MERGE (k)-[:HAS_ARTICLE]->(a)
                    """,
                    keyword=keyword,
                    url=article.get("url", ""),
                    title=article.get("title", ""),
                    published_at=article.get("published_at"),
                )

            # 2. Keyword.last_updated를 실제 기사 최신 발행일로 갱신
            if latest_pub_date:
                session.run(
                    """
                    MERGE (k:Keyword {name: $keyword})
                    SET k.last_updated = $last_updated
                    """,
                    keyword=keyword,
                    last_updated=latest_pub_date,
                )


    # ──────────────────────────────────────────
    # 그래프 적재 (Entity / Relation)
    # ──────────────────────────────────────────

    def load_graph_data(self, graph_data: GraphData):
        """
        Pydantic GraphData 객체를 받아 파싱한 뒤 Cypher 쿼리를 통해 노드와 엣지를 생성합니다.
        멱등성(Idempotency)을 지키기 위해 CREATE 대신 MERGE를 활용합니다.
        """
        if not self.driver:
            print("🚫 Neo4j 드라이버가 초기화되지 않아 적재를 건너뜁니다.")
            return

        with self.driver.session() as session:
            # 1. 노드(Entities) 적재
            for entity in graph_data.entities:
                name = entity.name.strip()
                label = entity.type.replace(" ", "_").strip().capitalize()
                if not label:
                    label = "Entity"

                # 모든 노드를 'Entity'라는 공통 라벨로 먼저 MERGE하여 ID 중복 방지 (이름 앞뒤 공백 제거)
                # 그 후 LLM이 지정한 구체적인 타입을 추가 라벨로 설정
                node_query = f"""
                MERGE (n:Entity {{id: $name}})
                ON CREATE SET n.name = $name
                SET n:`{label}`
                """
                session.run(node_query, name=name)

            # 2. 엣지(Relations) 적재
            for rel in graph_data.relations:
                s_name = rel.source.strip()
                t_name = rel.target.strip()
                edge_type = rel.type.replace(" ", "_").strip().upper()
                if not edge_type:
                    edge_type = "RELATED_TO"

                # 한 기사(source_url) 내에서 하나의 노드 쌍+관계타입은 단 하나만 존재하도록 MERGE
                edge_query = f"""
                MATCH (s:Entity {{id: $source_name}})
                MATCH (t:Entity {{id: $target_name}})
                MERGE (s)-[r:`{edge_type}` {{source_url: $source_url}}]->(t)
                SET r.description    = $description,
                    r.source_article = $source_article
                """
                session.run(
                    edge_query,
                    source_name=s_name,
                    target_name=t_name,
                    description=rel.description or "",
                    source_article=rel.source_article or "",
                    source_url=rel.source_url or "",
                )
