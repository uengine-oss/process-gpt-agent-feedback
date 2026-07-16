"""
스킬 HTTP API 클라이언트 모듈
process-gpt-deepagents가 제공하는 스킬 API(core/api/skills_router.py)의
HTTP 엔드포인트를 호출하는 기능을 제공합니다. 별도 claude-skills 서비스가 아닙니다.
"""

import os
import io
import zipfile
from typing import Dict, List, Optional, Any
from urllib.parse import quote

import requests
from dotenv import load_dotenv

from utils.logger import log, handle_error

load_dotenv()

# ============================================================================
# HTTP API 서버 설정
# ============================================================================

SKILL_API_BASE_URL = os.getenv("SKILL_API_BASE_URL", "http://localhost:8888")


def _get_base_url() -> str:
    """HTTP API 기본 URL 반환"""
    return SKILL_API_BASE_URL


def _make_request(
    method: str,
    endpoint: str,
    params: Optional[Dict] = None,
    json_data: Optional[Dict] = None,
    files: Optional[Dict] = None,
    data: Optional[Dict] = None,
) -> Dict[str, Any]:
    """
    HTTP 요청 실행
    
    Parameters
    ----------
    method : str
        HTTP 메서드 (GET, POST, PUT, DELETE)
    endpoint : str
        API 엔드포인트 (예: "/skills/upload")
    params : dict, optional
        URL 쿼리 파라미터
    json_data : dict, optional
        JSON 요청 본문
    files : dict, optional
        파일 업로드용 (multipart/form-data)
    data : dict, optional
        폼 데이터 (multipart/form-data와 함께 사용)
    
    Returns
    -------
    dict
        API 응답 (JSON 파싱된 결과)
    
    Raises
    ------
    requests.RequestException
        HTTP 요청 실패 시
    """
    url = f"{_get_base_url()}{endpoint}"
    
    try:
        response = requests.request(
            method=method,
            url=url,
            params=params,
            json=json_data,
            files=files,
            data=data,
            timeout=30,
        )
        response.raise_for_status()
        
        # JSON 응답 파싱
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        else:
            # JSON이 아닌 경우 (예: ZIP 파일 다운로드)
            return {"content": response.content, "status_code": response.status_code}
            
    except requests.exceptions.RequestException as e:
        handle_error(f"HTTP요청실패_{method}_{endpoint}", e)
        if hasattr(e.response, "json"):
            error_detail = e.response.json()
            raise Exception(f"API 요청 실패: {error_detail.get('detail', str(e))}")
        raise Exception(f"API 요청 실패: {str(e)}")


def create_skill_zip(skill_name: str, skill_content: str, additional_files: Optional[Dict[str, str]] = None) -> io.BytesIO:
    """
    스킬을 ZIP 파일로 패키징
    
    Parameters
    ----------
    skill_name : str
        스킬 이름
    skill_content : str
        SKILL.md 파일 내용
    additional_files : dict, optional
        추가 파일들 { "path": "content", ... }
        예: {"scripts/example.py": "print('hello')", "README.md": "# Skill"}
    
    Returns
    -------
    io.BytesIO
        ZIP 파일 바이트 스트림
    """
    zip_buffer = io.BytesIO()
    
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        # SKILL.md 파일 추가 (필수)
        zip_file.writestr("SKILL.md", skill_content)
        
        # 추가 파일들 추가
        if additional_files:
            for file_path, file_content in additional_files.items():
                zip_file.writestr(file_path, file_content)
    
    zip_buffer.seek(0)
    return zip_buffer


def upload_skill(
    skill_name: str,
    skill_content: str,
    tenant_id: str,
    additional_files: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    스킬을 ZIP 파일로 업로드

    Parameters
    ----------
    skill_name : str
        스킬 이름
    skill_content : str
        SKILL.md 파일 내용
    tenant_id : str
        테넌트 ID (서버가 필수로 요구 — 없으면 400)
    additional_files : dict, optional
        추가 파일들 { "path": "content", ... }

    Returns
    -------
    dict
        API 응답
        {
            "status": "ok",
            "skills_added": ["Skill Name"],
            "total_skills": 79,
            ...
        }
    """
    if not tenant_id:
        raise ValueError("tenant_id는 필수입니다")

    log(f"📦 스킬 ZIP 패키징: {skill_name}")

    # ZIP 파일 생성
    zip_buffer = create_skill_zip(skill_name, skill_content, additional_files)

    # multipart/form-data로 업로드
    files = {
        "file": (f"{skill_name}.zip", zip_buffer, "application/zip")
    }

    data = {"tenant_id": tenant_id}

    log(f"📤 스킬 업로드: {skill_name}, tenant_id={tenant_id}")

    return _make_request("POST", "/skills/upload", files=files, data=data)


def update_skill_file(
    skill_name: str,
    file_path: str,
    content: str,
    tenant_id: str,
    requester_ids: Optional[List[str]] = None,
    reviewer_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    스킬 파일 업데이트 (git provider commit + PR 워크플로우 경유)

    서버에 파일을 직접 덮어쓰는 PUT 라우트는 존재하지 않는다 — 실제로 존재하는
    POST /skills/{name}/commit(commit_skill_file)을 사용한다. 이 라우트는 내부적으로
    tenant_git_config에 연동된 git provider를 조회하므로, **git이 연동되지 않은
    테넌트는 이 호출이 항상 400으로 실패한다** (process-gpt-deepagents
    core/skills/git_providers/factory.py:get_provider). 로컬 전용 스킬을 덮어쓰는
    서버 라우트는 현재 없다 — upload_skill(CREATE)은 이미 존재하면 409를 반환한다.

    requester_ids/reviewer_id는 이 커밋이 기본 브랜치를 대상으로 해 PR이 열릴 때
    resource_pull_requests에 기록될 귀속 정보다(fix-merge-request-requester).
    **서버(process-gpt-deepagents)는 현재 이 두 필드를 읽지 않는다** —
    core/api/skills_router.py:commit_skill_file이 requester_id를 스칼라로 다루고
    zero-uuid로 폴백하며, reviewer_id는 아예 파싱하지 않는다. 서버가 requester_id를
    uuid[] 배열로, reviewer_id를 함께 받도록 고쳐지기 전까지는 이 값들이 전달돼도
    무시되거나(reviewer_id) 서버 쪽 컬럼 타입과 어긋나 저장에 실패한다.
    서버가 requester_id를 uuid[] 배열로, reviewer_id를 함께 받도록 고쳐지기 전까지는
    이 계약이 유효하다. requester_ids가 빈 배열이면 아예 보내지 않는다 — 빈 배열을
    보내면 falsy로 취급돼 서버가 zero-uuid 폴백을 쓰다 uuid[] 컬럼에 스칼라를
    넣으려다 타입 에러가 나기 때문이다.

    Parameters
    ----------
    skill_name : str
        스킬 이름
    file_path : str
        파일 경로 (예: "scripts/example.py", "SKILL.md")
    content : str
        텍스트 파일 내용
    tenant_id : str
        테넌트 ID (git provider 조회에 필수)
    requester_ids : list[str], optional
        이 개선을 촉발한 피드백 작성자 user_id 목록(중복 제거). 비어 있으면 요청
        본문에 requester_id 키 자체를 생략한다.
    reviewer_id : str, optional
        이 개선을 승인한 사람의 id.

    Returns
    -------
    dict
        API 응답 {"committed": True, "branch": ..., "pr_created": ..., ...}
    """
    encoded_skill_name = quote(skill_name)
    endpoint = f"/skills/{encoded_skill_name}/commit"

    json_data: Dict[str, Any] = {
        "tenant_id": tenant_id,
        "file_path": file_path,
        "content": content,
    }
    if requester_ids:
        json_data["requester_id"] = requester_ids
    if reviewer_id:
        json_data["reviewer_id"] = reviewer_id

    log(f"✏️ 스킬 파일 커밋: {skill_name}/{file_path}")

    return _make_request("POST", endpoint, json_data=json_data)


def delete_skill_file(skill_name: str, file_path: str, tenant_id: str) -> Dict[str, Any]:
    """
    스킬 파일 삭제

    Parameters
    ----------
    skill_name : str
        스킬 이름
    file_path : str
        삭제할 파일 경로
    tenant_id : str
        테넌트 ID (서버가 필수로 요구 — 없으면 400)

    Returns
    -------
    dict
        API 응답
        {
            "skill_name": "...",
            "file_path": "...",
            "message": "File deleted successfully"
        }
    """
    if not tenant_id:
        raise ValueError("tenant_id는 필수입니다")

    encoded_skill_name = quote(skill_name)
    encoded_file_path = quote(file_path)

    endpoint = f"/skills/{encoded_skill_name}/files/{encoded_file_path}"

    log(f"🗑️ 스킬 파일 삭제: {skill_name}/{file_path}")

    return _make_request("DELETE", endpoint, json_data={"tenant_id": tenant_id})


def delete_skill(skill_name: str, tenant_id: str) -> Dict[str, Any]:
    """
    전체 스킬 삭제

    Parameters
    ----------
    skill_name : str
        삭제할 스킬 이름
    tenant_id : str
        테넌트 ID (서버가 필수로 요구 — 없으면 400)

    Returns
    -------
    dict
        API 응답
        {
            "skill_name": "...",
            "message": "Skill deleted successfully"
        }
    """
    if not tenant_id:
        raise ValueError("tenant_id는 필수입니다")

    encoded_skill_name = quote(skill_name)

    endpoint = f"/skills/{encoded_skill_name}"

    log(f"🗑️ 스킬 삭제: {skill_name}")

    return _make_request("DELETE", endpoint, json_data={"tenant_id": tenant_id})


def list_uploaded_skills(tenant_id: str = "") -> List[Dict[str, Any]]:
    """
    업로드된 스킬 목록 조회 (GET /skills — list_skills)

    이전에는 존재하지 않는 GET /skills/list를 호출해 항상 빈 목록으로 실패했다.
    실제 라우트는 GET /skills이며 tenant_id 쿼리 파라미터로 스코프를 지정한다.

    Parameters
    ----------
    tenant_id : str, optional
        테넌트 ID (없으면 전역/기본 스코프만 조회됨)

    Returns
    -------
    list
        업로드된 스킬 목록 (각 항목에 최소 "name", "description" 포함)
    """
    try:
        params = {"tenant_id": tenant_id} if tenant_id else None
        response = _make_request("GET", "/skills", params=params)
        return response.get("skills", [])
    except Exception as e:
        log(f"⚠️ 스킬 목록 조회 실패: {e}")
        return []


def check_skill_exists(skill_name: str, tenant_id: str = "") -> bool:
    """
    스킬 존재 확인

    이전에는 존재하지 않는 GET /skills/check를 호출해 항상 404 → False로
    잘못 판정했다. 실제로는 그런 단건 조회 라우트가 없어 목록에서 찾는다.

    Parameters
    ----------
    skill_name : str
        확인할 스킬 이름
    tenant_id : str, optional
        테넌트 ID

    Returns
    -------
    bool
        스킬이 존재하면 True
    """
    return check_skill_exists_with_info(skill_name, tenant_id) is not None


def check_skill_exists_with_info(skill_name: str, tenant_id: str = "") -> Optional[Dict[str, Any]]:
    """
    스킬 존재 확인 및 상세 정보 조회 (list_uploaded_skills 결과에서 이름으로 검색)

    Parameters
    ----------
    skill_name : str
        확인할 스킬 이름
    tenant_id : str, optional
        테넌트 ID

    Returns
    -------
    dict | None
        스킬이 존재하면 목록 항목(dict, "exists": True 추가) 반환, 없으면 None
    """
    try:
        for skill in list_uploaded_skills(tenant_id):
            if skill.get("name") == skill_name:
                return {**skill, "exists": True}
        return None
    except Exception as e:
        log(f"⚠️ 스킬 존재 확인 실패: {e}")
        return None


def get_skill_files(skill_name: str, tenant_id: str) -> List[Dict[str, Any]]:
    """
    스킬 파일 목록 조회

    tenant_id를 전달하지 않으면 서버가 테넌트 전용 스킬 디렉토리(_find_tenant_skill_dir)를
    탐색하지 않아 테넌트 스킬을 찾지 못한다.

    Parameters
    ----------
    skill_name : str
        스킬 이름
    tenant_id : str
        테넌트 ID

    Returns
    -------
    list
        파일 목록
        [
            {
                "path": "SKILL.md",
                "size": 1234,
                "modified": 1704067200.0
            },
            ...
        ]
    """
    encoded_skill_name = quote(skill_name)

    endpoint = f"/skills/{encoded_skill_name}/files"

    params = {"tenant_id": tenant_id} if tenant_id else None
    response = _make_request("GET", endpoint, params=params)
    return response.get("files", [])


def get_skill_file_content(skill_name: str, file_path: str, tenant_id: str) -> Dict[str, Any]:
    """
    스킬 파일 내용 조회

    tenant_id를 전달하지 않으면 서버가 테넌트 전용 스킬 디렉토리(_find_tenant_skill_dir)를
    탐색하지 않아 테넌트 스킬을 찾지 못한다.

    Parameters
    ----------
    skill_name : str
        스킬 이름
    file_path : str
        파일 경로
    tenant_id : str
        테넌트 ID

    Returns
    -------
    dict
        파일 정보
        {
            "skill_name": "...",
            "file_path": "...",
            "type": "text" | "binary",
            "size": 1234,
            "content": "...",  # 텍스트 파일인 경우
            "content_base64": "...",  # 바이너리 파일인 경우
            ...
        }
    """
    encoded_skill_name = quote(skill_name)
    encoded_file_path = quote(file_path)

    endpoint = f"/skills/{encoded_skill_name}/files/{encoded_file_path}"

    params = {"tenant_id": tenant_id} if tenant_id else None
    return _make_request("GET", endpoint, params=params)

