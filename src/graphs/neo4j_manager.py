import os
from typing import Optional, List, Dict, Any
from neo4j import GraphDatabase
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from src.configs.schema import GraphData
from src.configs.settings import (
    NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD,
    EMBEDDING_MODEL, VECTOR_INDEX_DIM
)


class Neo4jLoader:
    def __init__(self):
        self.uri = NEO4J_URI
        self.user = NEO4J_USER
        self.password = NEO4J_PASSWORD

        try:
            self.driver = GraphDatabase.driver(self.uri, auth=(self.user, self.password))
            self.driver.verify_connectivity()
            print("🔌 Neo4j 데이터베이스 연결 성공!")
        except Exception as e:
            print(f"❌ Neo4j 연결 실패: {e}")
            self.driver = None

    def close(self):
        if self.driver:
            self.driver.close()

    def create_vector_index(self):
        """NewsArticle 노드의 embedding 속성에 대한 Neo4j Vector Index 생성 (Gemini v4: 3072차원)"""
        if not self.driver: return
        try:
            with self.driver.session() as session:
                # 기존 인덱스 삭제 후 재생성
                session.run("DROP INDEX article_embedding IF EXISTS")
                session.run("DROP INDEX batch_embedding IF EXISTS") # 과거 구형 인덱스 청소
                session.run(f"""
                CREATE VECTOR INDEX article_embedding IF NOT EXISTS
                FOR (a:NewsArticle) ON (a.embedding)
                OPTIONS {{indexConfig: {{
                  `vector.dimensions`: {VECTOR_INDEX_DIM},
                  `vector.similarity_function`: 'cosine'
                }}}}
                """)
                print("🧠 NewsArticle Vector Index 확인 완료!")
        except Exception as e:
            print(f"⚠️ Vector 인덱스 생성 오류: {e}")

    def clear_database(self):
        if not self.driver: return
        with self.driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n")
            print("🧹 Neo4j 데이터베이스 초기화 완료.")

    def get_keyword_watermarks(self, keyword: str) -> dict:
        """키워드 노드에 저장된 날짜별 마지막 수집 시각(watermarks)을 반환합니다."""
        if not self.driver: return {}
        with self.driver.session() as session:
            result = session.run("MATCH (k:Keyword {name: $k}) RETURN coalesce(k.watermarks, '{}') AS wm", k=keyword).single()
            if result:
                import json
                try:
                    return json.loads(result["wm"])
                except:
                    pass
        return {}

    def get_keyword_article_dates(self, keyword: str) -> List[str]:
        """키워드와 연결된 기사들의 발행 날짜(YYYY-MM-DD) 목록을 반환합니다."""
        if not self.driver:
            return []
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (:Keyword {name: $k})-[:HAS_ARTICLE]->(a:NewsArticle)
                WHERE a.published_at IS NOT NULL
                RETURN DISTINCT toString(date(a.published_at)) AS published_date
                ORDER BY published_date
                """,
                k=keyword,
            )
            return [record["published_date"] for record in result if record.get("published_date")]
        
    def update_keyword_watermarks(self, keyword: str, new_wms: dict):
        """키워드 노드에 날짜별 마지막 수집 시각(watermarks)을 병합하여 업데이트합니다."""
        if not self.driver: return
        with self.driver.session() as session:
            import json
            # 기존 워터마크를 불러와서 새 워터마크 중 더 나중 시간인 것들만 갱신
            result = session.run("MATCH (k:Keyword {name: $k}) RETURN coalesce(k.watermarks, '{}') AS wm", k=keyword).single()
            current = {}
            if result:
                try:
                    current = json.loads(result["wm"])
                except:
                    pass
            
            for date_str, time_str in new_wms.items():
                if date_str not in current or time_str > current[date_str]:
                    current[date_str] = time_str
                    
            session.run("MERGE (k:Keyword {name: $k}) SET k.watermarks = $wm, k.last_updated = datetime()", 
                        k=keyword, wm=json.dumps(current))

    def filter_new_urls(self, urls: List[str]) -> List[str]:
        """주어진 URL 목록 중 DB에 존재하지 않는(신규) URL만 필터링하여 반환합니다."""
        if not self.driver or not urls: return []
        with self.driver.session() as session:
            # 일괄 조회를 위해 UNWIND 사용 (성능 최적화)
            result = session.run("""
                UNWIND $urls AS url
                OPTIONAL MATCH (a:NewsArticle {id: url})
                WITH url, a
                WHERE a IS NULL
                RETURN url
            """, urls=urls)
            return [record["url"] for record in result]

    def upsert_keyword(self, keyword: str):
        if not self.driver: return
        with self.driver.session() as session:
            session.run("MERGE (k:Keyword {name: $keyword}) SET k.last_updated = datetime()", keyword=keyword)

    def upsert_articles(self, keyword: str, articles: List[Dict[str, Any]]):
        if not self.driver or not articles: return
        with self.driver.session() as session:
            for article in articles:
                session.run(
                    """
                    // 1. 키워드 노드 먼저 확실히 생성/업데이트
                    MERGE (k:Keyword {name: $keyword})
                    SET k.last_updated = datetime()
                    
                    WITH k
                    // 2. 기사 노드 생성 및 속성 설정 (독립적으로 실행)
                    MERGE (a:NewsArticle {id: $url})
                    SET a.url = $url,
                        a.title = $title,
                        a.published_at = $published_at,
                        a.keyword = $keyword
                    
                    WITH k, a
                    // 3. 관계 연결
                    MERGE (k)-[:HAS_ARTICLE]->(a)
                    """,
                    keyword=keyword, url=article.get("url", ""),
                    title=article.get("title", ""), published_at=article.get("published_at")
                )

    def load_graph_data(self, graph_data: GraphData, batch_text: Optional[str] = None):
        """
        [지능형 데이터 적재]
        기존의 NewsArticle 노드(크롤러)와 추출된 엔티티를 직접 연결하고, 
        기사별 텍스트를 벡터 임베딩하여 NewsArticle 노드에 저장합니다.
        """
        if not self.driver: return
        
        import re
        google_api_key = os.getenv("GOOGLE_API_KEY")
        embedder = GoogleGenerativeAIEmbeddings(model=EMBEDDING_MODEL, google_api_key=google_api_key)

        with self.driver.session() as session:
            # 0. 기사별 임베딩 생성 및 저장
            if batch_text:
                # '---' 구분자로 개별 기사 분리
                article_blocks = batch_text.split("\n---\n")
                for block in article_blocks:
                    block = block.strip()
                    if not block: continue
                    
                    # URL 추출
                    url_match = re.search(r'링크:\s*([^\s]+)', block)
                    if url_match:
                        url = url_match.group(1)
                        try:
                            # 개별 기사 임베딩 생성
                            article_embedding = embedder.embed_query(block)
                            
                            # NewsArticle 노드에 텍스트와 임베딩 저장
                            session.run(
                                """
                                MERGE (a:NewsArticle {id: $url})
                                SET a.text = $text, a.embedding = $embedding
                                """,
                                url=url, text=block, embedding=article_embedding
                            )
                        except Exception as e:
                            print(f"⚠️ 기사 임베딩 생성 실패 ({url}): {e}")

            # 1. 노드 적재
            for entity in graph_data.entities:
                name = entity.name.strip()
                label = entity.type.replace(" ", "_").strip()
                if not label: label = "Entity"

                props = {"id": name, "name": name}

                query = f"""
                MERGE (n:Entity {{id: $id}}) 
                SET n:`{label}`, 
                    n.name = $name
                """
                session.run(query, **props)
                
                # NewsArticle와 Entity의 직접 연결 (어떤 기사에서 언급되었는지 명시)
                if batch_text:
                    # 해당 엔티티가 언급된 기사들의 URL을 찾음 (batch_text 전체 기준)
                    # 실제로는 LLM 결과의 source_url을 사용하는 것이 더 정확함 (2단계에서 처리)
                    pass

            # 2. 관계 적재 (핵심: 다각도 매칭으로 사일로 제거)
            for rel in graph_data.relations:
                edge_type = rel.type.replace(" ", "_").strip().upper()
                if not edge_type: edge_type = "RELATED_TO"

                # 엔티티 간의 관계 생성 및 원본 기사 직결
                rel_query = """
                MATCH (s:Entity) WHERE s.id = $s_name OR s.name = $s_name
                MATCH (t:Entity) WHERE t.id = $t_name OR t.name = $t_name
                
                MERGE (s)-[r:__EDGE_TYPE__]->(t)
                SET r.description = $desc, 
                    r.source_url = CASE WHEN $s_url IS NOT NULL AND $s_url <> '' THEN $s_url ELSE r.source_url END,
                    r.source_article = CASE WHEN $s_art IS NOT NULL AND $s_art <> '' THEN $s_art ELSE r.source_article END,
                    r.article_id = CASE WHEN $article_id IS NOT NULL AND $article_id <> '' THEN $article_id ELSE r.article_id END,
                    r.provenance = CASE WHEN $prov IS NOT NULL AND $prov <> '' THEN $prov ELSE coalesce(r.provenance, 'article') END
                
                WITH s, t
                OPTIONAL MATCH (a:NewsArticle {id: $s_url})
                FOREACH (_ IN CASE WHEN a IS NULL THEN [] ELSE [1] END |
                    MERGE (a)-[:MENTIONS]->(s)
                    MERGE (a)-[:MENTIONS]->(t)
                )
                """
                rel_query = rel_query.replace("__EDGE_TYPE__", edge_type)
                session.run(rel_query, 
                    s_name=rel.source.strip(), t_name=rel.target.strip(),
                    desc=rel.description,
                    s_url=rel.source_url,
                    s_art=rel.source_article,
                    article_id=rel.article_id,
                    prov=rel.provenance,
                )
