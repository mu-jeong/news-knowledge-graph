# 🏢 News Graph Pipeline - Architecture Document

이 문서는 `News Graph Pipeline` 프로젝트의 전체 시스템 아키텍처 및 핵심 모듈의 동작 원리를 설명합니다. 파편화된 비정형 뉴스 데이터를 수집하여 의미 있는 지식 그래프(Knowledge Graph)로 구조화하고, 이를 인터랙티브하게 탐색할 수 있는 엔드 투 엔드(End-to-End) 데이터 파이프라인 시스템을 구축하는 데 초점을 맞추고 있습니다.

---

## 🏗️ 1. High-Level Architecture Overview

시스템은 유지보수성과 확장성을 보장하기 위해 각 역할이 명확히 분리된 **디커플링(Decoupling)** 아키텍처를 따르고 있습니다. 전반적인 데이터 흐름은 다음과 같습니다.

1. **Data Ingestion (크롤링):** 다양한 출처(뉴스, 공시 등)에서 비정형 텍스트 데이터를 수집 및 청킹(Chunking)합니다. 영문 기사는 LLM을 통해 한국어로 자동 번역합니다.
2. **Entity Resolution (정규화 및 필터링):** 수집된 데이터에서 엔티티(기업, 인물, 기술 등)를 추출하고 동의어를 표준어로 병합합니다.
3. **Graph Construction (증분 적재):** 정제된 엔티티와 관계(Edge) 데이터를 Pydantic 스키마 기반으로 검증한 후 Neo4j에 **MERGE 방식으로 누적 적재**합니다. 동일 키워드를 재검색할 경우 마지막 처리 기사 이후의 신규 기사만 처리합니다(Incremental Update).
4. **Visualization & Analytics (시각화 및 분석):** 대시보드 상에서 물리적 연결망을 시각화하고, PageRank 알고리즘을 통해 핵심 테마를 추출하거나 기간 필터링을 통해 시계열적 변화를 추적합니다. 부수적으로 자연어 질문을 Cypher 쿼리로 변환하여 응답하는 Graph RAG 채팅 인터페이스도 제공합니다.

---

## 📦 2. Core Modules & Directory Structure

프로젝트의 핵심 디렉토리 및 모듈별 역할은 다음과 같습니다.

### `src/configs/` (Layer 1: Config & Schema)
* **`schema.py`:** `Pydantic`을 활용하여 추출될 그래프 데이터의 스키마(`Entity`, `Relation`, `GraphData`)를 엄격하게 정의합니다.
* 이를 통해 LLM(대형 언어 모델)의 환각(Hallucination) 현상을 방지하고, 항상 정규화된 JSON 포맷의 출력을 강제하여 파이프라인의 안정성을 보장합니다.
* **`settings.py`:** 파이프라인 전체에서 사용하는 **모든 튜닝 파라미터를 한 곳에서 관리**합니다. 코드를 수정하지 않고 이 파일만 변경하면 동작을 조정할 수 있습니다.
  * LLM 모델명 (`LLM_MODEL`), 병렬 호출 수 (`LLM_MAX_WORKERS`), 청크 크기 (`CHUNK_SIZE`)
  * 페이지네이션 기준 (`DAYS_BACK_PER_PAGE`, `MAX_PAGES`)
  * 유사 기사 필터 (`MAX_ARTICLES_PER_DAY`, `SIMILARITY_THRESHOLD`)
  * 그래프 표시 설정 (`GRAPH_QUERY_LIMIT`, `GRAPH_HOP_DEPTH`)

### `src/core/crawlers/` (Layer 2: Data Crawlers)
* **`base_provider.py`:** 향후 Bloomberg, Yahoo Finance, DART 등 다양한 데이터 프로바이더를 수용할 수 있도록 `fetch_data` / `cluster_data` 추상 인터페이스를 정의합니다.
* **`naver_news.py`:** 네이버 뉴스 API 구현체입니다. 주요 기능은 다음과 같습니다.
  * `fetch_data(since_date=)`: `settings.DAYS_BACK_PER_PAGE`(기본 3일)당 1페이지(100건)씩 페이지네이션 요청을 수행합니다 (최대 `MAX_PAGES`=10페이지). `since_date` 이후 기사만 필터링하여 증분 업데이트에 활용합니다.
  * `filter_similar_articles()`: 날짜별로 기사를 묶고, 제목 유사도(`SequenceMatcher`) ≥ `SIMILARITY_THRESHOLD`(기본 0.6)인 기사를 중복으로 판별해 제거합니다. 중복 제거 후에도 하루 `MAX_ARTICLES_PER_DAY`(기본 30건)를 초과하면 균등 샘플링합니다.
  * `cluster_data()`: 기사를 N개씩 묶어 하나의 텍스트 청크로 병합합니다. 영문 기사는 LLM(Gemini)으로 한국어 번역 후 청크에 포함합니다.
  * `get_article_metadata()`: URL / 제목 / 발행일 메타데이터를 추출하여 Neo4j Article 노드 저장에 활용합니다.

### `src/core/utils/` (Layer 3: Data Processing & Entity Resolution)
* **`entity_resolution.py`:** 데이터의 파편화를 막기 위한 핵심 모듈입니다. '삼전', '삼성물산', 'Samsung' 등 다양한 형태로 등장하는 엔티티를 하나의 일관된 표준어로 정규화합니다.
* **[진화 방향 - 하이브리드 파이프라인]** 단순 사전(Dictionary) 매핑을 넘어, 1차 임베딩 기반 군집화 → 2차 sLM/LLM 기반 검증 → 3차 동적 캐싱(Dynamic Dictionary)을 통해 비용 효율적인 엔터프라이즈 엔티티 정규화를 수행할 예정입니다.

### `src/graphs/` (Layer 4: Graph Database & RAG)
* **`neo4j_manager.py`:** Neo4j 적재를 담당하는 핵심 클래스입니다. 주요 기능은 다음과 같습니다.
  * `get_last_article_date(keyword)`: 해당 키워드로 마지막 처리된 기사의 날짜를 반환합니다. 증분 업데이트의 기준점입니다.
  * `upsert_articles(keyword, articles)`: 처리된 기사를 `Article` 노드로 저장하고 `Keyword` 노드와 연결합니다. `Keyword.last_updated`는 파이프라인 실행 시각이 아닌 **실제 기사의 최신 발행일(`max(published_at)`)**로 기록합니다.
  * `load_graph_data(graph_data)`: 추출된 Entity/Relation을 MERGE 방식으로 적재합니다.
* **`graph_rag_bot.py`:** 사용자의 자연어 질문을 LangChain의 `GraphCypherQAChain`을 활용해 Neo4j에서 실행 가능한 **Cypher 쿼리로 변환(Text-to-Cypher)**합니다.

### `apps/gui/` (Layer 5: User Interface & Analytics)
* **`app.py`:** Streamlit과 Pyvis를 활용하여 구축된 대화형 그래프 시각화 대시보드입니다.
  * **증분 파이프라인:** 검색 버튼 클릭 시 `[0/4] 증분 기준 조회 → [1/4] 신규 기사 수집 → [2/4] LLM 추출 → [3/4] 정규화 → [4/4] 누적 적재` 순으로 진행됩니다.
  * **다중 필터 기반 뷰:** 노드/엣지 유형을 고를 수 있고, `NetworkX` 기반의 **PageRank**로 핵심 허브 노드 상위 N%만 추려내어 가독성을 높입니다.
  * **시계열 연결망 뷰:** 기간 필터(Date Picker)를 통해, 지정된 날짜 사이에 발행된 데이터 엣지만 분리하여 특정 시점(Time-series)의 지식 연결망을 검증할 수 있습니다.
  * **엣지 스타일 & 직관적 UI:** 기사 출처가 존재하는 엣지는 **밝은 실선 및 🔗 라벨**로, 기사 출처가 없는 엣지는 **어두운 회색 점선**으로 분리하여 노이즈를 식별하고 빠르게 원문으로 넘어갈 수 있게 돕습니다.
  * **`GRAPH_HOP_DEPTH`-hop BFS 필터:** 검색어를 기준으로 지정된 단계 이내로 연결된 노드/엣지만 표시하여 관련도 높은 정보에 집중합니다.

---

## ⚙️ 3. 파라미터 관리 (`src/configs/settings.py`)

모든 튜닝 가능한 파라미터는 `settings.py` 한 곳에서 관리됩니다.

| 파라미터 | 기본값 | 설명 |
|---|---|---|
| `LLM_MODEL` | `gemini-2.5-flash` | LLM 모델명 |
| `LLM_MAX_WORKERS` | `5` | 병렬 LLM API 호출 수 |
| `CHUNK_SIZE` | `20` | LLM 1회 호출당 기사 묶음 크기 |
| `DEFAULT_DAYS_BACK` | `1` | UI 기본 수집 기간(일) |
| `DAYS_BACK_PER_PAGE` | `3` | 페이지당 기준 일수 (3일=1페이지=100건) |
| `MAX_PAGES` | `10` | 최대 수집 페이지 (상한 1,000건) |
| `MAX_ARTICLES_PER_DAY` | `30` | 날짜별 최대 기사 수 (유사 필터 후) |
| `SIMILARITY_THRESHOLD` | `0.6` | 제목 유사도 임계값 (이상이면 중복 판별) |
| `GRAPH_QUERY_LIMIT` | `500` | Neo4j 조회 최대 엣지 수 |
| `GRAPH_HOP_DEPTH` | `3` | 검색어 기준 표시 홉(Hop) 깊이 |
| `PAGERANK_DEFAULT_TOP` | `40` | 그래프 표시 시 PageRank 기준 상위 % (하위 노드 숨김) |


```
(:Keyword {name, last_updated})
    │
    └──[:HAS_ARTICLE]──▶ (:Article {url, title, published_at, keyword})

(:Company / :Person / :Technology / :Product / :Country)
    └──[:RELATION_TYPE {description, source_article, source_url}]──▶ (:Entity)
```

| 노드 | 속성 | 역할 |
|------|------|------|
| `Keyword` | `name`, `last_updated` | 검색어 추적, 마지막 처리 시각 기록 |
| `Article` | `url`(PK), `title`, `published_at`, `keyword` | 기사 중복 방지 + 증분 기준점 |
| `Entity` 계열 | `id`(PK=name), `name` | 키워드 무관 공유 → 크로스-키워드 분석 가능 |

---

## 💰 4. 특징 및 향후 최적화 전략 (Cost & Automation)

엔터프라이즈 도입을 위한 핵심 과제 및 설계 방향입니다.

### 1) 3-Tier Cost Routing Architecture (비용 최적화)
수많은 비정형 텍스트를 모두 고비용 LLM(GPT-4o, Gemini Pro 등)에 태우는 것은 비효율적입니다.
* **Tier 3 (Junk Drop):** 단순 정보, 노이즈 기사는 정규표현식이나 소형 분류 모델로 사전 차단 ($0 비용).
* **Tier 2 (General Info):** 일반 시장 동향은 로컬 sLM(소형 언어모델) 또는 GLiNER(개체명 인식 특화 모델)를 활용하여 가볍게 노드 추출.
* **Tier 1 (High Value DL):** M&A 피인수, 핵심 공급망 이슈 등 실사에 영향을 주는 핵심 뉴스에만 GPT-4o / Gemini를 할당하여 심층 엣지(Edge) 추론.

### 2) Pipeline Automation
데이터 크롤링부터 Entity Resolution, Neo4j DB 적재로 이어지는 배치성 작업을 **Apache Airflow** 혹은 **Prefect**와 같은 오케스트레이션 툴과 연동하여, 매일 장 개장 전 완전 자동화된 파이프라인으로 구성하는 것을 목표로 합니다. 현재 구현된 증분 업데이트 구조는 이 자동화의 핵심 기반이 됩니다.
