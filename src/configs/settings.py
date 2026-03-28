"""
settings.py — 프로젝트 전역 튜닝 파라미터 관리
=====================================================
이 파일에서 모든 하이퍼파라미터를 한 곳에서 관리합니다.
코드를 수정하지 않고 이 파일만 변경하면 파이프라인 동작을 조정할 수 있습니다.
"""
import os

BASE_DIR: str = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))

# ──────────────────────────────────────────
# LLM 설정
# ──────────────────────────────────────────

# 사용할 Gemini 모델명
# 변경 예시: "gemini-2.0-flash", "gemini-1.5-pro"
LLM_MODEL: str = os.getenv("LLM_MODEL", "gemini-2.5-flash")

# 임베딩 모델명 (Neo4j 벡터 인덱스 생성 및 검색에 사용)
EMBEDDING_MODEL: str = "models/gemini-embedding-001"

# 임베딩 벡터 차원 수 (gemini-embedding-001 기준)
# 모델 변경 시 반드시 이 값도 함께 수정 필요 (Neo4j 인덱스 재생성 필요)
VECTOR_INDEX_DIM: int = 3072


# ──────────────────────────────────────────
# Neo4j 연결 설정
# ──────────────────────────────────────────

# Neo4j 서버 접속 정보 (환경 변수 우선, 없으면 기본값)
NEO4J_URI: str = os.getenv("NEO4J_URI", "neo4j://localhost:7687")
NEO4J_USER: str = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD: str = os.getenv("NEO4J_PASSWORD", "testtest")


# ──────────────────────────────────────────
# 크롤러 설정 (naver_news.py)
# ──────────────────────────────────────────

# 기본 수집 기간 (일) — UI의 초기값
DEFAULT_DAYS_BACK: int = 1

# 페이지당 기준 일수 (N일 = 1페이지 = 100건)
# 예: 3이면 days_back=7 → ceil(7/3) = 3페이지
DAYS_BACK_PER_PAGE: int = 3

# 최대 수집 페이지 수 (상한: MAX_PAGES × 100건)
MAX_PAGES: int = 10

# 날짜별 최대 기사 수 (유사 필터링 후 초과 시 다운샘플)
MAX_ARTICLES_PER_DAY: int = 100

# 제목 유사도 임계값 (이 값 이상이면 중복 기사로 판별)
# 0.0 ~ 1.0 / 낮출수록 더 공격적으로 중복 제거
SIMILARITY_THRESHOLD: float = 0.5

# 수집 허용 언론사 도메인 화이트리스트
# 이 목록에 없는 도메인의 기사는 cluster_data 단계에서 제외됩니다.
ALLOWED_NEWS_DOMAINS: list = [
    "chosun.com",       # 조선일보
    "joongang.co.kr",   # 중앙일보
    "donga.com",        # 동아일보
    "hani.co.kr",       # 한겨레
    "khan.co.kr",       # 경향신문
    "mk.co.kr",         # 매일경제
    "hankyung.com",     # 한국경제
    "ytn.co.kr",        # YTN
    "yna.co.kr",        # 연합뉴스
    "etnews.com",       # 전자신문
    "zdnet.co.kr",      # 지디넷코리아
]


# ──────────────────────────────────────────
# 파이프라인 설정 (app.py)
# ──────────────────────────────────────────

# LLM 병렬 호출 수 (ThreadPoolExecutor max_workers)
# 높일수록 빠르지만 API Rate Limit 초과 위험
LLM_MAX_WORKERS: int = 5

# LLM에 한 번에 넘기는 기사 묶음 크기 (배치)
# 클수록 LLM 호출 횟수가 줄지만 컨텍스트 길이가 늘어남
BATCH_SIZE: int = int(os.getenv("BATCH_SIZE", 10))

# 엔티티 의미 기반 병합 사용 여부
ENABLE_ENTITY_SEMANTIC_MERGE: bool = os.getenv("ENABLE_ENTITY_SEMANTIC_MERGE", "1") == "1"

# 엔티티 의미 기반 병합 임계값
ENTITY_SEMANTIC_MERGE_THRESHOLD: float = float(os.getenv("ENTITY_SEMANTIC_MERGE_THRESHOLD", "0.9"))

# taxonomy 확장 사용 여부
ENABLE_TAXONOMY_ENRICHMENT: bool = os.getenv("ENABLE_TAXONOMY_ENRICHMENT", "1") == "1"

# 공통 seed taxonomy 경로
SEED_TAXONOMY_PATH: str = os.getenv(
    "SEED_TAXONOMY_PATH",
    os.path.join(BASE_DIR, "src", "configs", "entity_taxonomy.json"),
)

# 사용자 확장 taxonomy 경로
USER_TAXONOMY_PATH: str = os.getenv(
    "USER_TAXONOMY_PATH",
    os.path.join(BASE_DIR, "src", "configs", "entity_taxonomy.user.json"),
)

# ontology 후보 수집 사용 여부
ENABLE_ONTOLOGY_CANDIDATE_CAPTURE: bool = os.getenv("ENABLE_ONTOLOGY_CANDIDATE_CAPTURE", "1") == "1"

# taxonomy 미등록 엔티티 후보 레지스트리 경로
ONTOLOGY_CANDIDATE_REGISTRY_PATH: str = os.getenv(
    "ONTOLOGY_CANDIDATE_REGISTRY_PATH",
    os.path.join(BASE_DIR, "data", "ontology_candidates.json"),
)

# 반복 출현 엔티티에 대해서만 parent 후보를 추천
ONTOLOGY_PARENT_SUGGESTION_MIN_COUNT: int = int(os.getenv("ONTOLOGY_PARENT_SUGGESTION_MIN_COUNT", "3"))

# parent 후보 추천 시 최소 유사도
ONTOLOGY_PARENT_SUGGESTION_THRESHOLD: float = float(
    os.getenv("ONTOLOGY_PARENT_SUGGESTION_THRESHOLD", "0.78")
)


# ──────────────────────────────────────────
# 그래프 표시 설정 (app.py)
# ──────────────────────────────────────────

# Neo4j 그래프 조회 최대 엣지 수
GRAPH_QUERY_LIMIT: int = 500

# 검색어 기준 표시할 최대 홉(Hop) 깊이
# 3이면 검색어에서 3단계까지 연결된 노드를 표시
GRAPH_HOP_DEPTH: int = 3

# PageRank 상위 % 슬라이더 기본값 (1~100)
# 낮출수록 더 적은 핵심 노드만 표시
PAGERANK_DEFAULT_TOP: int = 50
