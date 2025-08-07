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
load_dotenv()

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
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from core.polling_manager import start_todolist_polling, initialize_connections
from utils.logger import log

@asynccontextmanager
async def lifespan(app: FastAPI):
    """애플리케이션 생명주기 관리"""
    log("서버 시작 - 연결 초기화 및 폴링 시작")
    initialize_connections()
    asyncio.create_task(start_todolist_polling(interval=7))
    yield
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
# 서버 실행
# ============================================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=int(os.environ.get("PORT", 8000)),
        ws="none"
    ) 