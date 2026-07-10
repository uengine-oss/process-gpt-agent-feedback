"""
스킬 HTTP API 클라이언트 모듈
Claude Skills Backend의 HTTP API 엔드포인트를 호출하는 기능을 제공합니다.
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

SKILL_API_BASE_URL = os.getenv("SKILL_API_BASE_URL", "http://localhost:8765")


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
    tenant_id: Optional[str] = None,
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
    tenant_id : str, optional
        테넌트 ID (제공 시 scope="tenant"로 설정)
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
    log(f"📦 스킬 ZIP 패키징: {skill_name}")
    
    # ZIP 파일 생성
    zip_buffer = create_skill_zip(skill_name, skill_content, additional_files)
    
    # multipart/form-data로 업로드
    files = {
        "file": (f"{skill_name}.zip", zip_buffer, "application/zip")
    }
    
    data = {}
    if tenant_id:
        data["tenant_id"] = tenant_id
    
    log(f"📤 스킬 업로드: {skill_name}, tenant_id={tenant_id or 'None (global)'}")
    
    return _make_request("POST", "/skills/upload", files=files, data=data)


def update_skill_file(
    skill_name: str,
    file_path: str,
    content: Optional[str] = None,
    content_base64: Optional[str] = None,
) -> Dict[str, Any]:
    """
    스킬 파일 업데이트 (또는 새 파일 추가)
    
    Parameters
    ----------
    skill_name : str
        스킬 이름
    file_path : str
        파일 경로 (예: "scripts/example.py", "SKILL.md")
    content : str, optional
        텍스트 파일 내용
    content_base64 : str, optional
        바이너리 파일 내용 (base64 인코딩)
    
    Returns
    -------
    dict
        API 응답
        {
            "skill_name": "...",
            "file_path": "...",
            "size": 1234,
            "modified": 1704067600.0,
            "message": "File updated successfully"
        }
    
    Raises
    ------
    ValueError
        content와 content_base64가 모두 없거나 둘 다 있는 경우
    """
    if not content and not content_base64:
        raise ValueError("content 또는 content_base64 중 하나는 필수입니다")
    if content and content_base64:
        raise ValueError("content와 content_base64는 동시에 사용할 수 없습니다")
    
    # URL 인코딩
    encoded_skill_name = quote(skill_name)
    encoded_file_path = quote(file_path)
    
    endpoint = f"/skills/{encoded_skill_name}/files/{encoded_file_path}"
    
    json_data = {}
    if content:
        json_data["content"] = content
    if content_base64:
        json_data["content_base64"] = content_base64
    
    log(f"✏️ 스킬 파일 업데이트: {skill_name}/{file_path}")
    
    return _make_request("PUT", endpoint, json_data=json_data)


def delete_skill_file(skill_name: str, file_path: str) -> Dict[str, Any]:
    """
    스킬 파일 삭제
    
    Parameters
    ----------
    skill_name : str
        스킬 이름
    file_path : str
        삭제할 파일 경로
    
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
    encoded_skill_name = quote(skill_name)
    encoded_file_path = quote(file_path)
    
    endpoint = f"/skills/{encoded_skill_name}/files/{encoded_file_path}"
    
    log(f"🗑️ 스킬 파일 삭제: {skill_name}/{file_path}")
    
    return _make_request("DELETE", endpoint)


def delete_skill(skill_name: str) -> Dict[str, Any]:
    """
    전체 스킬 삭제
    
    Parameters
    ----------
    skill_name : str
        삭제할 스킬 이름
    
    Returns
    -------
    dict
        API 응답
        {
            "skill_name": "...",
            "message": "Skill deleted successfully"
        }
    """
    encoded_skill_name = quote(skill_name)
    
    endpoint = f"/skills/{encoded_skill_name}"
    
    log(f"🗑️ 스킬 삭제: {skill_name}")
    
    return _make_request("DELETE", endpoint)


def check_skill_exists(skill_name: str) -> bool:
    """
    스킬 존재 확인
    
    Parameters
    ----------
    skill_name : str
        확인할 스킬 이름
    
    Returns
    -------
    bool
        스킬이 존재하면 True
    """
    try:
        # URL 인코딩을 명시적으로 수행 (다른 함수들과 일관성 유지)
        encoded_skill_name = quote(skill_name)
        endpoint = f"/skills/check?name={encoded_skill_name}"
        url = f"{_get_base_url()}{endpoint}"
        
        # 직접 요청하여 404를 정상 응답으로 처리
        response = requests.get(url, timeout=30)
        if response.status_code == 404:
            # 404는 스킬이 존재하지 않음을 의미 (정상 응답)
            return False
        response.raise_for_status()
        
        # JSON 응답 파싱
        if response.headers.get("content-type", "").startswith("application/json"):
            data = response.json()
            return data.get("exists", False)
        return False
    except requests.exceptions.HTTPError as e:
        # 404가 아닌 다른 HTTP 에러인 경우만 로깅
        if e.response.status_code != 404:
            log(f"⚠️ 스킬 존재 확인 실패 (HTTP {e.response.status_code}): {skill_name}")
        return False
    except Exception as e:
        log(f"⚠️ 스킬 존재 확인 실패: {str(e)[:200]}...")
        return False


def check_skill_exists_with_info(skill_name: str) -> Optional[Dict[str, Any]]:
    """
    스킬 존재 확인 및 상세 정보 조회
    
    Parameters
    ----------
    skill_name : str
        확인할 스킬 이름
    
    Returns
    -------
    dict | None
        스킬이 존재하면 상세 정보 딕셔너리 반환, 없으면 None
        {
            "name": "...",
            "description": "...",
            "source": "...",
            "document_count": 4,
            "exists": true
        }
    """
    try:
        # URL 인코딩을 명시적으로 수행 (다른 함수들과 일관성 유지)
        encoded_skill_name = quote(skill_name)
        endpoint = f"/skills/check?name={encoded_skill_name}"
        response = _make_request("GET", endpoint)
        if response.get("exists", False):
            return response
        return None
    except Exception as e:
        log(f"⚠️ 스킬 존재 확인 실패: {e}")
        return None


def list_uploaded_skills() -> List[Dict[str, Any]]:
    """
    업로드된 스킬 목록 조회
    
    Returns
    -------
    list
        업로드된 스킬 목록
        [
            {
                "name": "My Custom Skill",
                "description": "A custom skill for data analysis",
                "directory": "my-custom-skill",
                "file_count": 5,
                "path": "/path/to/skills/my-custom-skill"
            },
            ...
        ]
    """
    try:
        response = _make_request("GET", "/skills/list")
        return response.get("skills", [])
    except Exception as e:
        log(f"⚠️ 스킬 목록 조회 실패: {e}")
        return []


def get_skill_files(skill_name: str) -> List[Dict[str, Any]]:
    """
    스킬 파일 목록 조회
    
    Parameters
    ----------
    skill_name : str
        스킬 이름
    
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
    
    response = _make_request("GET", endpoint)
    return response.get("files", [])


def get_skill_file_content(skill_name: str, file_path: str) -> Dict[str, Any]:
    """
    스킬 파일 내용 조회
    
    Parameters
    ----------
    skill_name : str
        스킬 이름
    file_path : str
        파일 경로
    
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
    
    return _make_request("GET", endpoint)

