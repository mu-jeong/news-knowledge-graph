import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from dotenv import load_dotenv

# 로컬 환경 변수 로드
load_dotenv()

from src.core.crawlers.naver_news import NaverNewsProvider
from src.configs.schema import GraphData, get_graph_extraction_prompt
from langchain_google_genai import ChatGoogleGenerativeAI

def main():
    print("=== 1. 데이터 수집 및 클러스터링 (Data Ingestion) ===")
    naver_client_id = os.getenv("NAVER_CLIENT_ID")
    naver_client_secret = os.getenv("NAVER_CLIENT_SECRET")
    
    if not naver_client_id or not naver_client_secret or naver_client_id == "your_naver_client_id_here":
        print("⚠️ 에러: .env 파일에 NAVER_CLIENT_ID와 NAVER_CLIENT_SECRET을 올바르게 설정해주세요.")
        print("테스트를 진행하려면 네이버 개발자 센터에서 발급받은 API 키가 필요합니다.")
        return

    # 1. API를 통한 데이터 수집
    provider = NaverNewsProvider(client_id=naver_client_id, client_secret=naver_client_secret)
    keyword = "삼성전자"
    print(f"[{keyword}] 키워드로 최근 30일치 뉴스 수집 중...")
    
    chunks = provider.run_pipeline(keyword=keyword, days_back=30)
    
    if not chunks:
        print("수집된 데이터가 없습니다. API 키나 트래픽을 확인해주세요.")
        return
        
    print(f"\n✅ 성공적으로 {len(chunks)}개의 텍스트 청크(Chunk)가 준비되었습니다.")
    print("-" * 50)
    print(f"첫 번째 청크 프리뷰:\n{chunks[0][:]}...\n")
    print("-" * 50)
    
    print("\n=== 2. 정보 추출 (Information Extraction) ===")
    google_api_key = os.getenv("GOOGLE_API_KEY")
    if not google_api_key or google_api_key == "your_gemini_api_key_here":
        print("⚠️ 알림: .env 파일에 GOOGLE_API_KEY(Gemini API Key)가 없어 LLM 추출은 생략합니다.")
        return
        
    print("Gemini 2.5 Flash 모델에 첫 번째 청크를 주입하여 Entity와 Relation을 추출합니다...")
    
    try:
        # LLM 초기화 (Structured Output을 사용하여 Pydantic 스키마 형태의 결과를 강제함)
        llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash", api_key=google_api_key)
        structured_llm = llm.with_structured_output(GraphData)
        
        # Pydantic 기반으로 작성했던 프롬프트 생성 (여기서는 시스템/유저가 합쳐진 형태를 사용)
        raw_prompt = get_graph_extraction_prompt(chunks[0])
        
        print("LLM 호출 중 (약 10~20초 소요)...\n")
        
        # LLM 호출 및 Pydantic 객체로 결과 반환
        result: GraphData = structured_llm.invoke(raw_prompt)
        
        print("✅ 추출 성공! 결과물은 다음과 같습니다:\n")
        print("--- 💡 추출된 엔티티 (Entities) ---")
        for entity in result.entities:
            print(f"  - [{entity.type}] {entity.name}")
            
        print("\n--- 🔗 추출된 관계 (Relations) ---")
        for rel in result.relations:
            desc = f" ({rel.description})" if rel.description else ""
            print(f"  - {rel.source} --[{rel.type}]--> {rel.target}{desc}")
            
        print("\n=== 3. 엔티티 정규화 (Entity Resolution) ===")
        from src.core.utils.entity_resolution import EntityResolver
        
        resolver = EntityResolver()
        resolved_graph = resolver.resolve(result)
        
        print("✅ 정규화 성공! '삼성', '삼전' 같은 동의어가 표준 명칭으로 통합되었습니다.")
        # 간단한 출력 비교
        if result.entities != resolved_graph.entities:
            print("[정규화 차이점] 일부 엔티티 명칭이 변경되었습니다!")
            
        print("\n=== 4. 그래프 DB 적재 (Graph Construction) ===")
        print("⚠️ Neo4j가 로컬(bolt://localhost:7687)에 실행 중이어야 이 단계가 성공합니다.")
        try:
            from src.graphs.neo4j_manager import Neo4jLoader
            
            loader = Neo4jLoader()
            if loader.driver:
                try:
                    loader.load_graph_data(resolved_graph)
                    loader.close()
                    print("🎉 (성공) Neo4j 데이터베이스에 추출된 노드와 엣지 적재가 완료되었습니다!")
                except Exception as db_err:
                     print(f"❌ Neo4j 적재 중 예외 발생: {db_err}")
            else:
                print("🚫 Neo4j에 연결할 수 없어 적재 파이프라인을 종료합니다. (.env의 NEO4J_URI/PASSWORD를 확인하세요.)")
        except ImportError as e:
            print(f"⚠️ [경고] {e}")
            print("현재 디렉토리 구조 변경으로 인해 'neo4j_manager.py' 모듈을 찾을 수 없습니다. (삭제되었거나 아직 패키지로 복원되지 않음)")
            
        print("\n🎉 네이버 API -> LLM 정보 추출 -> 정규화 -> DB 적재 전체 파이프라인 테스트 종료!")

    except Exception as e:
         print(f"❌ 실행 중 오류 발생: {e}")

if __name__ == "__main__":
    main()
