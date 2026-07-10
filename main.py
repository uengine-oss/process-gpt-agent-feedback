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

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
os.environ["PYTHONIOENCODING"] = "utf-8"
load_dotenv(override=True)

_orig_print = builtins.print
def print(*args, **kwargs):
    if 'flush' not in kwargs:
        kwargs['flush'] = True
    _orig_print(*args, **kwargs)
builtins.print = print

warnings.filterwarnings("ignore", category=DeprecationWarning)

# ============================================================================
# FastAPI 애플리케이션 설정
# ============================================================================
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from core.polling_manager import start_feedback_polling, initialize_connections
from core.feedback_batch_manager import start_feedback_batch_collection, start_feedback_batch_trigger
from core.feedback_proposal_routes import router as feedback_proposals_router
from utils.logger import log

# 배치 수집 → 제안 → 승인 플로우로 전환할지 여부. false면 기존처럼 즉시 처리한다.
# 두 경로를 동시에 켜두면 같은 agent_feedback_task 큐를 경쟁적으로 소비하게 되므로 반드시 배타적으로 운영한다.
USE_BATCHED_FEEDBACK = os.environ.get("USE_BATCHED_FEEDBACK", "").lower() in ("1", "true", "yes", "on")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """애플리케이션 생명주기 관리"""
    initialize_connections()

    tasks = []
    if USE_BATCHED_FEEDBACK:
        log("서버 시작 - 연결 초기화, 피드백 배치 수집(7s) + 배치 트리거 확인(900s) 시작")
        tasks.append(asyncio.create_task(start_feedback_batch_collection(interval=7)))
        tasks.append(asyncio.create_task(start_feedback_batch_trigger(interval=900)))
    else:
        log("서버 시작 - 연결 초기화 및 피드백 폴링(즉시 처리) 시작")
        tasks.append(asyncio.create_task(start_feedback_polling(interval=7)))

    yield

    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log(f"⚠️ 폴링 작업 종료 중 에러 (무시): {str(e)[:200]}...")
    log("서버 종료")

app = FastAPI(
    title="Agent Feedback Skill Processor",
    version="2.0",
    description="피드백 기반 에이전트 스킬 개선 서비스 (Deep Agent)",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(feedback_proposals_router)

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
