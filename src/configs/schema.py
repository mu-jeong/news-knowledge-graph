import json
from typing import List, Optional
from pydantic import BaseModel, Field

# ----------------------------------------------------
# 1. Pydantic 스키마 정의 (엄격한 JSON 구조 강제)
# ----------------------------------------------------
class Entity(BaseModel):
    name: str = Field(description="엔티티의 이름 (예: 기업명, 기술명, 국가명, 인물 등)")
    type: str = Field(description="엔티티 카테고리 (예: Company, Technology, Person, Country 등)")

class Relation(BaseModel):
    source: str = Field(description="관계를 시작하는 엔티티의 이름 (출발점)")
    target: str = Field(description="관계의 대상이 되는 엔티티의 이름 (도착점)")
    type: str = Field(description="관계의 특징 (예: SUPPLIES_TO, COMPETES_WITH, INVESTS_IN, PARTNERS_WITH 등)")
    description: Optional[str] = Field(description="관계에 대한 맥락 또는 부연 설명 (선택 사항)", default=None)
    source_article: str = Field(description="이 관계가 추출된 원본 뉴스의 제목 (제공된 텍스트 중 '제목:' 에 해당하는 값을 반드시 가져올 것)")
    source_url: str = Field(description="원본 뉴스의 링크 (제공된 텍스트 중 '링크:' 에 해당하는 값을 반드시 가져올 것)")

class GraphData(BaseModel):
    entities: List[Entity] = Field(description="추출된 엔티티 목록", default_factory=list)
    relations: List[Relation] = Field(description="엔티티 간의 관계 목록", default_factory=list)

# ----------------------------------------------------
# 2. 프롬프트 생성용 유틸리티 함수
# ----------------------------------------------------
def get_graph_extraction_prompt(text_chunk: str) -> str:
    """
    LLM에 전달할 시스템/사용자 결합 프롬프트를 생성합니다.
    """
    # Pydantic 모델을 JSON 스키마로 변환하여 시스템 프롬프트에 제공
    schema_json = json.dumps(GraphData.model_json_schema(), ensure_ascii=False, indent=2)
    
    system_prompt = (
        "당신은 금융/IT 뉴스 텍스트에서 주요 주체(Entity)와 그들 간의 관계(Relation)를 추출하여 "
        "지식 그래프(Knowledge Graph)를 구축하는 데이터 분석 및 엔지니어링 전문가입니다.\n\n"
        
        "다음 지침을 매우 엄격하게 따르십시오:\n"
        "1. 문맥에서 명확하게 나타난 사실에 기반한 관계만 추출하세요.\n"
        "2. 절대 존재하지 않는 정보를 지어내지 마세요(Zero Hallucination).\n"
        "3. 엔티티 타입(Type)은 가급적 'Company', 'Technology', 'Person', 'Country', 'Product' 중에서 선택하세요.\n"
        "4. 관계 타입(Type)은 모두 대문자와 언더스코어를 사용하여 직관적으로 표현하시되, 동의어 파편화를 막기 위해 "
        "[가능한 한 아래의 주요 경제/주식 표준 관계 목록 중 하나를 선택]하십시오.\n"
        "   - [투자/지분]: INVESTS_IN(투자), ACQUIRES(인수), MERGES_WITH(합병), OWNS_STAKE_IN(지분보유)\n"
        "   - [제휴/공급]: PARTNERS_WITH(제휴/협력), SUPPLIES_TO(공급), JOINT_VENTURE_WITH(합작)\n"
        "   - [경쟁/시장]: COMPETES_WITH(경쟁), LEADS_MARKET(시장선도/점유율1위)\n"
        "   - [제품/기술]: DEVELOPS(개발), LAUNCHES(출시), PATENTED(특허)\n"
        "   - [경영/인물]: CEO_OF(최고경영자), FOUNDED(설립), BOARD_MEMBER_OF(이사회/임원)\n"
        "   - [국가/정책]: REGULATES(규제), SUBSIDIZES(보조금지원), PENALIZES(제재/벌금과징금)\n"
        "   - 위 목록에 적합한 것이 도저히 없을 경우에만 새로운 관계 타입을 생성하되, 다음 명명 규칙을 엄수하십시오:\n"
        "     * 반드시 2~3단어 이내의 간결한 영문 대문자와 언더스코어(_) 조합으로 작성할 것 (예: SUED_BY, SPONSORS)\n"
        "     * 관계명에 구체적인 수치, 날짜, 고유명사를 포함하지 말 것 (세부 내용은 description 필드에 작성)\n"
        "     * 방향성이 명확히 드러나도록 동사 형태(능동/수동)를 사용할 것\n"
        "5. 출력 포맷은 반드시 아래 제공된 JSON Schema를 만족하는 완벽한 JSON 형식이어야 합니다. "
        "Markdown 코드 블록 기호(예: ```json ... ```)를 제외한 순수 JSON만 반환하거나, "
        "또는 Structure Output 기능을 사용할 경우 JSON 스키마만 엄격히 준수하세요.\n"
        "6. 추출하는 [모든 관계(Relation)]에 대해, 본문에 명시된 원본 뉴스의 '제목'과 '링크'를 반드시 찾아내어 `source_article`과 `source_url` 속성에 채워넣어야 합니다. 절대 비워두지 마세요.\n\n"
        
        f"----- JSON SCHEMA -----\n{schema_json}\n-----------------------\n"
    )
    
    user_prompt = (
        "다음 주간 뉴스 요약문(청크)에서 지식 그래프 데이터를 추출해 주십시오:\n\n"
        f"\"\"\"\n{text_chunk}\n\"\"\""
    )
    
    return system_prompt + "\n\n" + user_prompt

# 간단한 테스트 블럭
if __name__ == "__main__":
    sample_chunk = "제목: 엔비디아, TSMC와 차세대 AI 칩 생산 파트너십 강화\n내용: 엔비디아가 삼성전자를 제치고 TSMC에 H200 및 블랙웰 생산의 대부분을 위탁하기로 했다."
    prompt = get_graph_extraction_prompt(sample_chunk)
    # print(prompt)  # 직접 실행 시 어떻게 프롬프트가 구성되는지 확인할 수 있습니다.
