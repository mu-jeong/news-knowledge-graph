"""
cypher_validator.py
===================
LLM이 생성한 Cypher 쿼리를 실행 전에 검증하는 노드입니다.

보안 목표:
1. Cypher Injection 방지: DELETE, DROP, DETACH, SET (벌크) 등 파괴적 쿼리 차단
2. 구문 오류 사전 탐지: 실행 전 EXPLAIN으로 유효성 검사
3. 피드백 루프: 최대 3회 재시도 후 실패 시 generator 없이 종료
"""
import re
from neo4j import GraphDatabase
from src.graphs.state import AgentState
from src.configs.settings import NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD

# ──────────────────────────────────────────
# 1. 허용/차단 키워드 화이트리스트/블랙리스트
# ──────────────────────────────────────────

# 절대로 실행해선 안 되는 명령어 (파괴/변조 가능)
_BLOCKED_KEYWORDS = [
    r"\bDELETE\b",
    r"\bDETACH\b",
    r"\bDROP\b",
    r"\bCREATE\s+INDEX\b",
    r"\bCREATE\s+CONSTRAINT\b",
    r"\bCREATE\s+VECTOR\s+INDEX\b",
    r"\bREMOVE\b",
    r"\bCALL\s+apoc\.",     # apoc 프로시저 (위험 가능성)
    r"\bCALL\s+db\.schema", # 스키마 노출
    r"\bFOREACH\b",         # 대량 변조 루프
]

# 허용되는 조회용 진입점 키워드
_ALLOWED_START_KEYWORDS = [
    "MATCH", "OPTIONAL MATCH", "CALL db.index.vector", "WITH", "RETURN",
]

MAX_RETRIES = 3

def _is_read_only(query: str) -> tuple[bool, str]:
    """
    쿼리가 읽기 전용(Read-only)인지 검사합니다.
    Returns: (is_valid, reason)
    """
    upper_q = query.upper().strip()

    # 1. 차단 키워드 검사
    for pattern in _BLOCKED_KEYWORDS:
        if re.search(pattern, query, re.IGNORECASE):
            matched = re.search(pattern, query, re.IGNORECASE).group()
            return False, f"차단된 명령어 감지: `{matched}` — 읽기 전용 쿼리만 허용합니다."

    # 2. 쿼리가 허용된 키워드로 시작하는지 검사
    starts_ok = any(upper_q.startswith(kw.upper()) for kw in _ALLOWED_START_KEYWORDS)
    if not starts_ok:
        return False, f"허용되지 않은 쿼리 시작 형태입니다. MATCH 또는 CALL db.index.vector로 시작해야 합니다."

    return True, ""


def _check_syntax(query: str) -> tuple[bool, str]:
    """
    Neo4j EXPLAIN을 활용해 문법 오류를 사전 탐지합니다.
    실제 데이터를 읽지 않으므로 안전합니다.
    """
    try:
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        with driver.session() as session:
            session.run(f"EXPLAIN {query}")
        driver.close()
        return True, ""
    except Exception as e:
        return False, f"Cypher 문법 오류: {str(e)[:200]}"


def cypher_validator_node(state: AgentState) -> dict:
    """
    생성된 Cypher 쿼리를 검증합니다.
    - 유효: 그대로 통과 (final_answer는 None 유지)
    - 무효 + retry < 3: retry_count 증가, generated_cypher 초기화 (재시도 유도)
    - 무효 + retry >= 3: final_answer에 에러 메시지를 담아 generator를 건너뜁니다
    """
    cypher = state.get("generated_cypher", "").strip()
    retry_count = state.get("retry_count", 0)

    # --- 검증 1: 읽기 전용 여부 ---
    is_safe, reason = _is_read_only(cypher)
    if not is_safe:
        print(f"🚫 [CypherValidator] 보안 위반 감지 ({retry_count+1}회): {reason}")
        return _handle_failure(reason, retry_count, cypher)

    # --- 검증 2: Neo4j 문법 오류 ---
    is_valid_syntax, err_msg = _check_syntax(cypher)
    if not is_valid_syntax:
        print(f"❌ [CypherValidator] 문법 오류 ({retry_count+1}회): {err_msg}")
        return _handle_failure(err_msg, retry_count, cypher)

    # --- 검증 통과 ---
    print(f"✅ [CypherValidator] 쿼리 검증 통과")
    return {
        "retry_count": retry_count,  # 유지
        "final_answer": None,        # 에러 없음
    }


def _handle_failure(reason: str, retry_count: int, cypher: str) -> dict:
    """검증 실패 처리: 재시도 또는 최종 에러 반환"""
    new_retry_count = retry_count + 1

    if new_retry_count < MAX_RETRIES:
        # 재시도: generated_cypher를 비워서 text2cypher가 다시 생성하도록 유도
        return {
            "retry_count": new_retry_count,
            "generated_cypher": "",   # 리셋 → text2cypher 재시도
            "cypher_result": [],
            "search_context": "",
            "final_answer": None,     # 아직 에러 확정 아님
        }
    else:
        # 3회 초과: generator 없이 즉시 종료
        error_message = (
            f"⚠️ Cypher 쿼리 검증이 {MAX_RETRIES}회 연속 실패하여 요청을 처리할 수 없습니다.\n\n"
            f"**마지막 실패 사유:** {reason}\n\n"
            f"**생성된 쿼리:**\n```cypher\n{cypher}\n```\n\n"
            "질문을 다르게 표현하거나 다른 검색 방식을 시도해 보세요."
        )
        return {
            "retry_count": new_retry_count,
            "final_answer": error_message,  # 이 값이 있으면 generator를 건너뜀
        }
