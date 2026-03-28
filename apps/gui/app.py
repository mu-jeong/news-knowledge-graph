import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

import json
import collections
import networkx as nx
from concurrent.futures import ThreadPoolExecutor, as_completed
import streamlit as st
from pyvis.network import Network
import streamlit.components.v1 as components
from dotenv import load_dotenv
from src.configs.settings import (
    LLM_MODEL, LLM_MAX_WORKERS, BATCH_SIZE,
    DEFAULT_DAYS_BACK, GRAPH_QUERY_LIMIT, GRAPH_HOP_DEPTH,
    PAGERANK_DEFAULT_TOP,
)

from src.graphs.neo4j_manager import Neo4jLoader

load_dotenv()

st.set_page_config(layout="wide", page_title="News Graph Dashboard", page_icon="🕸️")




# ─────────────────────────────
# 파이프라인: 검색어 → 뉴스 수집 → LLM 추출 → Neo4j 적재
# ─────────────────────────────
def run_pipeline(keyword: str, days_back: int = 7):
    """검색어로 뉴스를 수집하고 그래프 DB에 증분(Incremental) 업데이트합니다."""
    from src.core.crawlers.naver_news import NaverNewsProvider
    from src.configs.schema import GraphData, get_graph_extraction_prompt
    from src.core.utils.entity_resolution import EntityResolver
    from src.graphs.neo4j_manager import Neo4jLoader
    from langchain_google_genai import ChatGoogleGenerativeAI

    log = []

    # 0. 마지막 처리 기사 날짜 조회 (증분 업데이트 기준점)
    log.append(f"🔍 **[0/4] 증분 기준 조회 중...** `{keyword}`의 과거 날짜별 수집 이력(Watermarks)을 확인합니다.")
    yield "\n".join(log)
    watermarks = {}
    try:
        from datetime import datetime, timedelta
        _loader_check = Neo4jLoader()
        watermarks = _loader_check.get_keyword_watermarks(keyword)
        _loader_check.close()
        
        if watermarks:
            log.append(f"  → 📅 DB에 기존 데이터가 확인되었습니다. 부분 수집된 날짜를 고려하여 신규 기사만 정밀 추출합니다.")
        else:
            log.append(f"  → 🆕 처음 검색하는 키워드 — 요청하신 **최근 {days_back}일치 전체**를 수집합니다.")
        
    except Exception as _e:
        watermarks = {}
        log.append(f"  → ⚠️ 수집 이력 조회 실패 (전체 수집 진행): {_e}")

    yield "\n".join(log)

    # 1. 뉴스 수집 (since_date 이후 기사만)
    log.append(f"\n📡 **[1/4] 뉴스 수집 중...** `{keyword}`")
    yield "\n".join(log)
    try:
        provider = NaverNewsProvider()
        # 1. API를 통해 해당 범위의 기사들을 일단 모두 가져옴 (fetch_data) - watermark 적용
        raw_articles = provider.fetch_data(keyword=keyword, days_back=days_back, watermarks=watermarks)
        article_metadata = provider.get_article_metadata(raw_articles)
        
        # ───────── 지능형 DB 필터링 (Gap-free Incremental) ──────────
        loader = Neo4jLoader()
        all_urls = [a["url"] for a in article_metadata if a.get("url")]
        # DB에 없는 URL만 필터링
        new_urls = set(loader.filter_new_urls(all_urls))
        loader.close()
        
        # 신규 기사만 필터링
        filtered_article_metadata = [a for a in article_metadata if a["url"] in new_urls]
        
        if not filtered_article_metadata:
            # 케이스 1: API에서 기사는 가져왔거나(all_urls), 워터마크에 의해 모두 필터링된 경우(watermarks)
            if all_urls or watermarks:
                log.append(f"  → ✨ **이미 최신 지식이 반영되어 있습니다.** (금일 요청 범위에서 새로 발행된 기사가 없습니다.)")
                log.append("  💡 새로운 뉴스가 나올 때까지 기다리거나, 더 넓은 기간으로 검색해 보세요.")
            # 케이스 2: API 결과 자체가 없고 기존 수집 이력도 없는 경우
            else:
                log.append("  → ❌ **수집된 기사가 전혀 없습니다.** (키워드 오타, 네이버 서버 일시 오류, 혹은 수집 허용 언론사에 해당 뉴스가 없을 수 있음)")
            
            yield "\n".join(log)
            return
             
        # 새로운 기사가 있다면 배치를 재생성 (원래 batches는 raw_articles 기준이므로 걸러야 함)
        # ※ 성능을 위해 raw_articles 중 신규인 것들만 다시 클러스터링
        filtered_raw = [a for a in raw_articles if (a.get('originallink') or a.get('link', '')) in new_urls]
        batches = provider.cluster_data(filtered_raw, batch_size=BATCH_SIZE)
        article_metadata = filtered_article_metadata
        # ─────────────────────────────────────────────────────────────

    except Exception as e:
        log.append(f"  → ⚠️ 수집 중 예외 발생: {e}")
        yield "\n".join(log)
        return

    log.append(f"  → 📡 {len(all_urls)}개 기사 중 **신규 {len(article_metadata)}개** 발견 (나머지 중복은 제외)")
    log.append(f"\n🤖 **[2/4] LLM 병렬 추출 시작...** ({len(batches)}개 배치 동시 요청)")
    yield "\n".join(log)

    # 2. LLM 병렬 호출 (ThreadPoolExecutor)
    google_api_key = os.getenv("GOOGLE_API_KEY")
    resolver = EntityResolver()
    all_resolved = []

    def _extract_batch(idx_batch):
        idx, batch_text = idx_batch
        llm = ChatGoogleGenerativeAI(model=LLM_MODEL, api_key=google_api_key)
        structured_llm = llm.with_structured_output(GraphData)
        prompt = get_graph_extraction_prompt(batch_text)
        result: GraphData = structured_llm.invoke(prompt)
        resolved = resolver.resolve(result)
        return idx, resolved, len(result.entities), len(result.relations), batch_text

    with ThreadPoolExecutor(max_workers=LLM_MAX_WORKERS) as executor:
        futures = {executor.submit(_extract_batch, (i, batch_text)): i for i, batch_text in enumerate(batches)}
        for future in as_completed(futures):
            try:
                idx, resolved, n_ent, n_rel, batch_text = future.result()
                all_resolved.append((resolved, batch_text))
                log.append(f"  → 배치 [{idx+1}/{len(batches)}]: 엔티티 {n_ent}개, 관계 {n_rel}개")
            except Exception as e:
                log.append(f"  ⚠️ 배치 실패: {e}")
            yield "\n".join(log)

    total_rels = sum(len(g.relations) for g, _ in all_resolved)
    log.append(f"\n🔗 **[3/4] 엔티티 정규화 완료** → 총 {total_rels}개 관계 정규화")
    yield "\n".join(log)

    # 3. Neo4j 누적 적재 (MERGE 방식 — 기존 데이터 유지)
    log.append(f"\n🗄️ **[4/4] Neo4j 누적 적재 중...**")
    yield "\n".join(log)

    try:
        loader = Neo4jLoader()
        if loader.driver:
            # 3-0. 벡터 인덱스 먼저 생성
            loader.create_vector_index()
            # 3-1. 엔티티/관계 및 기사 본문 임베딩 누적 적재
            for g, batch_text in all_resolved:
                loader.load_graph_data(g, batch_text=batch_text)
            # 3-2. Article 노드 저장 + Keyword 노드 갱신
            loader.upsert_articles(keyword, article_metadata)
            
            new_wms = {}
            today_str = datetime.now().strftime("%Y-%m-%d")
            for art in article_metadata:
                dt = art["published_at"]
                d_str = dt.strftime("%Y-%m-%d")
                t_str = dt.strftime("%Y-%m-%dT%H:%M:%S")
                
                # 오늘 기사라면 현재 시각까지 완료된 것으로 간주
                if d_str == today_str:
                    t_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                
                # 해당 날짜의 기존값보다 더 나중 관측 시점이면 갱신 후보
                if d_str not in new_wms or t_str > new_wms[d_str]:
                    new_wms[d_str] = t_str
            
            if new_wms:
                loader.update_keyword_watermarks(keyword, new_wms)
            
            loader.close()
            log.append(f"  → ✅ 적재 및 수집 워터마크 기록 완료! (기사 {len(article_metadata)}개 누적)")
        else:
            log.append("  → ❌ Neo4j 연결 실패")
    except Exception as e:
        import traceback
        error_msg = f"  → ❌ 적재 오류: {e}\n\n```\n{traceback.format_exc()}\n```"
        log.append(error_msg)
        yield "\n".join(log)
        return False # 실패 반환

    log.append(f"\n🎉 **파이프라인 완료!** 아래 그래프가 자동으로 갱신됩니다.")
    yield "\n".join(log)
    return True # 성공 반환



# ─────────────────────────────
# 그래프 데이터 조회
# ─────────────────────────────
def fetch_graph_data(keyword: str = "", date_from=None, date_to=None):
    """
    keyword가 주어지면 해당 키워드의 기사 URL을 기준으로
    source_url이 일치하는 관계(Edge)만 반환합니다.
    date_from / date_to(날짜 객체)가 주어지면 해당 범위의 기사만 조회합니다.
    """
    if not keyword:
        return [], {}
    try:
        from src.graphs.neo4j_manager import Neo4jLoader
        from datetime import datetime as _dt, time as _time
        loader = Neo4jLoader()
        if not loader.driver:
            return [], {}

        # date 객체 → Neo4j-호환 날짜문자열
        dt_from = _dt.combine(date_from, _time.min).isoformat() if date_from else None
        dt_to   = _dt.combine(date_to,   _time.max).isoformat() if date_to   else None

        # 날짜 필터 WHERE 조건 동적 생성
        date_filter = ""
        if dt_from:
            date_filter += "  AND a.published_at >= datetime($dt_from)\n"
        if dt_to:
            date_filter += "  AND a.published_at <= datetime($dt_to)\n"

        with loader.driver.session() as session:
            params = {"keyword": keyword, "dt_from": dt_from, "dt_to": dt_to}
            # 키워드->기사 관계와 엔티티 간 관계를 결합하여 조회
            query = f"""
                // 1. 엔티티 관계 조회
                MATCH (k:Keyword {{name: $keyword}})-[:HAS_ARTICLE]->(a:NewsArticle)
                WHERE 1=1
{date_filter}                WITH collect(distinct a.id) AS article_ids, collect(distinct a.title) AS article_titles
                
                MATCH (n:Entity)-[r]->(m:Entity)
                WHERE r.source_url IN article_ids 
                   OR r.source_article IN article_titles
                
                RETURN n.id AS source, 
                       head([lbl IN labels(n) WHERE lbl <> 'Entity']) AS source_type,
                       m.id AS target, 
                       head([lbl IN labels(m) WHERE lbl <> 'Entity']) AS target_type,
                       type(r) AS edge_type,
                       r.source_url AS source_url,
                       r.description AS description,
                       COALESCE(r.source_article, '정보 없음') AS source_article
                
                UNION
                
                // 2. 검색어와 뉴스 기사의 관계 추가 (보라색 노드 시각화용)
                MATCH (k:Keyword {{name: $keyword}})-[r:HAS_ARTICLE]->(a:NewsArticle)
                WHERE 1=1
{date_filter}                RETURN k.name AS source, 'Keyword' AS source_type,
                       a.id AS target, 'NewsArticle' AS target_type,
                       type(r) AS edge_type,
                       '' AS source_url, '' AS description, a.title AS source_article
            """
            records = [r.data() for r in session.run(query, **params)]

            
            centrality = {r["node_id"]: r["degree"] for r in session.run(f"""
                MATCH (k:Keyword {{name: $keyword}})-[:HAS_ARTICLE]->(a:NewsArticle)
                WHERE 1=1
{date_filter}                WITH collect(distinct a.id) AS article_ids, collect(distinct a.title) AS article_titles
                
                MATCH (n:Entity)-[r]-(m:Entity)
                WHERE r.source_url IN article_ids 
                   OR r.source_article IN article_titles
                RETURN n.id AS node_id, count(r) AS degree
            """, **params)}
        loader.close()
        return records, centrality
    except Exception as e:
        return [], {}


def get_connected(keyword, all_edges, max_hop=3):
    all_node_ids = {n for e in all_edges for n in (e["source"], e["target"])}
    if not keyword or keyword not in all_node_ids:
        return None
    adj = collections.defaultdict(list)
    for e in all_edges:
        src, tgt = e.get("source"), e.get("target")
        if src and tgt:
            adj[src].append(tgt)
            adj[tgt].append(src)
    visited = {keyword: 0}
    queue = collections.deque([(keyword, 0)])
    while queue:
        curr, depth = queue.popleft()
        if depth < max_hop:
            for nb in adj[curr]:
                if nb not in visited:
                    visited[nb] = depth + 1
                    queue.append((nb, depth + 1))
    return set(visited.keys())


# ─────────────────────────────
# UI
# ─────────────────────────────
st.title("🌐 News Graph Dashboard")


# ─────────────────────────────
# 사이드바: 검색 + 파이프라인 실행
# ─────────────────────────────
with st.sidebar:
    st.header("🔍 검색")
    search_input = st.text_input(
        "검색어",
        placeholder="예: 삼성전자, SK하이닉스, NVIDIA",
        label_visibility="collapsed",
    )
    days_input = st.number_input("수집 기간 (일)", min_value=1, max_value=100, value=DEFAULT_DAYS_BACK)
    run_btn = st.button("🚀 검색 & 그래프 생성", type="primary", use_container_width=True)

    st.divider()
    st.subheader("📅 기사 기간 필터")
    from datetime import date as _date, timedelta as _timedelta
    _today = _date.today()
    _default_from = _today - _timedelta(days=100)
    date_range = st.date_input(
        "date_range_filter",
        value=(_default_from, _today),
        min_value=_date(2020, 1, 1),
        max_value=_today,
        label_visibility="collapsed",
        format="YYYY-MM-DD",
    )
    # 단일 날짜 선택 중인 경우를 대비
    if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
        view_date_from, view_date_to = date_range[0], date_range[1]
    else:
        view_date_from, view_date_to = None, None

    st.divider()
    st.markdown("""
**범례 (엔티티 구조별 색상)**

**1. 뼈대 (메타데이터)**
- 🟣 **보라색**: 검색 키워드 기준 (Keyword)
- 🟢 **청록색**: 뉴스 기둥 (NewsArticle)

**2. 비즈니스 코어 (주요 분석 대상)**
- 🔴 **빨간색**: 기업 (Company)
- 🔵 **파란색**: 산업 생태계 (Industry)
- 🟠 **주황색**: 기업의 제품/서비스 (Product)

**3. 외부 환경 및 기타**
- 🟡 **노란색(Gold)**: 거시경제/외부 충격 (MacroEvent)
- ⚪ **회색**: 미분류 일반 명사 (Entity)

---
**관계(엣지) 연결 형태**
- **🔗 실선 (파란)**: 기사 출처가 명확함 — 클릭 시 원문 뉴스로 이동
- **➖ 점선 (회색)**: 기사 출처 없음 / 일반 엔티티 간 구조적 관계
""")

    st.divider()
    st.subheader("🔧 필터")
    pagerank_top = st.slider(
        "PageRank 상위 (%)",
        min_value=10, max_value=100, value=PAGERANK_DEFAULT_TOP, step=10,
        help="PageRank 점수 기준 상위 N%의 노드만 표시합니다."
    )
    st.markdown("**관계 유형** (검색 후 선택 가능)")
    _et_placeholder = st.empty()
    st.markdown("**노드 유형**")
    all_node_type_options = ["Keyword", "Company", "Industry", "Product", "MacroEvent", "NewsArticle", "Entity"]
    # NewsArticle은 기본적으로 제외 (사용자 요청)
    default_node_types = [t for t in all_node_type_options if t != "NewsArticle"]
    selected_node_types = st.multiselect(
        "node_type_filter",
        options=all_node_type_options,
        default=default_node_types,
        label_visibility="collapsed",
    )

    st.divider()
    if st.button("🗑️ 데이터베이스 초기화", use_container_width=True, help="Neo4j의 모든 데이터를 삭제하고 처음부터 다시 시작합니다."):
        try:
            import importlib
            import src.graphs.neo4j_manager
            importlib.reload(src.graphs.neo4j_manager)
            from src.graphs.neo4j_manager import Neo4jLoader
            loader = Neo4jLoader()
            if loader.driver:
                loader.clear_database()  # 이전에 추가한 clear_database 메서드 호출
                loader.close()
                st.success("✅ DB가 초기화되었습니다. 이제 검색 시 전체 데이터를 수집합니다.")
                st.rerun()
            else:
                st.error("Neo4j 드라이버 연결에 실패했습니다.")
        except Exception as e:
            st.error(f"초기화 중 오류 발생: {e}")

# 파이프라인 실행
if run_btn and search_input.strip():
    keyword = search_input.strip()

    st.info(f"**'{keyword}'** 뉴스를 수집하고 그래프를 구축합니다.")
    log_area = st.empty()

    with st.spinner("파이프라인 실행 중..."):
        pipeline_success = False
        for log_msg in run_pipeline(keyword, days_back=int(days_input)):
            log_area.markdown(log_msg)
            # 마지막 yield 값이 True/False라면 성공 여부 판단
            if log_msg is True: pipeline_success = True
            if log_msg is False: pipeline_success = False

    if pipeline_success:
        st.success("✅ 완료! 그래프가 아래에 표시됩니다.")
        st.rerun()

elif run_btn and not search_input.strip():
    st.sidebar.warning("검색어를 입력해 주세요.")


# ─────────────────────────────
# 메인 레이아웃: 수직 통합 배치 (위: 그래프, 아래: 채팅)
# ─────────────────────────────
st.header("🕸️ 지식 그래프")
records, centrality = fetch_graph_data(
    keyword=search_input.strip() if search_input else "",
    date_from=view_date_from,
    date_to=view_date_to,
)

if not records:
    st.markdown("---")
    st.markdown(
        """
        <div style="text-align:center; padding: 60px 20px;">
            <h2>🕸️ 아직 그래프 데이터가 없습니다</h2>
            <p style="font-size:1.1rem; color:#888;">
                왼쪽 사이드바에서 키워드를 입력하고<br>
                <b>🚀 검색 &amp; 그래프 생성</b> 버튼을 눌러주세요.
            </p>
            <p style="font-size:0.9rem; color:#aaa;">
                Neo4j가 실행 중인지, .env의 API 키가 올바른지 확인해주세요.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.stop()

all_edges = records
node_types = {}
for rec in all_edges:
    node_types[rec["source"]] = rec.get("source_type")
    node_types[rec["target"]] = rec.get("target_type")


# ① 엣지 타입 필터 (placeholder를 실제 multiselect로 교체)
all_edge_type_options = sorted({e.get("edge_type") for e in all_edges if e.get("edge_type")})
with st.sidebar:
    with _et_placeholder.container():
        selected_edge_types = st.multiselect(
            "edge_type_filter",
            options=all_edge_type_options,
            default=all_edge_type_options,
            label_visibility="collapsed",
        )

if selected_edge_types:
    all_edges = [e for e in all_edges if e.get("edge_type") in selected_edge_types]

# ② 노드 타입 필터
if selected_node_types:
    all_edges = [
        e for e in all_edges
        if node_types.get(e["source"]) in selected_node_types
        and node_types.get(e["target"]) in selected_node_types
    ]

# ③ PageRank 상위 N% 필터
if all_edges and pagerank_top < 100:
    G = nx.DiGraph()
    for e in all_edges:
        # source나 target이 None이면 NetworkX에서 에러 발생하므로 스킵
        if e.get("source") and e.get("target"):
            G.add_edge(e["source"], e["target"])
    pr = nx.pagerank(G, alpha=0.85)
    cutoff_count = max(1, int(len(pr) * pagerank_top / 100))
    top_nodes = {n for n, _ in sorted(pr.items(), key=lambda x: -x[1])[:cutoff_count]}
    all_edges = [e for e in all_edges if e["source"] in top_nodes and e["target"] in top_nodes]

# hop 필터
filter_kw = search_input.strip() if search_input else ""
connected = get_connected(filter_kw, all_edges, max_hop=GRAPH_HOP_DEPTH)

if connected:
    edges = [e for e in all_edges if e["source"] in connected and e["target"] in connected]
    nodes = connected
else:
    edges = all_edges
    nodes = {n for e in all_edges for n in (e["source"], e["target"])}

with st.sidebar:
    st.caption(f"필터 후: **{len(nodes)}**개 노드 · **{len(edges)}**개 관계")


st.caption("파란색 엣지를 클릭하면 연결된 기사 원문으로 이동합니다.")

# 그래프 빌드
color_map = {
    "Company": "#FF9999",
    "Person": "#99CC99",
    "Technology": "#9999FF",
    "Product": "#FFCC99",
    "Country": "#FFFF99",
}

net = Network(height="600px", width="100%", bgcolor="#1a1a2e",
              font_color="white", directed=True)
net.barnes_hut(gravity=-5000, central_gravity=0.3, spring_length=200,
               spring_strength=0.01, damping=0.09, overlap=0)

# 노드 색상 맵 (엔티티 계층 구조 기반 테마 적용)
color_map = {
    "Keyword":         "#8E44AD",   # 짙은 보라 (기준 축)
    "NewsArticle":     "#1ABC9C",   # 청록 (정보의 출처)
    
    "Company":         "#E74C3C",   # 빨강 (비즈니스 주체)
    "Industry":        "#2980B9",   # 짙은 파랑 (산업 생태계)
    "Product":         "#F39C12",   # 주황 (출시 제품)
    
    "MacroEvent":      "#F1C40F",   # 노랑/골드 (거시 충격)
    "Entity":          "#95A5A6",   # 회색 (미분류)
}


# 노드 배경색에 따띻 폰트 색상 자동 선택 함수
def _font_color_for(bg_hex: str) -> str:
    """HEX 배경색의 작모 기준으로 검정/흰색 폰트 선택"""
    r, g, b = int(bg_hex[1:3], 16), int(bg_hex[3:5], 16), int(bg_hex[5:7], 16)
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return "#FFFFFF" if luminance < 0.55 else "#1a1a2e"

import math as _math

for node in nodes:
    degree = centrality.get(node, 1)
    # 로그 스케일: 연결 수가 많아도 크기가 적절히 제한됨 (20~48px)
    size = 20 + _math.log1p(degree) * 9
    size = min(size, 48)
    color = color_map.get(node_types.get(node), "#7F8C8D")
    font_color = _font_color_for(color)
    border_color = "#FFFFFF"
    border_width = 2
    if node == filter_kw:
        color = "#8E44AD"  # 검색 키워드(Keyword) 지정 보라색
        size = max(size, 40)
        font_color = "#FFFFFF"
    net.add_node(
        node, label=node,
        title=f"🔵 {node}\n유형: {node_types.get(node, '?')} | 연결 수: {degree}",
        color={"background": color, "border": "#FFFFFF",
               "highlight": {"background": "#FFD700", "border": "#FFA500"}},
        borderWidth=2,
        size=size,
        font={"size": 13, "color": font_color, "bold": True,
              "strokeWidth": 3, "strokeColor": "#000000"},
    )

edge_url_map = {}
pair_counter = collections.defaultdict(int)

for i, rec in enumerate(edges):
    src, tgt = rec["source"], rec["target"]
    edge_id = f"e_{i}"  # 고유 ID 부여
    
    # 동일 노드 쌍 간의 엣지 개수에 따라 곡률(roundness) 조정
    pair_key = tuple(sorted((src, tgt))) 
    pair_counter[pair_key] += 1
    count = pair_counter[pair_key]
    # 첫 번째는 직선(0) 혹은 아주 작은 곡율, 이후는 0.15씩 증가하며 퍼짐
    roundness = 0.0 if count == 1 else 0.15 * (count // 2) * (1 if count % 2 == 0 else -1)

    tooltip = f"[{rec['edge_type']}]"
    if rec.get("description"):
        tooltip += f"\n{rec['description']}"
    if rec.get("source_article"):
        tooltip += f"\n📰 {rec['source_article']}"
    
    has_url = bool(rec.get("source_url"))
    if has_url:
        edge_url_map[edge_id] = rec["source_url"]
        tooltip += "\n🔗 클릭하여 원문 보기"

    edge_label = rec["edge_type"].lower().replace("_", " ")
    display_label = f"🔗 {edge_label}" if has_url else edge_label

    edge_color = "#5DADE2" if has_url else "#95A5A6"
    edge_width = 2 if has_url else 1
    
    net.add_edge(
        src, tgt,
        id=edge_id,
        label=display_label,
        title=tooltip,
        color={"color": edge_color, "highlight": "#FFD700"},
        width=edge_width,
        dashes=False if has_url else [6, 6],
        arrows={"to": {"enabled": True, "scaleFactor": 0.6}},
        smooth={"type": "curvedCW", "roundness": roundness},
        font={"size": 9, "color": edge_color,
              "bold": False, "strokeWidth": 2, "strokeColor": "#000000"},
    )

edge_url_json = json.dumps(edge_url_map, ensure_ascii=False)

js_code = f"""
<script type="text/javascript">
var edgeUrlMap = {edge_url_json};
window.addEventListener('load', function() {{
    var poll = setInterval(function() {{
        if (typeof network !== 'undefined') {{
            clearInterval(poll);
            network.on('click', function(params) {{
                if (params.edges.length > 0 && params.nodes.length === 0) {{
                    var edgeId = params.edges[0];
                    var url = edgeUrlMap[edgeId];
                    if (url) window.open(url, '_blank');
                }}
            }});
        }}
    }}, 200);
}});
</script>
</body>
"""

try:
    net.save_graph("graph.html")
    with open("graph.html", "r", encoding="utf-8") as f:
        html_raw = f.read()
    html_raw = html_raw.replace("</body>", js_code)
    with open("graph.html", "w", encoding="utf-8") as f:
        f.write(html_raw)
    with open("graph.html", "r", encoding="utf-8") as f:
        components.html(f.read(), height=620)
except Exception as e:
    st.error(f"그래프 렌더링 오류: {e}")


# ─────────────────────────────
# Agentic Chat (Graph RAG) 구현
# ─────────────────────────────
st.divider()
st.header("🔍 지식 그래프 기반 검색")

import uuid
from src.graphs.hybrid_rag import rag_app

# 세션 처리
if "messages" not in st.session_state:
    st.session_state.messages = []
if "thread_id" not in st.session_state:
    st.session_state.thread_id = str(uuid.uuid4())

# 1. 검색창을 상단에 배치 (고정된 느낌을 주기 위해 container 사용)
input_container = st.container()
with input_container:
    prompt = st.chat_input("수집된 데이터에 대해 질문해보세요. (예: 삼성전자 협력사는 어디야?, HBM 전망은?)")
    
chat_container = st.container()

# 입력 및 응답 생성 로직
if prompt:
    # 사용자 메시지 추가 (메시지 리스트의 맨 뒤에 추가하지만 출력은 역순으로 함)
    st.session_state.messages.append({"role": "user", "content": prompt})
    
    # 즉시 답변 생성 시각화 (spinner)
    with chat_container:
        with st.chat_message("assistant"):
            with st.spinner("AI가 최적의 검색 경로를 찾고 있습니다... 🔍"):
                config = {"configurable": {"thread_id": st.session_state.thread_id}}
                try:
                    result = rag_app.invoke({"question": prompt, "retry_count": 0, "final_answer": None}, config=config)
                    
                    # Cypher 검증 실패 시 final_answer에 에러 메시지가 담겨옴
                    final_answer = result.get("final_answer")
                    if final_answer:
                        answer = final_answer
                        route = result.get("route", "text2cypher")
                    else:
                        answer = result.get("generation", "응답 생성 실패")
                        route = result.get("route", "fallback")
                    
                    # 세션에 저장 (최신 답변을 위해 메시지 리스트에 추가)
                    st.session_state.messages.append({
                        "role": "assistant", 
                        "content": answer,
                        "route": route
                    })
                    st.rerun() # 전체 다시 그려서 최신 것이 위로 가게 함
                except Exception as e:
                    st.error(f"실행 중 오류: {e}")

# 2. 대화 세트 구성 (질문-답변 쌍을 한 묶음으로 처리)
chat_sets = []
current_set = []
for msg in st.session_state.messages:
    if msg["role"] == "user":
        if current_set:
            chat_sets.append(current_set)
        current_set = [msg]
    else:
        current_set.append(msg)
if current_set:
    chat_sets.append(current_set)
    
# 3. 대화 세트를 역순으로 출력하여 최신 묶음이 위로 오게 함 (질문 -> 답변 순서 유지)
with chat_container:
    for msg_set in reversed(chat_sets):
        for msg in msg_set:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"], unsafe_allow_html=True)
                if msg.get("route"):
                    st.caption(f"🛣️ 검색 경로 판단: `{msg['route']}`")
