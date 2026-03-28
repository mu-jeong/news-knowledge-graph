import math
import os
import re
from typing import Dict, List, Optional, Tuple

from langchain_google_genai import GoogleGenerativeAIEmbeddings

from src.configs.schema import GraphData, Entity, Relation, ENTITY_TYPES, RELATION_TYPES
from src.configs.settings import (
    EMBEDDING_MODEL,
    ENABLE_ENTITY_SEMANTIC_MERGE,
    ENTITY_SEMANTIC_MERGE_THRESHOLD,
)

class EntityResolver:
    """
    모듈 3. 엔티티 정규화 (Entity Resolution)
    지식 그래프가 파편화되는 것을 막기 위해 여러 동의어를 하나의 표준 명칭으로 통합합니다.
    """
    TYPE_PRIORITY = {
        "Company": 6,
        "Product": 5,
        "Technology": 4,
        "Industry": 3,
        "RiskFactor": 2,
        "MacroEvent": 1,
        "Entity": 0,
    }

    RELATION_NORMALIZATION = {
        "VULNERABLE_TO": "EXPOSED_TO",
        "AFFECTED_BY": "EXPOSED_TO",
        "IMPACTED_BY": "EXPOSED_TO",
        "EXPOSED": "EXPOSED_TO",
        "EXPOSED_TO_RISK": "EXPOSED_TO",
        "IMPACTS": "AFFECTS",
        "AFFECT": "AFFECTS",
        "INFLUENCES": "AFFECTS",
        "DRIVES": "AFFECTS",
        "USED_IN": "USES",
        "USED_BY": "USES",
        "POWERED_BY": "USES",
        "ENABLED_BY": "USES",
        "USES_TECH": "USES",
        "BELONGS_IN": "BELONGS_TO",
        "BELONG_TO": "BELONGS_TO",
        "BELONGS_WITHIN": "BELONGS_TO",
        "IS_A": "PART_OF",
        "KIND_OF": "PART_OF",
        "CATEGORY_OF": "PART_OF",
        "SUBCATEGORY_OF": "PART_OF",
        "PARTOF": "PART_OF",
        "WITHIN": "PART_OF",
        "RELEASES": "RELEASED",
        "BENEFITS": "BENEFITS_FROM",
        "BENEFITED_FROM": "BENEFITS_FROM",
        "SUPPORTED_BY": "BENEFITS_FROM",
        "RELATES_TO": "RELATED_TO",
        "ASSOCIATED_WITH": "RELATED_TO",
        "CONNECTED_TO": "RELATED_TO",
    }

    GENERIC_ENTITY_EXACT = {
        "기술 기업",
        "플랫폼 기업",
        "서비스 업체",
        "제조 업체",
        "주요 기업",
        "주요 업체",
        "관련 업계",
        "관련 산업",
    }

    GENERIC_ENTITY_SUFFIXES = (
        "기업",
        "업체",
        "업계",
        "산업",
        "분야",
        "영역",
        "섹터",
        "시장",
        "생태계",
        "밸류체인",
        "커뮤니티",
    )

    GENERIC_ENTITY_PREFIXES = (
        "주요 ",
        "글로벌 ",
        "국내 ",
        "해외 ",
        "지역 ",
        "현지 ",
        "일부 ",
        "복수의 ",
    )

    GENERIC_ENTITY_TOKENS = {
        "기술",
        "플랫폼",
        "서비스",
        "제조",
        "기업",
        "업체",
        "업계",
        "산업",
        "분야",
        "영역",
        "섹터",
        "시장",
        "생태계",
        "밸류체인",
        "주요",
        "글로벌",
        "국내",
        "해외",
        "지역",
        "현지",
        "일부",
        "복수의",
        "커뮤니티",
    }

    INSTITUTION_SUFFIXES = (
        "부",
        "청",
        "처",
        "위원회",
        "협회",
        "조합",
        "센터",
        "기관",
    )

    ABSTRACT_ENTITY_SUFFIXES = (
        "기대감",
        "전망",
        "가능성",
        "모멘텀",
        "효과",
        "영향",
        "부담",
        "대책",
        "기조",
        "강화",
        "확대",
        "감소",
        "증가",
        "하락",
        "상승",
        "수요",
        "공급",
        "전환",
        "회복",
    )

    ENGLISH_DESCRIPTIVE_TOKENS = {
        "reduced",
        "increase",
        "decrease",
        "demand",
        "supply",
        "growth",
        "decline",
        "expansion",
        "expectation",
        "pressure",
        "outlook",
        "recovery",
    }

    TYPE_KEYWORDS = {
        "Technology": (
            "AI",
            "인공지능",
            "LLM",
            "소프트웨어",
            "플랫폼",
            "기술",
            "오픈소스",
            "모델",
            "시스템",
            "프레임워크",
            "프로토콜",
            "알고리즘",
            "자동화",
            "디지털",
        ),
        "Industry": (
            "산업",
            "업계",
            "생태계",
            "공급망",
            "밸류체인",
            "섹터",
            "분야",
            "시장",
        ),
        "Product": (
            "제품",
            "서비스",
            "기기",
            "장치",
            "솔루션",
            "시리즈",
            "모델",
            "버전",
            "에디션",
            "패키지",
        ),
        "RiskFactor": (
            "리스크",
            "우려",
            "압력",
            "충돌",
            "격화",
            "캐즘",
            "인하",
            "둔화",
            "불확실성",
            "악화",
            "쇼크",
            "논란",
        ),
        "MacroEvent": (
            "인플레이션",
            "금리",
            "환율",
            "경기",
            "경제",
            "규제",
            "정책",
            "관세",
            "원자재",
        ),
    }

    def __init__(self, alias_dict: Dict[str, str] = None):
        """
        1차 사전 기반 맵핑 (Rule-based: 비용 0)
        src/configs/entity_aliases.json 파일에서 동의어 맵핑 정보를 로드합니다.
        """
        self.alias_dict = alias_dict if alias_dict is not None else self._load_default_aliases()
        self.taxonomy = self._load_taxonomy()
        self.alias_dict.update(self._build_taxonomy_aliases())
        self.enable_semantic_merge = ENABLE_ENTITY_SEMANTIC_MERGE and bool(os.getenv("GOOGLE_API_KEY"))
        self.semantic_merge_threshold = ENTITY_SEMANTIC_MERGE_THRESHOLD
        self._embedder: Optional[GoogleGenerativeAIEmbeddings] = None
        self._canonical_names: List[str] = list(self.taxonomy.keys())
        self._canonical_embeddings: Optional[List[List[float]]] = None
        self._semantic_cache: Dict[str, str] = {}
        self._known_types = set(ENTITY_TYPES)
        self._known_relations = set(RELATION_TYPES)

    def _load_default_aliases(self) -> Dict[str, str]:
        import json
        
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

    def _load_taxonomy(self) -> Dict[str, Dict]:
        import json

        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        config_path = os.path.join(base_dir, "configs", "entity_taxonomy.json")

        if not os.path.exists(config_path):
            return {}

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ Taxonomy 설정을 불러오는데 실패했습니다: {e}")
            return {}

    def _build_taxonomy_aliases(self) -> Dict[str, str]:
        alias_map: Dict[str, str] = {}
        for canonical_name, spec in self.taxonomy.items():
            alias_map[canonical_name] = canonical_name
            for alias in spec.get("aliases", []):
                alias_map[alias] = canonical_name
        return alias_map

    def _get_embedder(self) -> Optional[GoogleGenerativeAIEmbeddings]:
        if not self.enable_semantic_merge:
            return None
        if self._embedder is None:
            self._embedder = GoogleGenerativeAIEmbeddings(
                model=EMBEDDING_MODEL,
                google_api_key=os.getenv("GOOGLE_API_KEY"),
            )
        return self._embedder

    def _ensure_canonical_embeddings(self) -> None:
        if self._canonical_embeddings is not None or not self._canonical_names:
            return
        embedder = self._get_embedder()
        if embedder is None:
            return
        try:
            self._canonical_embeddings = embedder.embed_documents(self._canonical_names)
        except Exception as e:
            print(f"⚠️ Canonical 엔티티 임베딩 생성 실패: {e}")
            self._canonical_embeddings = []
            self.enable_semantic_merge = False

    def _clean_name(self, name: Optional[str]) -> str:
        if not name:
            return ""
        return " ".join(str(name).replace("\n", " ").strip().split())

    def _tokenize_name(self, name: str) -> List[str]:
        return [token for token in re.split(r"[\s/·(),\-]+", name) if token]

    def _is_english_descriptive_phrase(self, name: str) -> bool:
        if re.search(r"[가-힣]", name):
            return False
        tokens = self._tokenize_name(name.lower())
        if len(tokens) < 2:
            return False
        if any(token in self.ENGLISH_DESCRIPTIVE_TOKENS for token in tokens):
            return True
        return all(token.isalpha() and token.islower() for token in tokens)

    def _is_descriptive_abstract_entity(self, name: str, declared_type: Optional[str]) -> bool:
        if declared_type in {"RiskFactor", "MacroEvent"}:
            return False
        tokens = self._tokenize_name(name)
        if len(tokens) < 2:
            return False
        if tokens[-1] in self.ABSTRACT_ENTITY_SUFFIXES:
            return True
        return False

    def _is_generic_category_entity(self, name: str) -> bool:
        if not name or name in self.taxonomy:
            return False
        if name in self.GENERIC_ENTITY_EXACT:
            return True

        if any(name.startswith(prefix) for prefix in self.GENERIC_ENTITY_PREFIXES) and any(
            name.endswith(suffix) for suffix in self.GENERIC_ENTITY_SUFFIXES
        ):
            return True

        tokens = self._tokenize_name(name)
        if 1 < len(tokens) <= 4 and all(token in self.GENERIC_ENTITY_TOKENS for token in tokens):
            return True

        if len(tokens) <= 3 and any(name.endswith(suffix) for suffix in self.GENERIC_ENTITY_SUFFIXES):
            non_generic_tokens = [token for token in tokens if token not in self.GENERIC_ENTITY_TOKENS]
            if not non_generic_tokens:
                return True

        return False

    def _is_low_quality_entity(self, name: str, declared_type: Optional[str] = None) -> bool:
        if not name or len(name) < 2:
            return True
        low_signal_suffixes = ("입니다", "했다", "하는", "하며", "관련", "대한", "위한")
        if name.endswith(low_signal_suffixes):
            return True
        if name.count(" ") >= 5:
            return True
        if self._is_english_descriptive_phrase(name):
            return True
        if self._is_descriptive_abstract_entity(name, declared_type):
            return True
        if self._is_generic_category_entity(name):
            return True
        return False

    def _infer_type(self, name: str, declared_type: Optional[str]) -> str:
        if name in self.taxonomy:
            return self.taxonomy[name].get("type", "Entity")

        if name.endswith(self.INSTITUTION_SUFFIXES):
            return "Entity"

        scores = {entity_type: 0 for entity_type in self.TYPE_KEYWORDS}
        for entity_type, keywords in self.TYPE_KEYWORDS.items():
            scores[entity_type] = sum(1 for keyword in keywords if keyword in name)

        best_type, best_score = max(scores.items(), key=lambda item: item[1])
        if best_score >= 1:
            if declared_type in self._known_types and declared_type != "Entity":
                declared_score = scores.get(declared_type, 0)
                if declared_score == best_score:
                    return declared_type
            return best_type

        if declared_type in self._known_types:
            return declared_type
        return "Entity"

    def _normalize_relation_type(self, relation_type: Optional[str]) -> str:
        rel = self._clean_name(relation_type).replace("-", "_").replace(" ", "_").upper()
        if not rel:
            return "RELATED_TO"
        rel = self.RELATION_NORMALIZATION.get(rel, rel)
        if rel not in self._known_relations:
            return "RELATED_TO"
        return rel

    def _cosine_similarity(self, a: List[float], b: List[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(y * y for y in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def _semantic_match(self, name: str, declared_type: Optional[str]) -> Optional[str]:
        if not self.enable_semantic_merge or not name:
            return None
        if name in self._semantic_cache:
            return self._semantic_cache[name]

        self._ensure_canonical_embeddings()
        embedder = self._get_embedder()
        if embedder is None or not self._canonical_embeddings:
            return None

        try:
            query_embedding = embedder.embed_query(name)
        except Exception as e:
            print(f"⚠️ 엔티티 의미 병합 임베딩 생성 실패 ({name}): {e}")
            return None

        inferred_type = self._infer_type(name, declared_type)
        best_match = None
        best_score = -1.0

        for canonical_name, canonical_embedding in zip(self._canonical_names, self._canonical_embeddings):
            canonical_type = self.taxonomy.get(canonical_name, {}).get("type", "Entity")
            if inferred_type != "Entity" and canonical_type not in (inferred_type, "Entity"):
                continue
            score = self._cosine_similarity(query_embedding, canonical_embedding)
            if score > best_score:
                best_score = score
                best_match = canonical_name

        if best_match and best_score >= self.semantic_merge_threshold:
            self._semantic_cache[name] = best_match
            return best_match
        return None

    def _resolve_name(self, name: Optional[str], declared_type: Optional[str] = None) -> str:
        clean_name = self._clean_name(name)
        if not clean_name:
            return ""
        if clean_name in self.alias_dict:
            return self.alias_dict[clean_name]
        semantic_match = self._semantic_match(clean_name, declared_type)
        return semantic_match or clean_name

    def _prefer_type(self, left: str, right: str) -> str:
        return left if self.TYPE_PRIORITY.get(left, 0) >= self.TYPE_PRIORITY.get(right, 0) else right

    def _build_taxonomy_extensions(self, entity_map: Dict[str, Entity]) -> Tuple[List[Entity], List[Relation]]:
        taxonomy_entities: Dict[str, Entity] = {}
        taxonomy_relations: List[Relation] = []
        relation_keys = set()
        queue = list(entity_map.keys())
        visited = set()

        while queue:
            entity_name = queue.pop(0)
            if entity_name in visited:
                continue
            visited.add(entity_name)

            spec = self.taxonomy.get(entity_name)
            if not spec:
                continue

            for parent in spec.get("parents", []):
                parent_name = parent["name"]
                parent_type = parent.get("type", "Entity")
                relation_type = self._normalize_relation_type(parent.get("relation", "PART_OF"))

                existing = entity_map.get(parent_name) or taxonomy_entities.get(parent_name)
                if existing:
                    existing.type = self._prefer_type(existing.type, parent_type)
                else:
                    taxonomy_entities[parent_name] = Entity(name=parent_name, type=parent_type)

                relation_key = (entity_name, parent_name, relation_type, "taxonomy")
                if relation_key in relation_keys:
                    continue
                relation_keys.add(relation_key)
                taxonomy_relations.append(Relation(
                    source=entity_name,
                    target=parent_name,
                    type=relation_type,
                    description=f"{entity_name} is taxonomically linked to {parent_name}",
                    source_article=None,
                    source_url=None,
                    article_id=None,
                    provenance="taxonomy",
                ))
                queue.append(parent_name)

        return list(taxonomy_entities.values()), taxonomy_relations

    def resolve(self, graph_data: GraphData) -> GraphData:
        """추출된 GraphData의 엔티티 명칭(노드 및 엣지 연결고리)을 정규화합니다."""
        entity_map: Dict[str, Entity] = {}
        resolved_relations: List[Relation] = []
        name_cache: Dict[str, str] = {}
        
        # 1. 엔티티 정규화 (노드 이름 표준화)
        for entity in graph_data.entities:
            normalized_name = self._resolve_name(entity.name, entity.type)
            if self._is_low_quality_entity(normalized_name, entity.type):
                continue
            normalized_type = self._infer_type(normalized_name, entity.type)
            if normalized_name in entity_map:
                entity_map[normalized_name].type = self._prefer_type(entity_map[normalized_name].type, normalized_type)
            else:
                entity_map[normalized_name] = Entity(name=normalized_name, type=normalized_type)
            name_cache[entity.name] = normalized_name
            
        # 2. 관계 정규화 (엣지의 출발점/도착점 이름도 표준화된 이름에 맞춰 변경)
        relation_keys = set()
        for rel in graph_data.relations:
            normalized_source = name_cache.get(rel.source) or self._resolve_name(rel.source)
            normalized_target = name_cache.get(rel.target) or self._resolve_name(rel.target)
            if self._is_low_quality_entity(normalized_source) or self._is_low_quality_entity(normalized_target):
                continue
            normalized_type = self._normalize_relation_type(rel.type)

            if normalized_source not in entity_map:
                entity_map[normalized_source] = Entity(
                    name=normalized_source,
                    type=self._infer_type(normalized_source, None),
                )
            if normalized_target not in entity_map:
                entity_map[normalized_target] = Entity(
                    name=normalized_target,
                    type=self._infer_type(normalized_target, None),
                )

            relation_key = (
                normalized_source,
                normalized_target,
                normalized_type,
                getattr(rel, "source_url", None),
                getattr(rel, "article_id", None),
                "article",
            )
            if relation_key in relation_keys:
                continue
            relation_keys.add(relation_key)
            resolved_relations.append(Relation(
                source=normalized_source,
                target=normalized_target,
                type=normalized_type,
                description=rel.description,
                source_article=getattr(rel, "source_article", None),
                source_url=getattr(rel, "source_url", None),
                article_id=getattr(rel, 'article_id', None),
                provenance=getattr(rel, 'provenance', None) or "article",
            ))

        taxonomy_entities, taxonomy_relations = self._build_taxonomy_extensions(entity_map)
        for entity in taxonomy_entities:
            if entity.name in entity_map:
                entity_map[entity.name].type = self._prefer_type(entity_map[entity.name].type, entity.type)
            else:
                entity_map[entity.name] = entity

        for rel in taxonomy_relations:
            relation_key = (rel.source, rel.target, rel.type, rel.source_url, rel.article_id, rel.provenance)
            if relation_key not in relation_keys:
                relation_keys.add(relation_key)
                resolved_relations.append(rel)

        return GraphData(entities=list(entity_map.values()), relations=resolved_relations)
