"""
Skill artifact 처리 테스트
새로운 필드(overview, usage, additional_files) 처리 검증
"""

import sys
import os
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

# 상위 디렉토리를 Python 경로에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.learning_committers.skill_committer import commit_to_skill, _format_skill_document


class TestSkillArtifactProcessing:
    """Skill artifact 처리 테스트"""
    
    @pytest.mark.asyncio
    @patch('core.learning_committers.skill_committer._get_agent_by_id')
    @patch('core.learning_committers.skill_committer.upload_skill')
    @patch('core.learning_committers.skill_committer.update_agent_and_tenant_skills')
    async def test_commit_skill_with_all_fields(self, mock_update_skills, mock_upload, mock_get_agent):
        """모든 필드가 포함된 스킬 생성 테스트"""
        # Mock 설정
        mock_get_agent.return_value = {
            "id": "test_agent",
            "tenant_id": "test_tenant"
        }
        mock_upload.return_value = {
            "status": "ok",
            "skills_added": ["테스트 스킬"],
            "total_skills": 1
        }
        
        skill_artifact = {
            "name": "테스트 스킬",
            "description": "스킬 설명",
            "overview": "이 스킬은 특정 작업을 수행하기 위한 상세한 절차입니다.",
            "usage": "사용 시 주의사항을 확인하세요.",
            "steps": [
                "1단계: 데이터 수집",
                "2단계: 데이터 분석",
                "3단계: 결과 보고"
            ],
            "additional_files": {
                "scripts/helper.py": "def helper_function():\n    pass"
            }
        }
        
        await commit_to_skill(
            agent_id="test_agent",
            skill_artifact=skill_artifact,
            operation="CREATE"
        )
        
        # upload_skill이 호출되었는지 확인
        mock_upload.assert_called_once()
        call_args = mock_upload.call_args
        
        # skill_content에 개요가 포함되어야 함
        skill_content = call_args.kwargs['skill_content']
        assert "## 개요" in skill_content
        assert skill_artifact["overview"] in skill_content
        
        # skill_content에 사용법이 포함되어야 함
        assert "## 사용법" in skill_content
        assert skill_artifact["usage"] in skill_content
        
        # additional_files가 전달되어야 함
        assert call_args.kwargs['additional_files'] == skill_artifact["additional_files"]
    
    @pytest.mark.asyncio
    @patch('core.learning_committers.skill_committer._get_agent_by_id')
    @patch('core.learning_committers.skill_committer.upload_skill')
    @patch('core.learning_committers.skill_committer.update_agent_and_tenant_skills')
    async def test_commit_skill_without_optional_fields(self, mock_update_skills, mock_upload, mock_get_agent):
        """선택적 필드 없이 스킬 생성 테스트"""
        # Mock 설정
        mock_get_agent.return_value = {
            "id": "test_agent",
            "tenant_id": "test_tenant"
        }
        mock_upload.return_value = {
            "status": "ok",
            "skills_added": ["테스트 스킬"],
            "total_skills": 1
        }
        
        skill_artifact = {
            "name": "테스트 스킬",
            "description": "스킬 설명",
            "steps": [
                "1단계: 데이터 수집",
                "2단계: 데이터 분석"
            ]
        }
        
        await commit_to_skill(
            agent_id="test_agent",
            skill_artifact=skill_artifact,
            operation="CREATE"
        )
        
        # upload_skill이 호출되었는지 확인
        mock_upload.assert_called_once()
        call_args = mock_upload.call_args
        
        # skill_content 확인
        skill_content = call_args.kwargs['skill_content']
        assert "## 개요" in skill_content
        # description이 개요로 사용되어야 함
        assert skill_artifact["description"] in skill_content
        
        # 사용법 섹션이 없어야 함
        assert "## 사용법" not in skill_content
        
        # additional_files가 None이어야 함
        assert call_args.kwargs['additional_files'] is None
    
    @pytest.mark.asyncio
    @patch('core.learning_committers.skill_committer._get_agent_by_id')
    @patch('core.learning_committers.skill_committer.update_skill_file')
    @patch('core.learning_committers.skill_committer.check_skill_exists')
    async def test_update_skill_with_all_fields(self, mock_check_exists, mock_update_file, mock_get_agent):
        """모든 필드가 포함된 스킬 업데이트 테스트"""
        # Mock 설정
        mock_get_agent.return_value = {
            "id": "test_agent",
            "tenant_id": "test_tenant"
        }
        mock_check_exists.return_value = True
        mock_update_file.return_value = {"message": "Success"}
        
        skill_artifact = {
            "name": "테스트 스킬",
            "description": "스킬 설명",
            "overview": "업데이트된 개요",
            "usage": "업데이트된 사용법",
            "steps": [
                "1단계: 업데이트된 단계"
            ],
            "additional_files": {
                "scripts/updated.py": "updated code"
            }
        }
        
        await commit_to_skill(
            agent_id="test_agent",
            skill_artifact=skill_artifact,
            operation="UPDATE",
            skill_id="테스트 스킬"
        )
        
        # update_skill_file이 호출되었는지 확인
        assert mock_update_file.call_count >= 1
        
        # SKILL.md 업데이트 확인
        skill_md_call = None
        for call in mock_update_file.call_args_list:
            if call[0][1] == "SKILL.md":
                skill_md_call = call
                break
        
        assert skill_md_call is not None
        skill_content = skill_md_call[1]['content']
        
        # 개요와 사용법이 포함되어야 함
        assert "## 개요" in skill_content
        assert skill_artifact["overview"] in skill_content
        assert "## 사용법" in skill_content
        assert skill_artifact["usage"] in skill_content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

