"""
Skill 커밋 모듈
Claude Skill을 저장하는 로직 (HTTP API를 통해 구현)
"""

from typing import Dict, List, Optional
from utils.logger import log, handle_error
from core.database import update_agent_and_tenant_skills, _get_agent_by_id, record_knowledge_history
from core.skill_api_client import (
    upload_skill,
    update_skill_file,
    delete_skill,
    delete_skill_file,
    check_skill_exists,
    get_skill_file_content,
    get_skill_files,
)


async def commit_to_skill(agent_id: str, skill_artifact: Dict, operation: str = "CREATE", skill_id: str = None, feedback_content: Optional[str] = None):
    """
    Skill로 CRUD 작업 수행 (HTTP API를 통해)
    
    Args:
        agent_id: 에이전트 ID (tenant_id로 사용)
        skill_artifact: Skill 정보 {
            "name": "스킬 이름 (선택적)",
            "description": "스킬 설명 (frontmatter용, 선택적)",
            "overview": "스킬 개요 (본문에 표시, 선택적)",
            "usage": "사용법 (선택적)",
            "steps": ["1단계", "2단계", ...],
            "additional_files": {  # 선택적
                "path": "content",
                "scripts/helper.py": "# Python 코드",
                ...
            }
        }
        operation: "CREATE" | "UPDATE" | "DELETE"
        skill_id: UPDATE/DELETE 시 기존 스킬 이름 (필수)
                   CREATE 시 skill_artifact의 name 또는 skill_id 사용
        feedback_content: 원본 피드백 내용 (변경 이력 기록용, 선택적)
    
    Raises:
        Exception: 작업 실패 시
    """
    try:
        # skill_id가 있으면 우선 사용, 없으면 skill_artifact에서 가져오기
        skill_name = skill_id or skill_artifact.get("name", "피드백 기반 스킬")
        steps = skill_artifact.get("steps", [])
        additional_files = skill_artifact.get("additional_files", {})
        # SKILL.md frontmatter용 설명: 명시적으로 주어진 description을 우선 사용하고,
        # 없으면 기본 설명을 자동 생성하여 항상 description 필드를 채웁니다.
        description = skill_artifact.get(
            "description",
            f"{skill_name} 작업을 수행하기 위한 단계별 절차입니다.",
        )
        # 개요와 사용법 추출 (선택적)
        overview = skill_artifact.get("overview")
        usage = skill_artifact.get("usage")
        
        # 에이전트 정보 조회 (tenant_id 필요)
        agent_info = _get_agent_by_id(agent_id)
        tenant_id = agent_info.get("tenant_id") if agent_info else None
        
        if operation == "DELETE":
            if not skill_name:
                log(f"⚠️ DELETE 작업인데 skill_name이 없음")
                raise ValueError("DELETE 작업에는 skill_id(스킬 이름)가 필요합니다")
            
            log(f"🗑️ SKILL 삭제 시작: 에이전트 {agent_id}, skill_name={skill_name}")
            
            # 삭제 전 이전 내용 조회 (변경 이력용)
            previous_content = None
            try:
                if check_skill_exists(skill_name):
                    # SKILL.md 파일 내용 조회
                    try:
                        skill_file_info = get_skill_file_content(skill_name, "SKILL.md")
                        skill_content = skill_file_info.get("content", "")
                        previous_content = skill_content  # skill_content 문자열만 저장
                    except Exception as e:
                        log(f"   ⚠️ 삭제 전 스킬 내용 조회 실패 (변경 이력은 부분적으로 기록): {e}")
            except Exception:
                pass  # 스킬 존재 확인 실패 시 무시
            
            try:
                # 스킬 존재 확인
                if not check_skill_exists(skill_name):
                    log(f"   ⚠️ 스킬이 존재하지 않습니다: {skill_name}")
                    return
                
                # HTTP API를 통해 스킬 삭제
                result = delete_skill(skill_name)
                log(f"   ✅ SKILL 삭제 완료: {result.get('message', 'Success')}")
                
            except Exception as e:
                log(f"   ❌ SKILL 삭제 실패: {e}")
                raise
            
            # 삭제 성공 후 users.skills / tenants.skills 동기화
            try:
                update_agent_and_tenant_skills(agent_id, skill_name, "DELETE")
            except Exception as e:
                log(f"   ⚠️ SKILL 삭제 후 스킬 동기화 실패 (무시하고 계속 진행): {e}")
            
            # 변경 이력 기록
            try:
                # feedback_content에서 batch_job_id 추출 시도
                batch_job_id = None
                if feedback_content and ("배치" in feedback_content or "batch" in feedback_content.lower()):
                    # 배치 작업으로 삭제된 경우 (개선 가능)
                    pass
                
                record_knowledge_history(
                    knowledge_type="SKILL",
                    knowledge_id=skill_name,
                    agent_id=agent_id,
                    tenant_id=tenant_id,
                    operation="DELETE",
                    previous_content=previous_content,
                    feedback_content=feedback_content,
                    knowledge_name=skill_name,
                    batch_job_id=batch_job_id
                )
            except Exception as e:
                log(f"   ⚠️ 스킬 변경 이력 기록 실패 (무시하고 계속 진행): {e}")
        
        if operation == "UPDATE":
            if not skill_name:
                log(f"⚠️ UPDATE 작업인데 skill_name이 없음")
                raise ValueError("UPDATE 작업에는 skill_id(스킬 이름)가 필요합니다")
            
            # steps는 선택적이므로 비어있어도 계속 진행
            if not steps:
                log(f"⚠️ SKILL 수정: steps가 비어있음 (선택적 필드이므로 계속 진행)")
            
            log(f"✏️ SKILL 수정 시작: 에이전트 {agent_id}, skill_name={skill_name}")
            log(f"   스킬 이름: {skill_name}")
            log(f"   단계 수: {len(steps)}")
            
            # 업데이트 전 이전 내용 조회 (변경 이력용)
            previous_content = None
            try:
                if check_skill_exists(skill_name):
                    # SKILL.md 파일 내용 조회
                    try:
                        skill_file_info = get_skill_file_content(skill_name, "SKILL.md")
                        skill_content = skill_file_info.get("content", "")
                        previous_content = skill_content  # skill_content 문자열만 저장
                    except Exception as e:
                        log(f"   ⚠️ 업데이트 전 스킬 내용 조회 실패 (변경 이력은 부분적으로 기록): {e}")
            except Exception:
                pass  # 스킬 존재 확인 실패 시 무시
            
            try:
                # 스킬 존재 확인
                if not check_skill_exists(skill_name):
                    log(f"   ⚠️ 스킬이 존재하지 않습니다. CREATE로 전환: {skill_name}")
                    operation = "CREATE"
                else:
                    # SKILL.md 파일 업데이트 (frontmatter 규칙을 항상 만족하도록 생성)
                    skill_document = _format_skill_document(skill_name, steps, description, overview, usage)
                    
                    # 새 내용 구성 (변경 이력용 - skill_content 문자열만 저장)
                    new_content = skill_document
                    
                    result = update_skill_file(skill_name, "SKILL.md", content=skill_document)
                    log(f"   ✅ SKILL.md 업데이트 완료: {result.get('message', 'Success')}")
                    
                    # 추가 파일들도 업데이트
                    if additional_files:
                        for file_path, file_content in additional_files.items():
                            try:
                                update_skill_file(skill_name, file_path, content=file_content)
                                log(f"   ✅ 파일 업데이트 완료: {file_path}")
                            except Exception as e:
                                log(f"   ⚠️ 파일 업데이트 실패 ({file_path}): {e}")
                    
                    log(f"   ✅ SKILL 수정 완료: skill_name={skill_name}")
                    
                    # 변경 이력 기록
                    try:
                        record_knowledge_history(
                            knowledge_type="SKILL",
                            knowledge_id=skill_name,
                            agent_id=agent_id,
                            tenant_id=tenant_id,
                            operation="UPDATE",
                            previous_content=previous_content,
                            new_content=new_content,
                            feedback_content=feedback_content,
                            knowledge_name=skill_name
                        )
                    except Exception as e:
                        log(f"   ⚠️ 스킬 변경 이력 기록 실패 (무시하고 계속 진행): {e}")
                    
                    return
                    
            except Exception as e:
                log(f"   ⚠️ 스킬 수정 실패: {e}")
                log(f"   새로 생성하는 방식으로 진행")
                operation = "CREATE"
        
        if operation == "CREATE":
            # steps는 선택적이므로 비어있어도 계속 진행
            if not steps:
                log(f"⚠️ SKILL 저장: steps가 비어있음 (선택적 필드이므로 계속 진행)")
            
            log(f"✅ SKILL 저장 시작: 에이전트 {agent_id}")
            log(f"   스킬 이름: {skill_name}")
            if steps:
                log(f"   단계 수: {len(steps)}")
                for idx, step in enumerate(steps, start=1):
                    log(f"   {idx}. {step}")
            else:
                log(f"   단계: 없음 (개요/사용법만 포함)")
            
            try:
                if not agent_info:
                    raise ValueError(f"에이전트를 찾을 수 없습니다: agent_id={agent_id}")
                
                if not tenant_id:
                    raise ValueError(f"에이전트의 tenant_id가 없습니다: agent_id={agent_id}")
                
                # 스킬이 이미 존재하는지 확인
                # ⚠️ 자동 전환 제거: 에이전트가 직접 CREATE/UPDATE를 명시적으로 선택해야 함
                if check_skill_exists(skill_name):
                    log(f"   ❌ 스킬이 이미 존재합니다: {skill_name}")
                    log(f"   💡 기존 스킬을 수정하려면 operation='UPDATE', skill_id='{skill_name}'을 사용하세요.")
                    raise ValueError(f"스킬 '{skill_name}'이(가) 이미 존재합니다. 수정하려면 UPDATE 작업을 사용하세요.")
                
                # 스킬 문서 생성 (frontmatter 규칙을 항상 만족하도록 생성)
                skill_document = _format_skill_document(skill_name, steps, description, overview, usage)
                
                # 새 내용 구성 (변경 이력용 - skill_content 문자열만 저장)
                new_content = skill_document
                
                # HTTP API를 통해 스킬 업로드 (ZIP 파일로)
                # 에이전트의 실제 tenant_id 사용 (멀티테넌트 지원)
                result = upload_skill(
                    skill_name=skill_name,
                    skill_content=skill_document,
                    tenant_id=tenant_id,  # 에이전트의 실제 tenant_id 사용
                    additional_files=additional_files if additional_files else None,
                )
                
                skills_added = result.get("skills_added", [])
                log(f"   ✅ SKILL 저장 완료: skill_name={skill_name}")
                log(f"   추가된 스킬: {skills_added}")
                log(f"   총 스킬 수: {result.get('total_skills', 'N/A')}")

                # CREATE 성공 후 users.skills / tenants.skills 동기화
                try:
                    update_agent_and_tenant_skills(agent_id, skill_name, "CREATE")
                except Exception as e:
                    log(f"   ⚠️ SKILL 생성 후 스킬 동기화 실패 (무시하고 계속 진행): {e}")
                
                # 변경 이력 기록
                try:
                    record_knowledge_history(
                        knowledge_type="SKILL",
                        knowledge_id=skill_name,
                        agent_id=agent_id,
                        tenant_id=tenant_id,
                        operation="CREATE",
                        new_content=new_content,
                        feedback_content=feedback_content,
                        knowledge_name=skill_name
                    )
                except Exception as e:
                    log(f"   ⚠️ 스킬 변경 이력 기록 실패 (무시하고 계속 진행): {e}")
                
            except Exception as e:
                log(f"   ❌ SKILL 저장 실패: {e}")
                raise
        
    except Exception as e:
        handle_error(f"SKILL{operation}", e)
        raise


def _format_skill_document(
    skill_name: str, 
    steps: List[str], 
    description: Optional[str] = None,
    overview: Optional[str] = None,
    usage: Optional[str] = None
) -> str:
    """
    스킬 정보를 마크다운 문서 형식으로 변환
    
    Args:
        skill_name: 스킬 이름
        steps: 스킬 단계 목록
        description: 스킬 설명 (frontmatter용)
        overview: 스킬 개요 (본문에 표시)
        usage: 사용법 (선택적)
    
    Returns:
        마크다운 형식의 스킬 문서 (SKILL.md 규칙을 만족하는 frontmatter 포함)
    """
    if description is None:
        description = f"{skill_name} 작업을 수행하기 위한 단계별 절차입니다."
    
    if overview is None:
        overview = description

    lines: List[str] = []

    # --- Frontmatter (SKILL.md 필수 규칙) ---
    lines.append("---\n")
    lines.append(f"name: {skill_name}\n")
    lines.append(f"description: {description}\n")
    lines.append("---\n")
    lines.append("\n")

    # 본문: 개요 → 단계별 실행 절차 → 사용법 순서
    lines.append(f"# {skill_name}\n")
    lines.append("\n")
    
    # 개요 섹션
    lines.append("## 개요\n")
    lines.append(f"{overview}\n")
    lines.append("\n")
    
    # 단계별 실행 절차 섹션 (steps가 있는 경우만)
    if steps:
        lines.append("## 단계별 실행 절차\n")
        lines.append("\n")
        
        for idx, step in enumerate(steps, start=1):
            lines.append(f"{idx}. {step}\n")
        
        lines.append("\n")
    
    # 사용법 섹션 (선택적)
    if usage:
        lines.append("## 사용법\n")
        lines.append("\n")
        lines.append(f"{usage}\n")
        lines.append("\n")
    
    return "".join(lines)
