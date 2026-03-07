from typing import Dict
from src.config.schema import GraphData, Entity, Relation

class EntityResolver:
    """
    모듈 3. 엔티티 정규화 (Entity Resolution)
    지식 그래프가 파편화되는 것을 막기 위해 여러 동의어를 하나의 표준 명칭으로 통합합니다.
    """
    def __init__(self, alias_dict: Dict[str, str] = None):
        # 1차 사전 기반 맵핑 (Rule-based: 비용 0)
        # 실무에서는 이 딕셔너리를 DB나 별도의 설정 파일로 관리합니다.
        self.alias_dict = alias_dict or {
            # === 국내 주요 기업 ===
            "삼전": "삼성전자",
            "Samsung": "삼성전자",
            "삼성": "삼성전자",
            "SAMSUNG": "삼성전자",
            "하이닉스": "SK하이닉스",
            "SKhynix": "SK하이닉스",
            "엘지전자": "LG전자",
            "LGE": "LG전자",
            "현차": "현대자동차",
            "현대차": "현대자동차",
            "카뱅": "카카오뱅크",
            "네이버": "NAVER",
            
            # === 글로벌 빅테크 및 반도체 ===
            "엔비디아": "NVIDIA",
            "Nvidia": "NVIDIA",
            "NVDIA": "NVIDIA",
            "마이크로소프트": "Microsoft",
            "MS": "Microsoft",
            "마소": "Microsoft",
            "TSMC": "TSMC",
            "애플": "Apple",
            "APPLE": "Apple",
            "구글": "Google",
            "알파벳": "Google",
            "아마존": "Amazon",
            "메타": "Meta",
            "페이스북": "Meta",
            "테슬라": "Tesla",
            "오픈에이아이": "OpenAI",
            "암": "ARM",
            
            # === 주요 인물 ===
            "이재용": "이재용 삼성전자 회장",
            "머스크": "일론 머스크",
            "일론머스크": "일론 머스크",
            "젠슨황": "젠슨 황",
            "샘알트만": "샘 알트만",
            "샘 알트먼": "샘 알트만",
            "팀쿡": "팀 쿡",
            
            # === 기술 용어 및 기타 ===
            "인공지능": "AI",
            "Artificial Intelligence": "AI",
            "대규모 언어 모델": "LLM",
            "Large Language Model": "LLM",
            "생성형AI": "생성형 AI",
            "Generative AI": "생성형 AI",
            "파운드리": "Foundry"
        }

    def resolve(self, graph_data: GraphData) -> GraphData:
        """추출된 GraphData의 엔티티 명칭(노드 및 엣지 연결고리)을 정규화합니다."""
        resolved_entities = []
        resolved_relations = []
        
        # 1. 엔티티 정규화 (노드 이름 표준화)
        for entity in graph_data.entities:
            normalized_name = self.alias_dict.get(entity.name, entity.name)
            resolved_entities.append(Entity(name=normalized_name, type=entity.type))
            
        # 2. 관계 정규화 (엣지의 출발점/도착점 이름도 표준화된 이름에 맞춰 변경)
        for rel in graph_data.relations:
            normalized_source = self.alias_dict.get(rel.source, rel.source)
            normalized_target = self.alias_dict.get(rel.target, rel.target)
            resolved_relations.append(Relation(
                source=normalized_source,
                target=normalized_target,
                type=rel.type,
                description=rel.description,
                source_article=getattr(rel, 'source_article', None),
                source_url=getattr(rel, 'source_url', None)
            ))
            
        return GraphData(entities=resolved_entities, relations=resolved_relations)
