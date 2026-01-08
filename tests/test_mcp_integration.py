"""
MCP 서버 통합 테스트

실제 MCP 서버 클라이언트 인스턴스를 통해 스킬 조회 흐름을 검증한다.

전제:
- 환경 변수 MCP_SERVER_URL 이 설정되어 있고,
- 해당 URL에서 MCP 서버가 실제로 동작 중이어야 한다.
"""

import os
import sys
from typing import List

import pytest

# 상위 디렉토리를 Python 경로에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.mcp_client import get_mcp_client, get_mcp_tools, get_mcp_tool_by_name
from core.knowledge_retriever import retrieve_existing_skills


def _require_mcp_server() -> None:
    """
    MCP 서버 통합 테스트를 위한 전제 조건을 확인한다.
    조건을 만족하지 못하면 pytest.skip 으로 건너뛴다.
    """
    mcp_url = os.getenv("MCP_SERVER_URL")
    if not mcp_url:
        pytest.skip("MCP_SERVER_URL 이 설정되지 않아 MCP 통합 테스트를 건너뜁니다.")


def test_mcp_client_and_tools_loaded():
    """실제 MCP 클라이언트 인스턴스와 도구 목록을 정상적으로 로드하는지 테스트"""
    _require_mcp_server()

    client = get_mcp_client()
    if client is None:
        pytest.skip("MCP 클라이언트를 초기화할 수 없어 통합 테스트를 건너뜁니다.")

    tools: List = get_mcp_tools(force_reload=True)
    assert isinstance(tools, list)
    assert len(tools) > 0

    # find_helpful_skills 도구 존재 여부를 확인
    tool = get_mcp_tool_by_name("find_helpful_skills")
    assert tool is not None, "find_helpful_skills MCP 도구를 찾지 못했습니다."


@pytest.mark.asyncio
async def test_retrieve_existing_skills_with_real_mcp():
    """
    실제 MCP 서버를 통해 retrieve_existing_skills 가 끝까지 동작하는지 테스트

    - MCP 클라이언트/도구를 실제로 사용
    - find_helpful_skills 도구를 통해 스킬 조회
    """
    _require_mcp_server()

    client = get_mcp_client()
    if client is None:
        pytest.skip("MCP 클라이언트를 초기화할 수 없어 통합 테스트를 건너뜁니다.")

    # 도구 로드 및 대상 도구 확인
    tools: List = get_mcp_tools(force_reload=True)
    if not tools:
        pytest.skip("MCP 도구를 하나도 로드하지 못해 통합 테스트를 건너뜁니다.")

    find_skills_tool = get_mcp_tool_by_name("find_helpful_skills")
    if find_skills_tool is None:
        pytest.skip("find_helpful_skills MCP 도구를 찾지 못해 통합 테스트를 건너뜁니다.")

    # 실제 스킬 조회 호출
    skills = await retrieve_existing_skills(
        agent_id="test-agent-id",
        search_text="스킬 파일 생성 작업",
        top_k=3,
        tenant_id="test-tenant-id",  # 테스트용 tenant_id
    )
    
    print(skills)

    # retrieve_existing_skills 내부에서 다양한 응답 타입을 처리하므로,
    # 여기서는 리스트 타입과 예외 없이 호출 완료되었는지만 검증한다.
    assert isinstance(skills, list)


