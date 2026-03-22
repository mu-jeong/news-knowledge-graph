from typing import Dict
from src.configs.schema import GraphData, Entity, Relation

class EntityResolver:
    """
    모듈 3. 엔티티 정규화 (Entity Resolution)
    지식 그래프가 파편화되는 것을 막기 위해 여러 동의어를 하나의 표준 명칭으로 통합합니다.
    """
    def __init__(self, alias_dict: Dict[str, str] = None):
        """
        1차 사전 기반 맵핑 (Rule-based: 비용 0)
        src/configs/entity_aliases.json 파일에서 동의어 맵핑 정보를 로드합니다.
        """
        if alias_dict is not None:
            self.alias_dict = alias_dict
        else:
            self.alias_dict = self._load_default_aliases()

    def _load_default_aliases(self) -> Dict[str, str]:
        import json
        import os
        
        # 파일 경로 설정 (src/configs/entity_aliases.json)
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        config_path = os.path.join(base_dir, "configs", "entity_aliases.json")
        
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"⚠️ Alias 설정을 불러오는데 실패했습니다: {e}")
                return {}
        else:
            print(f"⚠️ Alias 설정 파일({config_path})을 찾을 수 없습니다.")
            return {}


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
