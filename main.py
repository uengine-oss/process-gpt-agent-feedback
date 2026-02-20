# -*- coding: utf-8 -*-

# ============================================================================
# 기본 환경 설정
# ============================================================================
import sys
import io
import os
import builtins
import warnings
import asyncio
from contextlib import asynccontextmanager
from dotenv import load_dotenv

# 환경 설정
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
os.environ["PYTHONIOENCODING"] = "utf-8"
load_dotenv(override=True)

# 전역 print 함수 설정 (flush=True 기본값)
_orig_print = builtins.print
def print(*args, **kwargs):
    if 'flush' not in kwargs:
        kwargs['flush'] = True
    _orig_print(*args, **kwargs)
builtins.print = print

# 경고 메시지 필터링
warnings.filterwarnings("ignore", category=DeprecationWarning)

# ============================================================================
# FastAPI 애플리케이션 설정
# ============================================================================
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from core.polling_manager import start_feedback_polling, initialize_connections
from core.react_agent import process_agent_knowledge_setup_with_react
from core.database import _get_agent_by_id, upsert_agent_knowledge_setup_log
from utils.logger import log

@asynccontextmanager
async def lifespan(app: FastAPI):
    """애플리케이션 생명주기 관리"""
    log("서버 시작 - 연결 초기화 및 피드백 폴링 시작")
    initialize_connections()
    # 피드백 폴링 작업을 백그라운드 태스크로 시작
    # start_feedback_polling 내부에서 모든 예외를 처리하므로 예외가 발생해도 계속 실행됨
    polling_task = asyncio.create_task(start_feedback_polling(interval=7))
    yield
    # 서버 종료 시 폴링 작업 취소
    polling_task.cancel()
    try:
        await polling_task
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log(f"⚠️ 폴링 작업 종료 중 에러 (무시): {str(e)[:200]}...")
    log("서버 종료")

# FastAPI 앱 생성
app = FastAPI(
    title="Deep Research Server",
    version="1.0",
    description="Deep Research API Server",
    lifespan=lifespan
)

# CORS 설정
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================================
# API 엔드포인트
# ============================================================================

class AgentKnowledgeSetupRequest(BaseModel):
    """에이전트 초기 지식 셋팅 요청 모델"""
    agent_id: str
    goal: Optional[str] = None
    persona: Optional[str] = None


@app.post("/setup-agent-knowledge")
async def setup_agent_knowledge(request: AgentKnowledgeSetupRequest):
    """
    에이전트 초기 지식 셋팅 API 엔드포인트
    
    에이전트의 goal과 persona를 기반으로 ReAct 에이전트가
    기억(MEMORY), 규칙(DMN_RULE), 스킬(SKILL)을 생성/수정합니다.
    
    Args:
        request: AgentKnowledgeSetupRequest 모델
            - agent_id: 에이전트 고유 ID (필수)
            - goal: 에이전트의 목표 (선택, 없으면 agent_info에서 가져옴)
            - persona: 에이전트의 페르소나 (선택, 없으면 agent_info에서 가져옴)
    
    Returns:
        처리 결과 (output, intermediate_steps, used_tools 등)
    """
    try:
        log(f"📥 에이전트 초기 지식 셋팅 요청 수신: agent_id={request.agent_id}")
        
        # 에이전트 정보 조회
        agent_info = _get_agent_by_id(request.agent_id)
        if not agent_info:
            raise HTTPException(
                status_code=404,
                detail=f"에이전트를 찾을 수 없습니다: {request.agent_id}"
            )
        
        # goal과 persona 결정: 요청에 있으면 사용, 없으면 agent_info에서 가져오기
        goal = request.goal or agent_info.get('goal')
        persona = request.persona or agent_info.get('persona')
        
        if not goal:
            raise HTTPException(
                status_code=400,
                detail="에이전트 정보에 goal이 없습니다."
            )
        
        # 지식 셋팅 시작 시 STARTED로 upsert
        upsert_agent_knowledge_setup_log(
            request.agent_id, agent_info.get('tenant_id'), status='STARTED'
        )
        # ReAct 에이전트로 초기 지식 셋팅 처리
        result = await process_agent_knowledge_setup_with_react(
            agent_id=request.agent_id,
            agent_info=agent_info,
            goal=goal,
            persona=persona,
        )
        
        # 에러가 있으면 FAILED로 upsert 후 500 에러 반환
        if result.get("error"):
            log(f"❌ 에이전트 초기 지식 셋팅 실패: {result.get('error')}")
            upsert_agent_knowledge_setup_log(
                request.agent_id, agent_info.get('tenant_id'), status='FAILED'
            )
            raise HTTPException(
                status_code=500,
                detail=result.get("error", "에이전트 초기 지식 셋팅 중 에러 발생")
            )
        
        # 종료 시 DONE으로 upsert
        upsert_agent_knowledge_setup_log(
            request.agent_id, agent_info.get('tenant_id'), status='DONE'
        )
        log(f"✅ 에이전트 초기 지식 셋팅 완료: agent_id={request.agent_id}")
        return result
        
    except HTTPException:
        # HTTPException은 그대로 전달
        raise
    except Exception as e:
        log(f"❌ 에이전트 초기 지식 셋팅 API 에러: {str(e)[:300]}...")
        # 예외 시 FAILED로 upsert (agent_info는 예외 발생 시점에 없을 수 있음)
        try:
            _agent = _get_agent_by_id(request.agent_id)
            upsert_agent_knowledge_setup_log(
                request.agent_id,
                _agent.get('tenant_id') if _agent else None,
                status='FAILED',
            )
        except Exception:
            pass
        raise HTTPException(
            status_code=500,
            detail=f"에이전트 초기 지식 셋팅 중 예상치 못한 에러 발생: {str(e)}"
        )

# ============================================================================
# 서버 실행
# ============================================================================
def _is_debug() -> bool:
    v = os.environ.get("DEBUG", "").lower()
    return v in ("1", "true", "yes", "on")

if __name__ == "__main__":
    import uvicorn
    debug = _is_debug()
    if debug:
        log("디버그 모드: reload=True, log_level=debug")
    uvicorn.run(
        "main:app" if debug else app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 6789)),
        ws="none",
        reload=debug,
        log_level="debug" if debug else "info",
    ) 