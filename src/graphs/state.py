import operator
from typing import Annotated, TypedDict, List, Dict, Any, Optional

class AgentState(TypedDict):
    question: str
    route: str                 # 'vector', 'text2cypher', 'vector_cypher'
    
    # Text2Cypher 및 VectorCypher에서 사용될 필드
    extracted_entities: List[str]  # 질문에서 추출된 핵심 엔티티 명
    generated_cypher: str          # LLM이 작성한 Cypher 쿼리
    cypher_result: List[Dict]      # DB에서 조회된 관계 정보 리스트
    
    # Vector 및 VectorCypher에서 사용될 본문 텍스트 (FAISS 또는 Neo4j NewsBatch)
    search_context: str            # 최종적으로 생성기(Generator)에 넘길 참조 컨텍스트 모음
    source_links: Dict[str, str]   # 기사 ID([Article_N])와 URL 매핑 테이블 (정밀 출처용)
    
    generation: str            # 최종 답변
    chat_history: Annotated[list, operator.add]  # 대화 기록

    # Cypher 검증 피드백 루프용 필드
    retry_count: int           # text2cypher 재시도 횟수 (최대 3회)
    final_answer: Optional[str]  # 검증 실패 시 generator 없이 직접 반환할 에러 메시지
