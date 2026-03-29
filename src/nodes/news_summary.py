from datetime import datetime, time
import os
import re
from typing import Dict, List

from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings

from src.configs.settings import EMBEDDING_MODEL, LLM_MODEL
from src.nodes.retriever import get_neo4j_driver


def _coerce_date_bounds(date_from, date_to):
    if hasattr(date_from, "year") and not isinstance(date_from, str):
        date_from = datetime.combine(date_from, time.min).isoformat()
    if hasattr(date_to, "year") and not isinstance(date_to, str):
        date_to = datetime.combine(date_to, time.max).isoformat()
    return date_from, date_to


def _build_date_filter(alias: str, date_from, date_to):
    clauses = []
    params = {}
    if date_from:
        clauses.append(f"{alias}.published_at >= datetime($date_from)")
        params["date_from"] = date_from
    if date_to:
        clauses.append(f"{alias}.published_at <= datetime($date_to)")
        params["date_to"] = date_to
    if not clauses:
        return "", params
    return " AND " + " AND ".join(clauses), params


def _format_actual_period(article_rows: List[Dict]) -> str:
    dates = []
    for row in article_rows:
        published_at = row.get("published_at")
        if not published_at:
            continue
        if hasattr(published_at, "to_native"):
            try:
                published_at = published_at.to_native()
            except Exception:
                pass
        if isinstance(published_at, datetime):
            dates.append(published_at.date())
            continue
        if hasattr(published_at, "date"):
            try:
                dates.append(published_at.date())
                continue
            except Exception:
                pass
        try:
            normalized = re.sub(r"(\.\d{6})\d+(?=[+-])", r"\1", str(published_at))
            dates.append(datetime.fromisoformat(normalized).date())
        except ValueError:
            continue

    if not dates:
        return "실제 기사 날짜 정보 없음"

    start_date = min(dates)
    end_date = max(dates)
    if start_date == end_date:
        return start_date.strftime("%Y-%m-%d")
    return f"{start_date:%Y-%m-%d} ~ {end_date:%Y-%m-%d}"


def summary_retriever_node(state: dict) -> dict:
    current_keyword = state.get("current_keyword", "")
    date_from, date_to = _coerce_date_bounds(state.get("date_from"), state.get("date_to"))

    if not current_keyword:
        return {"summary_context": "현재 키워드가 없어 요약을 생성할 수 없습니다.", "source_links": {}}

    driver = get_neo4j_driver()
    if not driver:
        return {"summary_context": "DB 연결에 실패했습니다.", "source_links": {}}

    embedder = GoogleGenerativeAIEmbeddings(
        model=EMBEDDING_MODEL,
        google_api_key=os.getenv("GOOGLE_API_KEY"),
    )
    query_vector = embedder.embed_query(f"{current_keyword} 주요 뉴스 핵심 이슈 기간 요약")

    article_params = {"current_keyword": current_keyword, "query_vector": query_vector}
    article_date_filter, date_params = _build_date_filter("scoped", date_from, date_to)
    article_params.update(date_params)

    entity_params = {"current_keyword": current_keyword}
    entity_date_filter, entity_date_params = _build_date_filter("a", date_from, date_to)
    entity_params.update(entity_date_params)

    article_query = f"""
    MATCH (k:Keyword {{name: $current_keyword}})-[:HAS_ARTICLE]->(scoped:NewsArticle)
    WHERE 1=1 {article_date_filter}
    WITH collect(scoped.id) AS scoped_ids
    CALL db.index.vector.queryNodes('article_embedding', 10, $query_vector)
    YIELD node, score
    WHERE node.id IN scoped_ids
    RETURN node.id AS url,
           coalesce(node.title, '제목 없음') AS title,
           coalesce(node.text, '') AS text,
           node.published_at AS published_at,
           score
    ORDER BY score DESC
    LIMIT 6
    """

    entity_query = f"""
    MATCH (k:Keyword {{name: $current_keyword}})-[:HAS_ARTICLE]->(a:NewsArticle)
    WHERE 1=1 {entity_date_filter}
    MATCH (a)-[:MENTIONS]->(e:Entity)
    RETURN e.id AS name,
           head([lbl IN labels(e) WHERE lbl <> 'Entity']) AS entity_type,
           count(*) AS mentions
    ORDER BY mentions DESC, name ASC
    LIMIT 8
    """

    try:
        with driver.session() as session:
            article_rows = [record.data() for record in session.run(article_query, **article_params)]
            entity_rows = [record.data() for record in session.run(entity_query, **entity_params)]
    except Exception as e:
        return {"summary_context": f"요약용 기사 조회 중 오류가 발생했습니다: {e}", "source_links": {}}
    finally:
        driver.close()

    if not article_rows:
        return {"summary_context": "현재 기간에 요약할 기사가 없습니다.", "source_links": {}}

    source_links: Dict[str, str] = {}
    article_blocks: List[str] = []
    for idx, article in enumerate(article_rows, start=1):
        article_id = f"[Article_{idx}]"
        source_links[article_id] = article.get("url", "")
        excerpt = article.get("text", "").strip()
        if len(excerpt) > 1200:
            excerpt = excerpt[:1200] + "..."
        article_blocks.append(
            f"{article_id}\n제목: {article.get('title', '제목 없음')}\n본문:\n{excerpt}"
        )

    entity_lines = [
        f"- {row.get('name', '알 수 없음')} ({row.get('entity_type') or 'Entity'}, 언급 {row.get('mentions', 0)}회)"
        for row in entity_rows
    ]

    period_text = _format_actual_period(article_rows)

    summary_context = (
        f"[요약 대상]\n키워드: {current_keyword}\n기간: {period_text}\n\n"
        f"[핵심 기사]\n" + "\n\n".join(article_blocks) + "\n\n"
        f"[기간 내 자주 언급된 엔티티]\n" + ("\n".join(entity_lines) if entity_lines else "- 없음")
    )

    return {"summary_context": summary_context, "source_links": source_links, "summary_period": period_text}


def summary_generator_node(state: dict) -> dict:
    current_keyword = state.get("current_keyword", "")
    summary_context = state.get("summary_context", "")
    source_links = state.get("source_links", {})
    summary_period = state.get("summary_period", "실제 기사 날짜 정보 없음")

    if not summary_context:
        return {"summary": "요약할 컨텍스트가 없습니다."}

    links_table = "\n".join([f"- {k}: {v}" for k, v in source_links.items()])
    llm = ChatGoogleGenerativeAI(model=LLM_MODEL, temperature=0.1)
    prompt = f"""
당신은 기간별 주요 뉴스를 빠르게 정리하는 요약 어시스턴트입니다.
반드시 제공된 기사 컨텍스트만 사용해서 답변하세요.

[키워드]
{current_keyword}

[요약에 실제 사용된 기사 날짜 범위]
{summary_period}

[기사 링크 매핑]
{links_table}

[컨텍스트]
{summary_context}

[작성 지침]
1. 첫 문단에서 해당 기간의 핵심 흐름을 2~3문장으로 요약하세요.
2. 그 아래에 '주요 뉴스' 섹션을 만들고 3~5개 bullet로 정리하세요.
3. 각 bullet에는 핵심 포인트와 함께 가능하면 기사 출처를 <a href="URL" target="_blank">[출처]</a> 형식으로 붙이세요.
4. 마지막에는 별도 체크포인트 섹션을 만들지 마세요.
5. 컨텍스트에 없는 내용은 추측하지 마세요.
"""

    try:
        response = llm.invoke(prompt)
        summary = response.content if hasattr(response, "content") else str(response)
    except Exception as e:
        summary = f"주요 뉴스 요약 생성 중 오류가 발생했습니다: {e}"

    return {"summary": summary}
