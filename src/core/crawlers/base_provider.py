from abc import ABC, abstractmethod
from typing import List, Dict, Any


class BaseDataProvider(ABC):
    """
    모든 데이터 제공자가 상속받아야 하는 추상 기본 클래스입니다.
    이를 통해 데이터 소스(Naver, Bloomberg 등)가 변경되어도 파이프라인의 나머지 부분은 수정할 필요가 없습니다.
    """

    @abstractmethod
    def fetch_data(self, keyword: str, days_back: int) -> List[Dict[str, Any]]:
        """특정 키워드에 대한 원시 데이터를 가져옵니다."""
        pass

    @abstractmethod
    def cluster_data(self, raw_data: List[Dict[str, Any]]) -> List[str]:
        """비용을 줄이기 위해 원시 데이터를 배치(Batch)로 클러스터링합니다."""
        pass

