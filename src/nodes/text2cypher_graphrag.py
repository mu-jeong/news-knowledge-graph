from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List

from langchain_google_genai import ChatGoogleGenerativeAI
from neo4j import GraphDatabase
from neo4j_graphrag.llm import LLMInterface
from neo4j_graphrag.llm.types import LLMResponse
from neo4j_graphrag.retrievers import Text2CypherRetriever
from neo4j_graphrag.types import RetrieverResultItem

from src.configs.settings import LLM_MODEL, NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD

NEO4J_DATABASE = os.getenv("NEO4J_DATABASE") or None

_BLOCKED_KEYWORDS = [
    r"\bDELETE\b",
    r"\bDETACH\b",
    r"\bDROP\b",
    r"\bCREATE\s+INDEX\b",
    r"\bCREATE\s+CONSTRAINT\b",
    r"\bCREATE\s+VECTOR\s+INDEX\b",
    r"\bREMOVE\b",
    r"\bCALL\s+apoc\.",
    r"\bCALL\s+db\.schema",
    r"\bFOREACH\b",
]

_ALLOWED_START_KEYWORDS = [
    "MATCH", "OPTIONAL MATCH", "CALL db.index.vector", "WITH", "RETURN",
]

TEXT2CYPHER_SCHEMA = """
Node properties:
Keyword {name: STRING, last_updated: DATETIME, watermarks: STRING}
NewsArticle {id: STRING, url: STRING, title: STRING, text: STRING, keyword: STRING, published_at: DATETIME}
Entity {id: STRING, name: STRING}
Company {name: STRING}
Industry {name: STRING}
MacroEvent {name: STRING}
Product {name: STRING}
Technology {name: STRING}
RiskFactor {name: STRING}

Relationship properties:
HAS_ARTICLE {}
MENTIONS {}
SUPPLIES_TO {description: STRING, source_url: STRING, source_article: STRING, article_id: STRING, provenance: STRING}
COMPETES_WITH {description: STRING, source_url: STRING, source_article: STRING, article_id: STRING, provenance: STRING}
BELONGS_TO {description: STRING, source_url: STRING, source_article: STRING, article_id: STRING, provenance: STRING}
PART_OF {description: STRING, source_url: STRING, source_article: STRING, article_id: STRING, provenance: STRING}
RELEASED {description: STRING, source_url: STRING, source_article: STRING, article_id: STRING, provenance: STRING}
USES {description: STRING, source_url: STRING, source_article: STRING, article_id: STRING, provenance: STRING}
EXPOSED_TO {description: STRING, source_url: STRING, source_article: STRING, article_id: STRING, provenance: STRING}
BENEFITS_FROM {description: STRING, source_url: STRING, source_article: STRING, article_id: STRING, provenance: STRING}
AFFECTS {description: STRING, source_url: STRING, source_article: STRING, article_id: STRING, provenance: STRING}
OWNS {description: STRING, source_url: STRING, source_article: STRING, article_id: STRING, provenance: STRING}
RELATED_TO {description: STRING, source_url: STRING, source_article: STRING, article_id: STRING, provenance: STRING}

The relationships:
(:Keyword)-[:HAS_ARTICLE]->(:NewsArticle)
(:NewsArticle)-[:MENTIONS]->(:Entity)
(:Company)-[:SUPPLIES_TO]->(:Company)
(:Company)-[:COMPETES_WITH]->(:Company)
(:Company)-[:BELONGS_TO]->(:Industry)
(:Entity)-[:PART_OF]->(:Entity)
(:Company)-[:RELEASED]->(:Product)
(:Company)-[:USES]->(:Technology)
(:Product)-[:USES]->(:Technology)
(:Company)-[:EXPOSED_TO]->(:RiskFactor)
(:Company)-[:EXPOSED_TO]->(:MacroEvent)
(:Product)-[:EXPOSED_TO]->(:RiskFactor)
(:Industry)-[:EXPOSED_TO]->(:MacroEvent)
(:Company)-[:BENEFITS_FROM]->(:MacroEvent)
(:Product)-[:BENEFITS_FROM]->(:Technology)
(:MacroEvent)-[:AFFECTS]->(:Entity)
(:RiskFactor)-[:AFFECTS]->(:Entity)
(:Technology)-[:AFFECTS]->(:Entity)
(:Entity)-[:OWNS]->(:Entity)
(:Entity)-[:RELATED_TO]->(:Entity)
""".strip()

TEXT2CYPHER_EXAMPLES = [
    "USER INPUT: '삼성전자와 경쟁하는 기업들은 누구야?' QUERY: MATCH (k:Keyword {name: $current_keyword})-[:HAS_ARTICLE]->(a:NewsArticle)-[:MENTIONS]->(:Entity)-[r:COMPETES_WITH]->(competitor:Entity) RETURN DISTINCT competitor.name AS competitor, r.source_url AS source_url, r.article_id AS article_id ORDER BY competitor",
    "USER INPUT: '하이닉스가 어떤 리스크에 노출돼 있어?' QUERY: MATCH (k:Keyword {name: $current_keyword})-[:HAS_ARTICLE]->(a:NewsArticle)-[:MENTIONS]->(company:Entity {name: 'SK하이닉스'})-[r:EXPOSED_TO]->(risk:Entity) RETURN DISTINCT company.name AS company, risk.name AS risk, r.description AS description, r.source_url AS source_url, r.article_id AS article_id ORDER BY risk",
    "USER INPUT: '삼성전자 관련 기사에서 언급된 기술은 뭐야?' QUERY: MATCH (k:Keyword {name: $current_keyword})-[:HAS_ARTICLE]->(a:NewsArticle)-[:MENTIONS]->(company:Entity {name: '삼성전자'}) MATCH (a)-[:MENTIONS]->(tech:Technology) RETURN DISTINCT tech.name AS technology, a.id AS article_url, a.title AS article_title ORDER BY technology",
]

TEXT2CYPHER_PROMPT = """
You are a Neo4j Cypher expert for a Korean business news knowledge graph.
Generate exactly one read-only Cypher query for the user's request.
Return only Cypher inside a single ```cypher``` block.

Schema:
{schema}

Examples:
{examples}

Rules:
1. You must scope the query to the current keyword with MATCH (k:Keyword {{name: $current_keyword}})-[:HAS_ARTICLE]->(a:NewsArticle) or an equivalent pattern that keeps every result inside that keyword scope.
2. Use the parameter $current_keyword exactly as written. Never inline the keyword value.
3. Only produce read-only Cypher. Never use CREATE, MERGE, DELETE, SET, REMOVE, CALL apoc, or schema-changing commands.
4. Prefer explicit managed relationships such as SUPPLIES_TO, COMPETES_WITH, EXPOSED_TO, BENEFITS_FROM, AFFECTS, OWNS, and RELATED_TO only when no better relationship fits.
5. When returning relationship facts, include source_url and article_id when available so the answer can cite evidence.
6. If article evidence is needed, use NewsArticle nodes connected through HAS_ARTICLE and MENTIONS.
7. Keep the query concise and syntactically valid for Neo4j.

Current keyword scope: {current_keyword}
User question: {query_text}
""".strip()


class GeminiGraphRAGLLM(LLMInterface):
    """Adapter so neo4j_graphrag can use the project's Gemini chat model."""

    def __init__(self, model_name: str, temperature: float = 0.0):
        super().__init__(model_name=model_name, model_params={"temperature": temperature})
        self._llm = ChatGoogleGenerativeAI(
            model=model_name,
            temperature=temperature,
            google_api_key=os.getenv("GOOGLE_API_KEY"),
        )

    def invoke(self, input: str, message_history=None, system_instruction=None) -> LLMResponse:
        prompt_parts = []
        if system_instruction:
            prompt_parts.append(system_instruction)
        if message_history:
            prompt_parts.append(str(message_history))
        prompt_parts.append(input)
        response = self._llm.invoke("\n\n".join(part for part in prompt_parts if part))
        content = response.content if hasattr(response, "content") else str(response)
        return LLMResponse(content=content)

    async def ainvoke(self, input: str, message_history=None, system_instruction=None) -> LLMResponse:
        return self.invoke(input=input, message_history=message_history, system_instruction=system_instruction)


class Text2CypherValidationError(Exception):
    def __init__(self, query: str, reason: str):
        super().__init__(reason)
        self.query = query
        self.reason = reason


def _normalize_cypher(query: str) -> str:
    normalized = query.strip()
    normalized = re.sub(r"^\s*```(?:cypher)?\s*", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s*```\s*$", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"^\s*cypher\s*\n", "", normalized, flags=re.IGNORECASE)
    return normalized.strip()


def _is_read_only(query: str) -> tuple[bool, str]:
    normalized_query = _normalize_cypher(query)
    upper_q = normalized_query.upper()

    for pattern in _BLOCKED_KEYWORDS:
        if re.search(pattern, normalized_query, re.IGNORECASE):
            matched = re.search(pattern, normalized_query, re.IGNORECASE).group()
            return False, f"차단된 명령어 감지: `{matched}` — 읽기 전용 쿼리만 허용합니다."

    starts_ok = any(upper_q.startswith(kw.upper()) for kw in _ALLOWED_START_KEYWORDS)
    if not starts_ok:
        return False, "허용되지 않은 쿼리 시작 형태입니다. MATCH 또는 CALL db.index.vector로 시작해야 합니다."

    return True, ""


def _check_syntax(query: str, current_keyword: str = "") -> tuple[bool, str]:
    try:
        normalized_query = _normalize_cypher(query)
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        with driver.session() as session:
            params = {"current_keyword": current_keyword} if current_keyword else {}
            session.run(f"EXPLAIN {normalized_query}", **params)
        driver.close()
        return True, ""
    except Exception as e:
        return False, f"Cypher 문법 오류: {str(e)[:200]}"


class ValidatingNeo4jDriverProxy:
    """Intercept execute_query so the official retriever can be used unchanged."""

    def __init__(self, driver: Any):
        self._driver = driver
        self._runtime_parameters: Dict[str, Any] = {}
        self.last_query: str = ""

    def set_runtime_parameters(self, parameters: Dict[str, Any] | None = None) -> None:
        self._runtime_parameters = dict(parameters or {})

    def execute_query(self, query_: str, **kwargs: Any):
        normalized_query = _normalize_cypher(query_)
        self.last_query = normalized_query
        current_keyword = self._runtime_parameters.get("current_keyword", "")

        is_safe, reason = _is_read_only(normalized_query)
        if not is_safe:
            raise Text2CypherValidationError(normalized_query, reason)

        if current_keyword and "$current_keyword" not in normalized_query:
            raise Text2CypherValidationError(
                normalized_query,
                "현재 검색어 범위 제한이 없는 Cypher입니다. `$current_keyword`를 사용해야 합니다.",
            )

        is_valid_syntax, err_msg = _check_syntax(normalized_query, current_keyword=current_keyword)
        if not is_valid_syntax:
            raise Text2CypherValidationError(normalized_query, err_msg)

        forwarded_kwargs = dict(kwargs)
        forwarded_kwargs["parameters_"] = self._runtime_parameters
        return self._driver.execute_query(query_=normalized_query, **forwarded_kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._driver, name)


def _text2cypher_result_formatter(record: Any) -> RetrieverResultItem:
    return RetrieverResultItem(content=record.data())


def build_text2cypher_retriever(driver):
    retriever = Text2CypherRetriever(
        driver=driver,
        llm=GeminiGraphRAGLLM(model_name=LLM_MODEL, temperature=0.0),
        neo4j_database=NEO4J_DATABASE,
        neo4j_schema=TEXT2CYPHER_SCHEMA,
        examples=TEXT2CYPHER_EXAMPLES,
        custom_prompt=TEXT2CYPHER_PROMPT,
        result_formatter=_text2cypher_result_formatter,
    )
    validating_driver = ValidatingNeo4jDriverProxy(driver)
    retriever.driver = validating_driver
    return retriever, validating_driver


def serialize_cypher_result(data: List[Dict[str, Any]]) -> str:
    def _json_serializable(obj):
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        return str(obj)

    return json.dumps(data, ensure_ascii=False, default=_json_serializable)[:4000]
