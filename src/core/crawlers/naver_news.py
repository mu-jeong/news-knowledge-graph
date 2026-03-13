import math
import urllib.request
import urllib.parse
import json
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

from src.core.crawlers.base_provider import BaseDataProvider
from src.configs.settings import (
    DAYS_BACK_PER_PAGE, MAX_PAGES,
    MAX_ARTICLES_PER_DAY, SIMILARITY_THRESHOLD,
    LLM_MODEL,
)


def _is_english(text: str) -> bool:
    """제목 전체가 영문인 경우에만 영문 기사로 판단합니다. (한글이 하나라도 포함되어 있으면 한글 기사로 간주)"""
    if not text:
        return False
    
    # 문장에 한글이 하나라도 포함되어 있으면 영문 기사가 아님
    if re.search(r'[가-힣]', text):
        return False
        
    # 한글이 전혀 없고 영문 알파벳이 존재하는 경우에만 영문으로 판별
    if re.search(r'[a-zA-Z]', text):
        return True
        
    return False


class NaverNewsProvider(BaseDataProvider):
    """
    네이버 뉴스 API를 연동하여 특정 키워드 관련 뉴스를 가져오고,
    유사한 주제(단순화된 방식)로 텍스트를 청크 단위로 묶는 클래스.
    """

    def __init__(self, client_id: str = None, client_secret: str = None):
        self.client_id = client_id or os.getenv("NAVER_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("NAVER_CLIENT_SECRET")
        
        if not self.client_id or not self.client_secret:
            print("Warning: NAVER_CLIENT_ID and NAVER_CLIENT_SECRET are not set.")

    def fetch_data(self, keyword: str, days_back: int = 7,
                   since_date: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """
        네이버 뉴스 API에서 키워드 검색을 통해 최근 기사를 가져옵니다.
        days_back에 비례해 페이지네이션을 적용합니다 (하루 ~10건 추정, 최대 1,000건).

        since_date를 지정하면 해당 시각 이후의 기사만 반환합니다.
        """
        if not self.client_id or not self.client_secret:
             raise ValueError("API Keys are missing. Cannot fetch data.")

        enc_text = urllib.parse.quote(keyword)

        # days_back 기준 컷오프 날짜
        cutoff_date = datetime.now() - timedelta(days=days_back)
        # since_date가 있으면 둘 중 더 최근 날짜를 기준으로 사용
        effective_cutoff = since_date.replace(tzinfo=None) if since_date else cutoff_date
        if since_date:
            effective_cutoff = max(effective_cutoff, cutoff_date)

        # DAYS_BACK_PER_PAGE일 = 100건 기준으로 필요한 페이지 수 산출 (최대 MAX_PAGES페이지)
        total_pages = min(math.ceil(days_back / DAYS_BACK_PER_PAGE), MAX_PAGES)

        all_items: List[Dict[str, Any]] = []
        filtered_items: List[Dict[str, Any]] = []

        try:
            for page in range(total_pages):
                start = page * 100 + 1
                url = (
                    f"https://openapi.naver.com/v1/search/news.json"
                    f"?query={enc_text}&display=100&start={start}&sort=date"
                )
                request = urllib.request.Request(url)
                request.add_header("X-Naver-Client-Id", self.client_id)
                request.add_header("X-Naver-Client-Secret", self.client_secret)

                response = urllib.request.urlopen(request)
                if response.getcode() != 200:
                    print(f"Error Code: {response.getcode()} (page {page + 1})")
                    break

                data = json.loads(response.read().decode("utf-8"))
                page_items = data.get("items", [])
                all_items.extend(page_items)

                # 페이지가 100건 미만이면 더 이상 결과 없음 → 조기 종료
                if len(page_items) < 100:
                    break

            # 날짜 필터링
            for item in all_items:
                try:
                    pub_date = datetime.strptime(
                        item.get("pubDate", ""), "%a, %d %b %Y %H:%M:%S %z"
                    )
                    # since_date보다 "이후"인 기사만 포함 (같은 시각은 이미 처리됨)
                    if pub_date.replace(tzinfo=None) > effective_cutoff:
                        filtered_items.append(item)
                except ValueError:
                    continue

            mode = (
                f"마지막 기사 이후({since_date.strftime('%Y-%m-%d %H:%M')})부터"
                if since_date else f"최근 {days_back}일"
            )
            pages_fetched = (len(all_items) + 99) // 100
            before_dedup = len(filtered_items)

            # 유사 기사 필터링 (하루 최대 30건)
            filtered_items = self.filter_similar_articles(filtered_items)

            print(
                f"[{keyword}] 총 {len(all_items)}건 조회 ({pages_fetched}페이지) "
                f"→ 날짜 필터 {before_dedup}건 "
                f"→ 유사 제거 후 {len(filtered_items)}건 ({mode} 기준)."
            )
            return filtered_items

        except Exception as e:
            print(f"Failed to fetch data from Naver API: {e}")
            return []


    def filter_similar_articles(
        self,
        articles: List[Dict[str, Any]],
        max_per_day: int = MAX_ARTICLES_PER_DAY,
        similarity_threshold: float = SIMILARITY_THRESHOLD,
    ) -> List[Dict[str, Any]]:
        """
        날짜별로 기사를 그룹화하고:
        1. 제목 유사도가 similarity_threshold 이상인 기사를 중복으로 판별 → 대표 1건만 유지
        2. 중복 제거 후에도 max_per_day 초과 시 균등 샘플링
        """
        from collections import defaultdict

        def _clean_title(title: str) -> str:
            return title.replace("<b>", "").replace("</b>", "").replace("&quot;", '"').strip()

        # 날짜별 그룹화
        daily_groups: dict = defaultdict(list)
        for item in articles:
            try:
                pub_date = datetime.strptime(
                    item.get("pubDate", ""), "%a, %d %b %Y %H:%M:%S %z"
                )
                date_key = pub_date.date()
            except ValueError:
                date_key = datetime.now().date()
            daily_groups[date_key].append(item)

        result: List[Dict[str, Any]] = []
        total_removed = 0
        total_sampled = 0

        for date_key in sorted(daily_groups.keys(), reverse=True):
            day_articles = daily_groups[date_key]
            if not day_articles:
                continue

            # 1. 제목 유사도 기반 중복 제거 (TF-IDF + Cosine Similarity)
            titles = [_clean_title(a.get("title", "")) for a in day_articles]
            unique_indices = []

            if titles:
                try:
                    from sklearn.feature_extraction.text import TfidfVectorizer
                    from sklearn.metrics.pairwise import cosine_similarity
                    
                    # 단어(공백 기준) 및 2-gram 단위로 문맥 의미를 벡터화
                    vectorizer = TfidfVectorizer(analyzer='word', ngram_range=(1, 2))
                    tfidf_matrix = vectorizer.fit_transform(titles)
                    sim_matrix = cosine_similarity(tfidf_matrix)

                    for i in range(len(titles)):
                        is_duplicate = False
                        for j in unique_indices:
                            # 코사인 유사도가 임계치 이상이면 중복으로 판별 (순서 무관)
                            if sim_matrix[i, j] >= similarity_threshold:
                                is_duplicate = True
                                break
                        if not is_duplicate:
                            unique_indices.append(i)
                except ValueError:
                    # 모든 제목이 비어있거나 하는 등 벡터화 불가 상황 처리
                    unique_indices = [0] if titles else []

            unique = [day_articles[i] for i in unique_indices]
            
            total_removed += len(day_articles) - len(unique)

            # 2. max_per_day 초과 시 균등 샘플링
            if len(unique) > max_per_day:
                step = len(unique) / max_per_day
                sampled = [unique[int(i * step)] for i in range(max_per_day)]
                total_sampled += len(unique) - max_per_day
                unique = sampled

            result.extend(unique)

        print(
            f"  [유사 필터] 중복 제거 {total_removed}건 / 다운샘플 {total_sampled}건 → 최종 {len(result)}건"
        )
        return result

    def cluster_data(self, raw_data: List[Dict[str, Any]], chunk_size: int = 10) -> List[str]:
        """
        간단한 클러스터링: 발행일(시간) 내림차순으로 이미 정렬되어 있으므로, 
        N개씩 묶어서 하나의 텍스트 청크로 병합합니다. (비용 절감을 위한 Batch 처리)
        """
        if not raw_data:
            return []
            
        chunks = []
        current_chunk = []

        for idx, item in enumerate(raw_data):
            # HTML 태그 및 탈출 문자 간단히 정제
            clean_title = item.get('title', '').replace('<b>', '').replace('</b>', '').replace('&quot;', '"')
            clean_desc = item.get('description', '').replace('<b>', '').replace('</b>', '').replace('&quot;', '"')
            clean_link = item.get('originallink') or item.get('link', '')

            # 영문 제목/내용이면 제외
            if _is_english(clean_title) or _is_english(clean_desc):
                print(f"  [제외] 영문 기사 감지 → 크롤링 대상에서 제외: {clean_title[:40]}...")
                # 예외 처리: 마지막 기사가 스킵되면서 지금까지 쌓인 청크가 있다면 저장
                if idx == len(raw_data) - 1 and current_chunk:
                    combined_text = "\n---\n".join(current_chunk)
                    chunks.append(combined_text)
                    current_chunk = []
                continue

            # 하나의 뉴스 요약
            news_text = f"제목: {clean_title}\n링크: {clean_link}\n내용: {clean_desc}"
            current_chunk.append(news_text)

            # chunk_size만큼 쌓이거나 마지막 데이터일 때 하나의 문자열로 결합
            if len(current_chunk) >= chunk_size or idx == len(raw_data) - 1:
                if current_chunk:
                    combined_text = "\n---\n".join(current_chunk)
                    chunks.append(combined_text)
                    current_chunk = []
        
        print(f"총 {len(raw_data)}개의 뉴스를 {len(chunks)}개의 청크로 압축(클러스터링)했습니다.")
        return chunks

    def get_article_metadata(self, raw_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        네이버 API 원시 응답에서 Article 노드 저장에 필요한 메타데이터를 추출합니다.

        Returns:
            [{"url": str, "title": str, "published_at": datetime}, ...]
        """
        metadata = []
        for item in raw_data:
            clean_title = (
                item.get("title", "")
                .replace("<b>", "").replace("</b>", "").replace("&quot;", '"')
            )
            url = item.get("originallink") or item.get("link", "")
            try:
                pub_date = datetime.strptime(item.get("pubDate", ""), "%a, %d %b %Y %H:%M:%S %z")
            except ValueError:
                pub_date = datetime.now()
            metadata.append({
                "url": url,
                "title": clean_title,
                "published_at": pub_date,
            })
        return metadata

# 간단한 테스트 블럭
if __name__ == "__main__":
    provider = NaverNewsProvider(client_id="dummy", client_secret="dummy")
    # 실제 환경에서는 provider.run_pipeline("엔비디아", days_back=7) 호출
