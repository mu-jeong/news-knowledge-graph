import os
import re
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
    LLM_MODEL, ALLOWED_NEWS_DOMAINS,
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


def _is_allowed_source(url: str) -> bool:
    """URL이 허용된 뉴스 도메인 목록에 속하는지 확인합니다."""
    if not url:
        return False
    for domain in ALLOWED_NEWS_DOMAINS:
        if domain in url:
            return True
    return False


class NaverNewsProvider(BaseDataProvider):
    """
    네이버 뉴스 API를 연동하여 특정 키워드 관련 뉴스를 가져오고,
    유사한 주제(단순화된 방식)로 텍스트를 배치(Batch) 단위로 묶는 클래스.
    """

    def __init__(self, client_id: str = None, client_secret: str = None):
        self.client_id = client_id or os.getenv("NAVER_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("NAVER_CLIENT_SECRET")
        
        if not self.client_id or not self.client_secret:
            print("Warning: NAVER_CLIENT_ID and NAVER_CLIENT_SECRET are not set.")

    def fetch_data(self, keyword: str, days_back: int = 1, watermarks: dict = None) -> List[Dict[str, Any]]:
        """
        네이버 뉴스 API에서 키워드 검색을 통해 최근 기사를 가져옵니다.
        days_back에 비례해 페이지네이션을 적용합니다 (하루 ~10건 추정, 최대 1,000건).
        watermarks(날짜별 워터마크 딕셔너리)를 지정하면 이전 수집 이력 이후 기사만 필터링합니다.
        """
        if watermarks is None: watermarks = {}
        if not self.client_id or not self.client_secret:
             raise ValueError("API Keys are missing. Cannot fetch data.")

        enc_text = urllib.parse.quote(keyword)

        # days_back 기준 컷오프 날짜 (사용자가 요청한 수집 범위)
        # 일(Day) 기준 컷오프: 5일이면 오늘부터 4일 전(총 5일)까지 수집
        now = datetime.now()
        # days_back=1이면 0일 전(오늘), days_back=5이면 4일 전(3/18) 00시부터 수집 시작
        cutoff_date = now - timedelta(days=days_back - 1)
        effective_cutoff = cutoff_date.replace(hour=0, minute=0, second=0, microsecond=0)

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
                    
                # 성능/버그 해결: 현재 페이지의 마지막 기사가 effective_cutoff보다 옛날 것이면 뒤쪽 페이지 안 부름
                if page_items:
                    last_item = page_items[-1]
                    try:
                        last_date = datetime.strptime(last_item.get("pubDate", ""), "%a, %d %b %Y %H:%M:%S %z").replace(tzinfo=None)
                        if last_date <= effective_cutoff:
                            break
                    except ValueError:
                        pass

            # 날짜 필터링
            for item in all_items:
                try:
                    pub_date = datetime.strptime(
                        item.get("pubDate", ""), "%a, %d %b %Y %H:%M:%S %z"
                    ).replace(tzinfo=None)
                    
                    # 1차 허들: days_back(수집기간 한계선)보다 과거이면 버림
                    if pub_date <= effective_cutoff:
                        continue
                        
                    # 2차 허들: 날짜(Date)별 워터마크가 있고, 이 워터마크 시각보다 이전이면 이미 수집된 것이므로 버림
                    date_str = pub_date.strftime("%Y-%m-%d")
                    if date_str in watermarks:
                        wm_time = datetime.strptime(watermarks[date_str], "%Y-%m-%dT%H:%M:%S")
                        if pub_date <= wm_time:
                            continue
                            
                    filtered_items.append(item)
                except ValueError:
                    continue

            if watermarks:
                mode = f"날짜별 워터마크 기반 증분 수집"
            else:
                mode = f"전체 수집 (최근 {days_back}일)"
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

    def cluster_data(self, raw_data: List[Dict[str, Any]], batch_size: int = 10) -> List[str]:
        """
        간단한 클러스터링: 발행일(시간) 내림차순으로 이미 정렬되어 있으므로, 
        N개씩 묶어서 하나의 텍스트 배치로 병합합니다. (비용 절감을 위한 Batch 처리)
        """
        if not raw_data:
            return []
            
        batches = []
        current_batch = []

        for idx, item in enumerate(raw_data):
            # HTML 태그 및 탈출 문자 간단히 정제
            clean_title = item.get('title', '').replace('<b>', '').replace('</b>', '').replace('&quot;', '"')
            clean_desc = item.get('description', '').replace('<b>', '').replace('</b>', '').replace('&quot;', '"')
            clean_link = item.get('originallink') or item.get('link', '')

            # 영문 및 미승인 도메인 제외
            if _is_english(clean_title) or _is_english(clean_desc):
                # print(f"  [제외] 영문 기사 감지: {clean_title[:40]}...")
                if idx == len(raw_data) - 1 and current_batch:
                    combined_text = "\n---\n".join(current_batch)
                    batches.append(combined_text)
                    current_batch = []
                continue
            if not _is_allowed_source(clean_link):
                # print(f"  [제외] 미승인 도메인 기사: {clean_link[:60]}")
                if idx == len(raw_data) - 1 and current_batch:
                    combined_text = "\n---\n".join(current_batch)
                    batches.append(combined_text)
                    current_batch = []
                continue

            # 하나의 뉴스 요약에 기사 ID(순번) 부여
            article_id_str = f"Article_{len(current_batch) + 1}"
            news_text = f"[{article_id_str}]\n제목: {clean_title}\n링크: {clean_link}\n내용: {clean_desc}"
            current_batch.append(news_text)

            # batch_size만큼 쌓였다면 하나의 문자열로 결합 (마지막 처리는 루프 밖에서 일괄 수행)
            if len(current_batch) >= batch_size:
                combined_text = "\n---\n".join(current_batch)
                batches.append(combined_text)
                current_batch = []
        
        # 마지막 남아있는 배치 처리
        if current_batch:
            combined_text = "\n---\n".join(current_batch)
            batches.append(combined_text)
        
        print(f"총 {len(raw_data)}개의 뉴스를 {len(batches)}개의 묶음(배치)으로 압축(클러스터링)했습니다.")
        return batches

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
