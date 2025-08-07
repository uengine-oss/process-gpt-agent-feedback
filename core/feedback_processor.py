import json
import os
from typing import Dict, List
from langchain_openai import ChatOpenAI
from mem0 import Memory
from utils.logger import log, handle_error
from dotenv import load_dotenv

# ============================================================================
# 설정 및 초기화
# ============================================================================

load_dotenv()

# 데이터베이스 연결 정보
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")

if not all([DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, DB_NAME]):
    raise ValueError("❌ DB 연결 환경 변수가 설정되지 않았습니다. .env 파일을 확인해주세요.")

CONNECTION_STRING = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# ============================================================================
# 유틸리티 함수
# ============================================================================

def clean_json_response(content: str) -> str:
    """LLM 응답에서 백틱과 json 키워드 제거"""
    # ```json과 ``` 제거
    content = content.replace("```json", "").replace("```", "")
    return content.strip()

# ============================================================================
# 피드백 매칭 프롬프트
# ============================================================================

async def match_feedback_to_agents(feedback: str, agents: List[Dict], task_description: str = "") -> Dict:
    """AI를 사용해 피드백을 각 에이전트에 매칭"""
    
    llm = ChatOpenAI(
        model="gpt-4o-mini",
        temperature=0.1,
        api_key=os.getenv("OPENAI_API_KEY")
    )
    
    agents_info = "\n".join([
        f"- 에이전트 ID: {agent['id']}, 이름: {agent['name']}, 역할: {agent['role']}, 목표: {agent['goal']}"
        for agent in agents
    ])
    
    prompt = f"""
다음 상황을 분석하여 각 에이전트에게 적절한 학습 피드백을 생성해주세요.

**작업 지시사항:**
{task_description}

**사용자 피드백 (시간순):**
{feedback}

**에이전트 목록:**
{agents_info}

**상황 설명:**
에이전트들이 위의 작업지시사항에 따라 작업을 수행했지만, 사용자가 여러 차례에 걸쳐 피드백을 제공했습니다. 
피드백은 시간순으로 제공되었으며, 가장 최근(마지막) 피드백이 가장 중요하지만 이전 피드백들의 내용도 모두 반영되어야 합니다.

**피드백 처리 방식:**
- **가장 최신(time이 늦은) 피드백을 최우선으로 반영**
- 이전 피드백들은 상황 맥락을 이해하기 위한 참고 정보로만 활용
- **시간 흐름을 파악하여 피드백들 간의 연결고리와 문맥을 이해**
- 최신 피드백의 요구사항이 이전과 다르면 최신 것을 따라야 함
- 최신 피드백에서 요구하는 정확한 액션을 명확히 파악
- **자연스럽고 통합된 하나의 완전한 피드백으로 작성**
- 최대 2500자까지 허용하여 상세히 작성

**중요한 상황별 처리:**
- 이전에 저장을 했는데 잘못 저장되었다면 → **수정**이 필요 (다시 저장하면 안됨)
- 이전에 조회만 했는데 저장이 필요하다면 → **저장**이 필요
- 최신 피드백에서 명시한 요구사항이 절대 우선

**통합 예시:**
1번 피드백: "정보 저장을 요청했는데 조회만 했다"
2번 피드백: "정보 저장을 하긴 했으나 잘못 저장되어서 수정을 요청"
→ 통합 결과: "이전에 저장된 정보가 잘못되었으므로, 올바른 정보로 수정이 필요하다"

**지시사항:**
1. 시간 순서대로 피드백을 분석하여 상황의 전체적인 흐름을 파악하세요
2. **최신 피드백의 요구사항을 중심으로** 완전한 해결책을 제시하세요
3. 이전 피드백들의 맥락을 자연스럽게 녹여서 통합된 하나의 피드백으로 작성하세요
4. 에이전트가 **처음부터 끝까지 완벽하게 수행할 수 있는 구체적이고 실행 가능한 가이드**를 제공하세요

**응답 형식:**
- 추가 설명 없이 오직 아래 JSON 구조로만 응답하세요
- 마크다운 코드블록(```)이나 기타 텍스트는 포함하지 마세요
- JSON 객체만 출력하세요

{{
  "agent_feedbacks": [
    {{
      "agent_id": "에이전트_ID",
      "agent_name": "에이전트_이름", 
      "specific_feedback": "시간순 피드백들을 통합한 자연스러운 학습 가이드"
    }}
  ]
}}
"""

    try:
        response = await llm.ainvoke(prompt)
        
        # 응답 내용 정리 (백틱 제거)
        cleaned_content = clean_json_response(response.content)
        
        log(f"📤 LLM 전체 응답: {cleaned_content}")
        
        # 피드백 내용이 있다면 전체 출력
        parsed_result = json.loads(cleaned_content)
        if parsed_result.get("agent_feedbacks"):
            for feedback in parsed_result["agent_feedbacks"]:
                log(f"📝 에이전트 '{feedback.get('agent_name', 'Unknown')}' 피드백:")
                log(f"   {feedback.get('specific_feedback', 'No feedback')}")
        
        return parsed_result
    except json.JSONDecodeError as e:
        log(f"❌ JSON 파싱 실패 - 응답: {response.content if 'response' in locals() else 'None'}")
        handle_error("피드백매칭 JSON 파싱", f"응답 파싱 실패: {e}")
        return {"agent_feedbacks": []}
    except Exception as e:
        handle_error("피드백매칭", e)
        return {"agent_feedbacks": []}

# ============================================================================
# Mem0 학습
# ============================================================================

async def save_to_mem0(agent_feedbacks: List[Dict]):
    """에이전트별 피드백을 Mem0에 저장 - Supabase 연결"""
    try:
        # Supabase 기반 Memory 인스턴스 초기화
        config = {
            "vector_store": {
                "provider": "supabase",
                "config": {
                    "connection_string": CONNECTION_STRING,
                    "collection_name": "memories",
                    "index_method": "hnsw",
                    "index_measure": "cosine_distance"
                }
            }
        }
        memory = Memory.from_config(config_dict=config)
        
        for feedback_data in agent_feedbacks:
            agent_id = feedback_data['agent_id']
            agent_name = feedback_data['agent_name']
            specific_feedback = feedback_data['specific_feedback']
            
            # 에이전트별로 학습 데이터 저장 - "피드백: 내용" 형식
            memory.add(
                messages=[{
                    "role": "user",
                    "content": f"{specific_feedback}"
                }],
                user_id=agent_id,
                metadata={
                    "data_type": "feedback"
                },
                infer=False  # 추론 없이 원본 그대로 저장
            )
            
            log(f"Mem0 학습 완료 (Supabase): 에이전트 {agent_name} (ID: {agent_id})")
            
    except Exception as e:
        handle_error("Mem0학습", e)

