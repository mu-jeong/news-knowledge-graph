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
    LLM_MODEL, LLM_MAX_WORKERS, CHUNK_SIZE,
    DEFAULT_DAYS_BACK, GRAPH_QUERY_LIMIT, GRAPH_HOP_DEPTH,
    PAGERANK_DEFAULT_TOP,
)

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
    log.append(f"🔍 **[0/4] 증분 기준 조회 중...** `{keyword}`의 마지막 처리 기사를 확인합니다.")
    yield "\n".join(log)
    try:
        _loader_check = Neo4jLoader()
        since_date = _loader_check.get_last_article_date(keyword)
        _loader_check.close()
        if since_date:
            log.append(f"  → 📅 마지막 기사: `{since_date.strftime('%Y-%m-%d %H:%M')}` — 이후 기사만 수집합니다.")
        else:
            log.append(f"  → 🆕 처음 검색하는 키워드 — 최근 {days_back}일치 전체 수집합니다.")
    except Exception as _e:
        since_date = None
        log.append(f"  → ⚠️ 기준 날짜 조회 실패 (전체 수집): {_e}")
    yield "\n".join(log)

    # 1. 뉴스 수집 (since_date 이후 기사만)
    log.append(f"\n📡 **[1/4] 뉴스 수집 중...** `{keyword}`")
    yield "\n".join(log)

    provider = NaverNewsProvider()
    raw_articles = provider.fetch_data(keyword=keyword, days_back=days_back, since_date=since_date)
    article_metadata = provider.get_article_metadata(raw_articles)
    chunks = provider.cluster_data(raw_articles, chunk_size=CHUNK_SIZE)

    if not chunks:
        if since_date:
            log.append(f"  → ✅ **이미 최신 상태입니다.** 마지막 기사(`{since_date.strftime('%Y-%m-%d %H:%M')}`) 이후 신규 기사가 없습니다.")
        else:
            log.append("  → ❌ 수집된 기사가 없습니다. API 키를 확인해주세요.")
        yield "\n".join(log)
        return

    log.append(f"  → {len(raw_articles)}개 기사 / {len(chunks)}개 청크 수집 완료 (신규)")
    log.append(f"\n🤖 **[2/4] LLM 병렬 추출 시작...** ({len(chunks)}개 청크 동시 요청)")
    yield "\n".join(log)

    # 2. LLM 병렬 호출 (ThreadPoolExecutor)
    google_api_key = os.getenv("GOOGLE_API_KEY")
    resolver = EntityResolver()
    all_resolved = []

    def _extract_chunk(idx_chunk):
        idx, chunk = idx_chunk
        llm = ChatGoogleGenerativeAI(model=LLM_MODEL, api_key=google_api_key)
        structured_llm = llm.with_structured_output(GraphData)
        prompt = get_graph_extraction_prompt(chunk)
        result: GraphData = structured_llm.invoke(prompt)
        resolved = resolver.resolve(result)
        return idx, resolved, len(result.entities), len(result.relations)

    with ThreadPoolExecutor(max_workers=LLM_MAX_WORKERS) as executor:
        futures = {executor.submit(_extract_chunk, (i, chunk)): i for i, chunk in enumerate(chunks)}
        for future in as_completed(futures):
            try:
                idx, resolved, n_ent, n_rel = future.result()
                all_resolved.append(resolved)
                log.append(f"  → 청크 [{idx+1}/{len(chunks)}]: 엔티티 {n_ent}개, 관계 {n_rel}개")
            except Exception as e:
                log.append(f"  ⚠️ 청크 실패: {e}")
            yield "\n".join(log)

    total_rels = sum(len(g.relations) for g in all_resolved)
    log.append(f"\n🔗 **[3/4] 엔티티 정규화 완료** → 총 {total_rels}개 관계 정규화")
    yield "\n".join(log)

    # 3. Neo4j 누적 적재 (MERGE 방식 — 기존 데이터 유지)
    log.append(f"\n🗄️ **[4/4] Neo4j 누적 적재 중...**")
    yield "\n".join(log)

    try:
        loader = Neo4jLoader()
        if loader.driver:
            # 3-1. 엔티티/관계 MERGE 적재
            for g in all_resolved:
                loader.load_graph_data(g)
            # 3-2. Article 노드 저장 + Keyword 노드 갱신
            loader.upsert_articles(keyword, article_metadata)
            loader.close()
            log.append(f"  → ✅ 적재 완료! (기사 {len(article_metadata)}개 누적)")
        else:
            log.append("  → ❌ Neo4j 연결 실패")
    except Exception as e:
        log.append(f"  → ❌ 적재 오류: {e}")

    log.append(f"\n🎉 **파이프라인 완료!** 아래 그래프가 자동으로 갱신됩니다.")
    yield "\n".join(log)



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
            records = [r.data() for r in session.run(f"""
                MATCH (k:Keyword {{name: $keyword}})-[:HAS_ARTICLE]->(a:Article)
                WHERE 1=1
{date_filter}                WITH collect(a.url) AS article_urls
                MATCH (n)-[r]->(m)
                WHERE r.source_url IN article_urls
                  AND NOT 'Keyword' IN labels(n) AND NOT 'Article' IN labels(n)
                  AND NOT 'Keyword' IN labels(m) AND NOT 'Article' IN labels(m)
                  AND n.id IS NOT NULL AND m.id IS NOT NULL
                RETURN n.id AS source, labels(n)[0] AS source_type,
                       m.id AS target, labels(m)[0] AS target_type,
                       type(r) AS edge_type,
                       r.description AS description,
                       r.source_article AS source_article,
                       r.source_url AS source_url
                LIMIT {GRAPH_QUERY_LIMIT}
            """, **params)]
            centrality = {r["node_id"]: r["degree"] for r in session.run(f"""
                MATCH (k:Keyword {{name: $keyword}})-[:HAS_ARTICLE]->(a:Article)
                WHERE 1=1
{date_filter}                WITH collect(a.url) AS article_urls
                MATCH (n)-[r]-(m)
                WHERE r.source_url IN article_urls
                  AND NOT 'Keyword' IN labels(n) AND NOT 'Article' IN labels(n)
                WITH n, count(r) AS degree
                RETURN n.id AS node_id, degree
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
        adj[e["source"]].append(e["target"])
        adj[e["target"]].append(e["source"])
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
    days_input = st.number_input("수집 기간 (일)", min_value=1, max_value=30, value=DEFAULT_DAYS_BACK)
    run_btn = st.button("🚀 검색 & 그래프 생성", type="primary", use_container_width=True)

    st.divider()
    st.subheader("📅 기사 기간 필터")
    from datetime import date as _date, timedelta as _timedelta
    _today = _date.today()
    _default_from = _today - _timedelta(days=30)
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
**범례**
- 🟡 금색: 검색 키워드 노드
- 🔴 빨강: 기업  🟢 초록: 인물  🔵 파랑: 기술  🟠 주황: 제품
- **🔗 실선 (파란)**: 기사 출처 있음 — 클릭 시 원문 이동
- **점선 (회색)**: 기사 출처 없음
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
    all_node_type_options = ["Company", "Person", "Technology", "Product", "Country", "Entity"]
    selected_node_types = st.multiselect(
        "node_type_filter",
        options=all_node_type_options,
        default=all_node_type_options,
        label_visibility="collapsed",
    )

# 파이프라인 실행
if run_btn and search_input.strip():
    keyword = search_input.strip()

    st.info(f"**'{keyword}'** 뉴스를 수집하고 그래프를 구축합니다.")
    log_area = st.empty()

    with st.spinner("파이프라인 실행 중..."):
        for log_msg in run_pipeline(keyword, days_back=int(days_input)):
            log_area.markdown(log_msg)

    st.success("✅ 완료! 그래프가 아래에 표시됩니다.")
    st.rerun()

elif run_btn and not search_input.strip():
    st.sidebar.warning("검색어를 입력해 주세요.")


# ─────────────────────────────
# 그래프 표시
# ─────────────────────────────
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

net = Network(height="820px", width="100%", bgcolor="#1a1a2e",
              font_color="white", directed=True)
net.barnes_hut(gravity=-5000, central_gravity=0.3, spring_length=200,
               spring_strength=0.01, damping=0.09, overlap=0)

# 노드 색상 맵 (더 선명한 파스텔 톤)
color_map = {
    "Company":    "#E74C3C",   # 선명한 빨강
    "Person":     "#2ECC71",   # 선명한 초록
    "Technology": "#3498DB",   # 선명한 파란
    "Product":    "#E67E22",   # 설지화된 주황
    "Country":    "#9B59B6",   # 볜란 보라
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
    if node == filter_kw:
        color = "#FFD700"
        size = max(size, 40)
        font_color = "#1a1a2e"
    net.add_node(
        node, label=node,
        title=f"🔵 {node}\n유형: {node_types.get(node, '?')} | 연결 수: {degree}",
        color={"background": color, "border": "#FFFFFF",
               "highlight": {"background": "#FFD700", "border": "#FFA500"}},
        size=size,
        font={"size": 13, "color": font_color, "bold": True,
              "strokeWidth": 3, "strokeColor": "#000000"},
    )

edge_url_map = {}
for rec in edges:
    key = f"{rec['source']}||{rec['target']}"
    tooltip = f"[{rec['edge_type']}]"
    if rec.get("description"):
        tooltip += f"\n{rec['description']}"
    if rec.get("source_article"):
        tooltip += f"\n📰 {rec['source_article']}"
    has_url = bool(rec.get("source_url"))
    if has_url:
        edge_url_map[key] = rec["source_url"]
        tooltip += "\n🔗 클릭하여 원문 보기"
    # 관계 유형: 소문자 + 언더스코어 → 공백으로 가독성 개선
    edge_label = rec["edge_type"].lower().replace("_", " ")
    display_label = f"🔗 {edge_label}" if has_url else edge_label

    net.add_edge(
        rec["source"], rec["target"],
        label=display_label,
        title=tooltip,
        color={"color": "#5DADE2" if has_url else "#95A5A6",   # 기사없는 엣지: 밝은 회색
               "highlight": "#FFD700"},
        width=2 if has_url else 1,
        dashes=False if has_url else [6, 6],                   # 기사없는 엣지: 점선
        arrows={"to": {"enabled": True, "scaleFactor": 0.6}},
        smooth={"type": "curvedCW", "roundness": 0.1},
        font={"size": 9, "color": "#5DADE2" if has_url else "#BDC3C7",
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
                    var edgeObj = edges.get(edgeId);
                    if (!edgeObj) return;
                    var url = edgeUrlMap[edgeObj.from + '||' + edgeObj.to];
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
        components.html(f.read(), height=840)
except Exception as e:
    st.error(f"그래프 렌더링 오류: {e}")
