"""
MCP 클라이언트 공용 유틸 모듈
LangChain MCP 어댑터 기반으로 MCP 서버에 연결하고, 도구를 로드/공유하는 기능을 제공합니다.
"""

import asyncio
import concurrent.futures
import os
import sys
import traceback
from types import ModuleType
from typing import List, Optional, Any, TypedDict, Literal, NotRequired
from dotenv import load_dotenv
from utils.logger import log, handle_error

# langchain_core.messages.content 호환성 패치
# 최신 langchain_core (0.3.80+)에서는 messages.content 모듈이 없고 content_blocks로 변경됨
try:
    import langchain_core.messages
    if not hasattr(langchain_core.messages, 'content'):
        # content 모듈이 없으면 호환성 레이어 생성
        content_module = ModuleType('content')
        
        # content_blocks를 확인하고 필요한 클래스/함수 제공
        try:
            # 타입 별칭 생성 (dict 기반)
            class TextContentBlock(TypedDict):
                type: Literal["text"]
                text: str
            
            class ImageContentBlock(TypedDict, total=False):
                type: Literal["image"]
                url: NotRequired[str]
                base64: NotRequired[str]
                mime_type: NotRequired[str]
            
            class FileContentBlock(TypedDict, total=False):
                type: Literal["file"]
                url: NotRequired[str]
                base64: NotRequired[str]
                mime_type: NotRequired[str]
                filename: NotRequired[str]
            
            def create_text_block(text: str) -> TextContentBlock:
                return {"type": "text", "text": text}
            
            def create_image_block(
                url: str | None = None,
                base64: str | None = None,
                mime_type: str | None = None
            ) -> ImageContentBlock:
                result: ImageContentBlock = {"type": "image"}
                if url:
                    result["url"] = url
                if base64:
                    result["base64"] = base64
                if mime_type:
                    result["mime_type"] = mime_type
                return result
            
            def create_file_block(
                url: str | None = None,
                base64: str | None = None,
                mime_type: str | None = None,
                filename: str | None = None
            ) -> FileContentBlock:
                result: FileContentBlock = {"type": "file"}
                if url:
                    result["url"] = url
                if base64:
                    result["base64"] = base64
                if mime_type:
                    result["mime_type"] = mime_type
                if filename:
                    result["filename"] = filename
                return result
            
            # 모듈에 추가
            content_module.TextContentBlock = TextContentBlock
            content_module.ImageContentBlock = ImageContentBlock
            content_module.FileContentBlock = FileContentBlock
            content_module.create_text_block = create_text_block
            content_module.create_image_block = create_image_block
            content_module.create_file_block = create_file_block
            
            # langchain_core.messages에 content 모듈 추가
            langchain_core.messages.content = content_module
            sys.modules['langchain_core.messages.content'] = content_module
            
            log("✅ langchain_core.messages.content 호환성 패치 적용 완료")
            
        except Exception as patch_error:
            log(f"⚠️ 호환성 패치 적용 실패: {patch_error}")
            
except Exception:
    pass  # langchain_core 자체가 없으면 패치하지 않음

# langchain_mcp_adapters 및 그 하위 의존성이 없을 수 있으므로 안전하게 로드한다.
try:  # pragma: no cover - 환경에 따라 분기
    from langchain_mcp_adapters.client import MultiServerMCPClient  # type: ignore
    _MCP_LIB_AVAILABLE = True
except Exception as e:
    # 일부 환경에서는 langchain_mcp_adapters 자체나 내부에서 참조하는 langchain_core 가
    # 설치되어 있지 않아 ImportError / ModuleNotFoundError 가 발생할 수 있다.
    # 이 경우 MCP 관련 기능은 사용하지 않고, 호출 시 None/빈 리스트를 반환하도록 한다.
    log(f"⚠️ langchain_mcp_adapters 로드 실패: {type(e).__name__}: {e}")
    log(f"   상세 정보: {str(e)}")
    log(f"   Traceback:\n{''.join(traceback.format_exception(type(e), e, e.__traceback__))}")
    MultiServerMCPClient = Any  # type: ignore
    _MCP_LIB_AVAILABLE = False

load_dotenv()

# ============================================================================
# MCP 서버 설정
# ============================================================================

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:8765/mcp")
MCP_SERVER_NAME = os.getenv("MCP_SERVER_NAME", "claude-skills")
COMPUTER_USE_MCP_URL = os.getenv("COMPUTER_USE_MCP_URL", "").strip()
USE_SKILL_CREATOR_WORKFLOW = os.getenv("USE_SKILL_CREATOR_WORKFLOW", "false").lower() in ("true", "1", "yes")

_mcp_client: Optional[MultiServerMCPClient] = None
_mcp_tools: Optional[List[Any]] = None


def get_mcp_client() -> Optional[MultiServerMCPClient]:
    """
    전역 MCP 클라이언트 인스턴스 반환 (lazy 초기화)

    Returns
    -------
    MultiServerMCPClient | None
        MCP 서버 URL이 설정되지 않았거나 MCP 라이브러리가 없는 경우 None
    """
    global _mcp_client

    if not _MCP_LIB_AVAILABLE:
        log("⚠️ langchain_mcp_adapters 가 설치되지 않아 MCP 클라이언트를 생성하지 않습니다.")
        return None

    if not MCP_SERVER_URL:
        log("⚠️ MCP_SERVER_URL이 설정되지 않아 MCP 클라이언트를 생성하지 않습니다.")
        return None

    if _mcp_client is None:
        try:
            log(f"🔌 MCP 클라이언트 초기화: server_name={MCP_SERVER_NAME}, url={MCP_SERVER_URL}")
            # URL에서 transport 타입 자동 감지 (streamable-http는 /mcp, transport="http")
            transport = "http"
            if MCP_SERVER_URL.startswith("ws://") or MCP_SERVER_URL.startswith("wss://"):
                transport = "websocket"
            elif MCP_SERVER_URL.endswith("/sse"):
                transport = "sse"

            # streamable-http(/mcp)용: HTTP/2 끔, keepalive 끔 (K8s 내부에서 421 등 완화)
            # 421이 계속 나면 claude-skills 쪽 TrustedHostMiddleware/allowed_hosts 에
            # claude-skills, claude-skills:8765, claude-skills.<ns>.svc.cluster.local 등 포함 여부 확인
            def _httpx_client_factory_no_http2(**kwargs):
                import httpx
                kwargs.pop("http2", None)
                kwargs.pop("limits", None)
                return httpx.AsyncClient(
                    http2=False,
                    limits=httpx.Limits(max_keepalive_connections=0),
                    **kwargs,
                )

            def _conn(url: str, t: str) -> dict:
                c: dict = {"url": url, "transport": t}
                if t == "http":
                    c["httpx_client_factory"] = _httpx_client_factory_no_http2
                return c

            connections = {MCP_SERVER_NAME: _conn(MCP_SERVER_URL, transport)}
            if COMPUTER_USE_MCP_URL:
                cu_transport = "http"
                if COMPUTER_USE_MCP_URL.startswith("ws://") or COMPUTER_USE_MCP_URL.startswith("wss://"):
                    cu_transport = "websocket"
                elif COMPUTER_USE_MCP_URL.endswith("/sse"):
                    cu_transport = "sse"
                connections["computer-use"] = _conn(COMPUTER_USE_MCP_URL, cu_transport)
                log(f"   computer-use MCP 추가: url={COMPUTER_USE_MCP_URL[:50]}...")
            
            _mcp_client = MultiServerMCPClient(connections=connections)
        except Exception as e:
            handle_error("MCP클라이언트초기화", e)
            return None

    return _mcp_client


async def get_mcp_tools_async(force_reload: bool = False) -> List[Any]:
    """
    MCP 서버에서 도구 목록을 비동기적으로 로드하여 반환 (전역 캐시)
    
    Parameters
    ----------
    force_reload : bool
        True인 경우 MCP 도구를 다시 로드
    """
    global _mcp_tools

    client = get_mcp_client()
    if client is None:
        return []

    if _mcp_tools is None or force_reload:
        try:
            _mcp_tools = await client.get_tools()
            log(f"✅ MCP 도구 로드 완료: {len(_mcp_tools)}개 도구")
        except Exception as e:
            handle_error("MCP도구로드", e)
            return []

    return _mcp_tools or []


def get_mcp_tools(force_reload: bool = False) -> List[Any]:
    """
    MCP 서버에서 도구 목록을 로드하여 반환 (전역 캐시)
    동기 함수로, 내부에서 비동기 함수를 호출합니다.
    
    Parameters
    ----------
    force_reload : bool
        True인 경우 MCP 도구를 다시 로드
    
    Note
    ----
    이미 실행 중인 이벤트 루프가 있는 경우 ThreadPoolExecutor를 사용합니다.
    """
    try:
        # 이미 실행 중인 이벤트 루프가 있는지 확인
        try:
            loop = asyncio.get_running_loop()
            # 이미 실행 중인 루프가 있으면 새 스레드에서 실행
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(asyncio.run, get_mcp_tools_async(force_reload))
                return future.result()
        except RuntimeError:
            # 실행 중인 루프가 없으면 asyncio.run 사용
            return asyncio.run(get_mcp_tools_async(force_reload))
    except Exception as e:
        handle_error("MCP도구로드", e)
        return []


def get_mcp_tool_by_name(name: str) -> Optional[Any]:
    """
    이름으로 MCP 도구 검색 (동기)
    """
    tools = get_mcp_tools()
    for tool in tools:
        if getattr(tool, "name", None) == name:
            return tool
    return None


async def get_mcp_tool_by_name_async(name: str, force_reload: bool = False) -> Optional[Any]:
    """
    이름으로 MCP 도구 검색 (비동기). skill_creator_committer 등에서 ainvoke 시 사용.
    """
    tools = await get_mcp_tools_async(force_reload=force_reload)
    for tool in tools:
        if getattr(tool, "name", None) == name:
            return tool
    return None


def close_mcp_client() -> None:
    """
    전역 MCP 클라이언트 정리 (서버 종료 시 호출 권장)
    
    Note
    ----
    MultiServerMCPClient는 close() 메서드가 없으므로, 단순히 참조를 제거합니다.
    실제 연결은 세션 종료 시 자동으로 정리됩니다.
    """
    global _mcp_client, _mcp_tools
    if _mcp_client is not None:
        log("🔌 MCP 클라이언트 참조 제거")
    _mcp_client = None
    _mcp_tools = None


