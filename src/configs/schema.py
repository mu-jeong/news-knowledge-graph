import json
from typing import List, Optional
from pydantic import BaseModel, Field

ENTITY_TYPES = (
    "Company",
    "Industry",
    "MacroEvent",
    "Product",
    "Technology",
    "RiskFactor",
)

RELATION_TYPES = (
    "SUPPLIES_TO",
    "COMPETES_WITH",
    "BELONGS_TO",
    "PART_OF",
    "RELEASED",
    "USES",
    "EXPOSED_TO",
    "BENEFITS_FROM",
    "AFFECTS",
    "OWNS",
    "RELATED_TO",
    "MENTIONS",
)

# ----------------------------------------------------
# 1. Pydantic 스키마 정의 (엄격한 JSON 구조 강제)
# ----------------------------------------------------
class Entity(BaseModel):
    name: str = Field(description="엔티티의 이름 (예: 삼성전자, 반도체, 갤럭시 S24 등)")
    type: str = Field(description="엔티티 카테고리 (Company, Industry, MacroEvent, Product, Technology, RiskFactor 중 선택)")

class Relation(BaseModel):
    source: str = Field(description="관계를 시작하는 엔티티의 이름")
    target: str = Field(description="관계의 대상이 되는 엔티티의 이름")
    type: str = Field(description="관계 타입 (SUPPLIES_TO, COMPETES_WITH, BELONGS_TO, PART_OF, RELEASED, USES, EXPOSED_TO, BENEFITS_FROM, AFFECTS, OWNS, RELATED_TO, MENTIONS 등)")
    description: Optional[str] = Field(description="관계에 대한 구체적인 맥락", default=None)
    source_article: Optional[str] = Field(description="추출 원본 뉴스 제목", default=None)
    source_url: Optional[str] = Field(description="추출 원본 링크", default=None)
    article_id: Optional[str] = Field(description="뉴스 텍스트 내에서 제공된 [기사 ID] (예: Article_1)", default=None)
    provenance: Optional[str] = Field(description="관계의 출처 유형 (article 또는 taxonomy)", default=None)

class GraphData(BaseModel):
    entities: List[Entity] = Field(description="추출된 엔티티 목록", default_factory=list)
    relations: List[Relation] = Field(description="엔티티 간의 관계 목록", default_factory=list)

# ----------------------------------------------------
# 2. 프롬프트 생성용 유틸리티 함수
# ----------------------------------------------------
def get_graph_extraction_prompt(batch_text: str) -> str:
    """
    LLM에 전달할 시스템/사용자 결합 프롬프트를 생성합니다.
    """
    # Pydantic 모델을 JSON 스키마로 변환하여 시스템 프롬프트에 제공
    schema_json = json.dumps(GraphData.model_json_schema(), ensure_ascii=False, indent=2)
    
    system_prompt = (
        "당신은 뉴스 데이터를 분석하여 정밀한 지식 그래프를 구축하는 금융/경제 AI 전문가입니다. "
        "당신에게는 10개 내외의 뉴스 기사가 포함된 [배치]가 제공됩니다.\n\n"
        
        "1. [분석 방식]:\n"
        "   - 각 기사는 [기사 ID: Article_N] 형태로 구분되어 있습니다.\n"
        "   - 각 기사를 독립적으로 분석하여 그 안에서 발견되는 엔티티와 관계를 추출하십시오.\n"
        "   - 관계(Relation)를 추출할 때, 해당 관계가 발견된 기사의 [기사 ID]를 `article_id` 필드에 반드시 기입하십시오.\n\n"

        "2. [엔티티 정의]:\n"
        "   - Company: 기업 (삼성전자, TSMC 등)\n"
        "   - Industry: 산업/섹터 (반도체, 파운드리, 2차전지 등)\n"
        "   - MacroEvent: 거시경제 요인 (금리 인상, 원자재 가격 상승 등)\n"
        "   - Product: 제품/서비스 (갤럭시 S24, HBM3E, AI PC 등)\n"
        "   - Technology: 기술/기술 영역 (AI, LLM, 메모리 압축 기술 등)\n"
        "   - RiskFactor: 리스크/압박 요인 (물가 상승 압력, 성장 하방 리스크 등)\n\n"
        
        "3. [관계 정의]:\n"
        "   - [Company] -SUPPLIES_TO-> [Company] (공급)\n"
        "   - [Company] -COMPETES_WITH-> [Company] (경쟁)\n"
        "   - [Company] -BELONGS_TO-> [Industry] (소속 산업)\n"
        "   - [Entity] -PART_OF-> [Entity] (상하위 범주/구성 관계)\n"
        "   - [Company] -RELEASED-> [Product] (제품 출시)\n"
        "   - [Product/Company] -USES-> [Technology] (기술 활용)\n"
        "   - [Company/Product/Industry] -EXPOSED_TO-> [RiskFactor/MacroEvent] (리스크 노출)\n"
        "   - [Company/Product/Industry] -BENEFITS_FROM-> [MacroEvent/Technology] (수혜)\n"
        "   - [MacroEvent/RiskFactor/Technology] -AFFECTS-> [Entity] (영향)\n\n"
        
        "4. [주의 사항]:\n"
        "   - 답변(Generation) 시 사용될 근거가 명확하도록 `source_url`과 `article_id`를 정확히 매칭하십시오.\n"
        "   - 기사 기반으로 확인되지 않은 관계를 임의로 만들지 마십시오.\n"
        "   - 출력값은 반드시 아래 JSON Schema를 완벽히 준수해야 합니다.\n\n"
        
        f"----- JSON SCHEMA -----\n{schema_json}\n-----------------------\n"
    )
    
    user_prompt = (
        "다음 10개 기사 묶음에서 지식 그래프 데이터를 정밀하게 추출해 주십시오. "
        "각 문장의 출처(기사 ID)를 정확히 기록하는 것이 최우선 과제입니다:\n\n"
        f"\"\"\"\n{batch_text}\n\"\"\""
    )
    
    return system_prompt + "\n\n" + user_prompt

# 간단한 테스트 블럭
if __name__ == "__main__":
    sample_batch = "제목: 엔비디아, TSMC와 차세대 AI 칩 생산 파트너십 강화\n내용: 엔비디아가 삼성전자를 제치고 TSMC에 H200 및 블랙웰 생산의 대부분을 위탁하기로 했다."
    prompt = get_graph_extraction_prompt(sample_batch)
    # print(prompt)  # 직접 실행 시 어떻게 프롬프트가 구성되는지 확인할 수 있습니다.
