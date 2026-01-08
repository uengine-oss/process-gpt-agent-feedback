"""
학습 커밋 모듈 테스트
"""

import sys
import os
import pytest
import asyncio
from unittest.mock import Mock, patch, AsyncMock, MagicMock
from typing import Dict

# 상위 디렉토리를 Python 경로에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 테스트 대상 모듈 import
from core.learning_committers.memory_committer import commit_to_memory
from core.learning_committers.dmn_committer import commit_to_dmn_rule
from core.learning_committers.skill_committer import commit_to_skill
from core.learning_committer import commit_learning


# ============================================================================
# Memory Committer 테스트
# ============================================================================

class TestMemoryCommitter:
    """Memory 커밋 테스트"""
    
    @pytest.mark.asyncio
    @patch('core.learning_committers.memory_committer._get_memory_instance')
    async def test_commit_to_memory_success(self, mock_get_memory):
        """Memory 저장 성공 테스트"""
        # Mock 설정
        mock_memory = Mock()
        mock_memory.add = Mock()
        mock_get_memory.return_value = mock_memory
        
        # 테스트 실행
        await commit_to_memory(
            agent_id="test_agent_id",
            content="테스트 메모리 내용",
            source_type="guideline"
        )
        
        # 검증
        mock_memory.add.assert_called_once()
        call_args = mock_memory.add.call_args
        assert call_args[1]['agent_id'] == "test_agent_id"
        assert call_args[0][0] == "테스트 메모리 내용"
        assert call_args[1]['metadata']['type'] == "guideline"
        assert call_args[1]['metadata']['source'] == "user_feedback"
        assert "note" in call_args[1]['metadata']
    
    @pytest.mark.asyncio
    @patch('core.learning_committers.memory_committer._get_memory_instance')
    async def test_commit_to_memory_failure(self, mock_get_memory):
        """Memory 저장 실패 테스트"""
        # Mock 설정 - 예외 발생
        mock_memory = Mock()
        mock_memory.add.side_effect = Exception("메모리 저장 실패")
        mock_get_memory.return_value = mock_memory
        
        # 테스트 실행 및 검증
        with pytest.raises(Exception):
            await commit_to_memory(
                agent_id="test_agent_id",
                content="테스트 메모리 내용"
            )


# ============================================================================
# DMN Committer 테스트
# ============================================================================

class TestDmnCommitter:
    """DMN Rule 커밋 테스트"""
    
    @pytest.mark.asyncio
    @patch('core.learning_committers.dmn_committer._get_agent_by_id')
    @patch('core.learning_committers.dmn_committer.get_db_client')
    @patch('core.learning_committers.dmn_committer._generate_dmn_xml_llm')
    async def test_commit_to_dmn_rule_success(self, mock_generate_xml, mock_get_db, mock_get_agent):
        """DMN Rule 저장 성공 테스트"""
        # Mock 설정
        mock_agent = {
            "id": "test_agent_id",
            "tenant_id": "test_tenant"
        }
        mock_get_agent.return_value = mock_agent
        
        mock_supabase = Mock()
        mock_table = Mock()
        mock_table.insert.return_value.execute.return_value = Mock()
        mock_supabase.table.return_value = mock_table
        mock_get_db.return_value = mock_supabase
        
        test_xml = '<?xml version="1.0"?><definitions></definitions>'
        mock_generate_xml.return_value = test_xml
        
        # 테스트 실행
        dmn_artifact = {
            "name": "테스트 규칙",
            "condition": "age < 18",
            "action": "20% 할인"
        }
        
        await commit_to_dmn_rule(
            agent_id="test_agent_id",
            dmn_artifact=dmn_artifact,
            feedback_content="원본 피드백"
        )
        
        # 검증
        mock_generate_xml.assert_called_once()
        mock_table.insert.assert_called_once()
        insert_args = mock_table.insert.call_args[0][0]
        assert insert_args['name'] == "테스트 규칙"
        assert insert_args['bpmn'] == test_xml
        assert insert_args['owner'] == "test_agent_id"
        assert insert_args['tenant_id'] == "test_tenant"
        assert insert_args['type'] == 'dmn'
        assert insert_args['isdeleted'] is False
    
    @pytest.mark.asyncio
    async def test_commit_to_dmn_rule_missing_condition(self):
        """DMN Rule 저장 - condition 누락 테스트"""
        dmn_artifact = {
            "name": "테스트 규칙",
            "action": "20% 할인"
        }
        
        # handle_error가 ValueError를 Exception으로 래핑하므로 Exception으로 기대
        with pytest.raises(Exception, match="condition과 action은 필수"):
            await commit_to_dmn_rule(
                agent_id="test_agent_id",
                dmn_artifact=dmn_artifact
            )
    
    @pytest.mark.asyncio
    async def test_commit_to_dmn_rule_missing_action(self):
        """DMN Rule 저장 - action 누락 테스트"""
        dmn_artifact = {
            "name": "테스트 규칙",
            "condition": "age < 18"
        }
        
        # handle_error가 ValueError를 Exception으로 래핑하므로 Exception으로 기대
        with pytest.raises(Exception, match="condition과 action은 필수"):
            await commit_to_dmn_rule(
                agent_id="test_agent_id",
                dmn_artifact=dmn_artifact
            )
    
    @pytest.mark.asyncio
    @patch('core.learning_committers.dmn_committer._get_agent_by_id')
    async def test_commit_to_dmn_rule_agent_not_found(self, mock_get_agent):
        """DMN Rule 저장 - 에이전트를 찾을 수 없음 테스트"""
        mock_get_agent.return_value = None
        
        dmn_artifact = {
            "condition": "age < 18",
            "action": "20% 할인"
        }
        
        # handle_error가 ValueError를 Exception으로 래핑하므로 Exception으로 기대
        with pytest.raises(Exception, match="에이전트를 찾을 수 없습니다"):
            await commit_to_dmn_rule(
                agent_id="invalid_agent_id",
                dmn_artifact=dmn_artifact
            )


# ============================================================================
# Skill Committer 테스트
# ============================================================================

class TestSkillCommitter:
    """Skill 커밋 테스트"""
    
    @pytest.mark.asyncio
    async def test_commit_to_skill_success(self):
        """Skill 저장 성공 테스트 (stub)"""
        skill_artifact = {
            "name": "테스트 스킬",
            "steps": [
                "1단계: 데이터 수집",
                "2단계: 데이터 분석",
                "3단계: 결과 보고"
            ]
        }
        
        # stub이므로 예외 없이 완료되어야 함
        await commit_to_skill(
            agent_id="test_agent_id",
            skill_artifact=skill_artifact
        )
    
    @pytest.mark.asyncio
    async def test_commit_to_skill_missing_steps(self):
        """Skill 저장 - steps 누락 테스트"""
        skill_artifact = {
            "name": "테스트 스킬"
        }
        
        # handle_error가 ValueError를 Exception으로 래핑하므로 Exception으로 기대
        with pytest.raises(Exception, match="steps는 필수"):
            await commit_to_skill(
                agent_id="test_agent_id",
                skill_artifact=skill_artifact
            )
    
    @pytest.mark.asyncio
    async def test_commit_to_skill_empty_steps(self):
        """Skill 저장 - 빈 steps 테스트"""
        skill_artifact = {
            "name": "테스트 스킬",
            "steps": []
        }
        
        # handle_error가 ValueError를 Exception으로 래핑하므로 Exception으로 기대
        with pytest.raises(Exception, match="steps는 필수"):
            await commit_to_skill(
                agent_id="test_agent_id",
                skill_artifact=skill_artifact
            )


# ============================================================================
# Learning Committer (라우터) 테스트
# ============================================================================

class TestLearningCommitter:
    """학습 커밋 라우터 테스트"""
    
    @pytest.mark.asyncio
    @patch('core.learning_committer.commit_to_memory')
    async def test_commit_learning_memory(self, mock_commit_memory):
        """MEMORY 타겟 커밋 테스트"""
        routed_learning = {
            "target": "MEMORY",
            "artifacts": {
                "memory": "테스트 메모리 내용"
            },
            "reasoning": "지침 형태의 피드백"
        }
        
        await commit_learning(
            agent_id="test_agent_id",
            routed_learning=routed_learning,
            original_content="원본 내용"
        )
        
        mock_commit_memory.assert_called_once_with(
            "test_agent_id",
            "테스트 메모리 내용",
            source_type="guideline"
        )
    
    @pytest.mark.asyncio
    @patch('core.learning_committer.commit_to_dmn_rule')
    async def test_commit_learning_dmn_rule(self, mock_commit_dmn):
        """DMN_RULE 타겟 커밋 테스트"""
        routed_learning = {
            "target": "DMN_RULE",
            "artifacts": {
                "dmn": {
                    "name": "할인 규칙",
                    "condition": "age < 18",
                    "action": "20% 할인"
                }
            },
            "reasoning": "조건-결과 형태의 규칙"
        }
        
        await commit_learning(
            agent_id="test_agent_id",
            routed_learning=routed_learning,
            original_content="원본 피드백"
        )
        
        mock_commit_dmn.assert_called_once_with(
            "test_agent_id",
            {
                "name": "할인 규칙",
                "condition": "age < 18",
                "action": "20% 할인"
            },
            "원본 피드백"
        )
    
    @pytest.mark.asyncio
    @patch('core.learning_committer.commit_to_skill')
    async def test_commit_learning_skill(self, mock_commit_skill):
        """SKILL 타겟 커밋 테스트"""
        routed_learning = {
            "target": "SKILL",
            "artifacts": {
                "skill": {
                    "name": "처리 절차",
                    "steps": ["1단계", "2단계", "3단계"]
                }
            },
            "reasoning": "단계별 절차 형태"
        }
        
        await commit_learning(
            agent_id="test_agent_id",
            routed_learning=routed_learning
        )
        
        mock_commit_skill.assert_called_once_with(
            "test_agent_id",
            {
                "name": "처리 절차",
                "steps": ["1단계", "2단계", "3단계"]
            }
        )
    
    @pytest.mark.asyncio
    @patch('core.learning_committer.commit_to_dmn_rule')
    @patch('core.learning_committer.commit_to_skill')
    @patch('core.learning_committer.commit_to_memory')
    async def test_commit_learning_mixed(self, mock_commit_memory, mock_commit_skill, mock_commit_dmn):
        """MIXED 타겟 커밋 테스트 - 우선순위 검증"""
        routed_learning = {
            "target": "MIXED",
            "artifacts": {
                "dmn": {
                    "condition": "age < 18",
                    "action": "20% 할인"
                },
                "skill": {
                    "steps": ["1단계", "2단계"]
                },
                "memory": "추가 지침"
            },
            "reasoning": "혼합 형태"
        }
        
        await commit_learning(
            agent_id="test_agent_id",
            routed_learning=routed_learning,
            original_content="원본"
        )
        
        # DMN과 Skill은 호출되어야 함
        mock_commit_dmn.assert_called_once()
        mock_commit_skill.assert_called_once()
        # MEMORY는 호출되지 않아야 함 (우선순위 규칙)
        mock_commit_memory.assert_not_called()
    
    @pytest.mark.asyncio
    @patch('core.learning_committer.commit_to_memory')
    async def test_commit_learning_mixed_only_memory(self, mock_commit_memory):
        """MIXED 타겟 - MEMORY만 있는 경우"""
        routed_learning = {
            "target": "MIXED",
            "artifacts": {
                "memory": "지침 내용"
            },
            "reasoning": "MEMORY만 포함"
        }
        
        await commit_learning(
            agent_id="test_agent_id",
            routed_learning=routed_learning
        )
        
        # MEMORY는 호출되어야 함 (DMN/Skill이 없는 경우)
        mock_commit_memory.assert_called_once()
    
    @pytest.mark.asyncio
    @patch('core.learning_committer.commit_to_memory')
    async def test_commit_learning_unknown_target(self, mock_commit_memory):
        """알 수 없는 타겟 - 기본값 MEMORY로 처리"""
        routed_learning = {
            "target": "UNKNOWN",
            "artifacts": {
                "memory": "기본 처리"
            }
        }
        
        await commit_learning(
            agent_id="test_agent_id",
            routed_learning=routed_learning
        )
        
        # 기본값으로 MEMORY 처리
        mock_commit_memory.assert_called_once()


# ============================================================================
# 통합 테스트
# ============================================================================

class TestIntegration:
    """통합 테스트"""
    
    @pytest.mark.asyncio
    @patch('core.learning_committer.commit_to_memory')
    @patch('core.learning_committer.commit_to_dmn_rule')
    @patch('core.learning_committer.commit_to_skill')
    async def test_full_learning_flow(self, mock_skill, mock_dmn, mock_memory):
        """전체 학습 흐름 테스트"""
        # 다양한 타겟 테스트
        test_cases = [
            {
                "target": "MEMORY",
                "artifacts": {"memory": "메모리 내용"},
                "expected_calls": {"memory": 1, "dmn": 0, "skill": 0}
            },
            {
                "target": "DMN_RULE",
                "artifacts": {"dmn": {"condition": "age < 18", "action": "할인"}},
                "expected_calls": {"memory": 0, "dmn": 1, "skill": 0}
            },
            {
                "target": "SKILL",
                "artifacts": {"skill": {"steps": ["1", "2"]}},
                "expected_calls": {"memory": 0, "dmn": 0, "skill": 1}
            }
        ]
        
        for case in test_cases:
            mock_memory.reset_mock()
            mock_dmn.reset_mock()
            mock_skill.reset_mock()
            
            routed_learning = {
                "target": case["target"],
                "artifacts": case["artifacts"],
                "reasoning": "테스트"
            }
            
            await commit_learning(
                agent_id="test_agent_id",
                routed_learning=routed_learning
            )
            
            assert mock_memory.call_count == case["expected_calls"]["memory"]
            assert mock_dmn.call_count == case["expected_calls"]["dmn"]
            assert mock_skill.call_count == case["expected_calls"]["skill"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
