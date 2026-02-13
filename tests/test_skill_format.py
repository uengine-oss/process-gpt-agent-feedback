"""
Skill 마크다운 포맷팅 테스트
개요, 사용법, 스크립트 파일 지원 검증
"""

import sys
import os
import pytest

# 상위 디렉토리를 Python 경로에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.learning_committers.skill_committer import _format_skill_document


class TestSkillFormat:
    """Skill 마크다운 포맷팅 테스트"""
    
    def test_format_skill_document_basic(self):
        """기본 스킬 문서 생성 테스트"""
        skill_name = "테스트 스킬"
        steps = ["1단계", "2단계", "3단계"]
        description = "기본 설명"
        
        result = _format_skill_document(skill_name, steps, description)
        
        # Frontmatter 확인
        assert "---" in result
        assert f"name: {skill_name}" in result
        assert f"description: {description}" in result
        
        # 제목 확인
        assert f"# {skill_name}" in result
        
        # 개요 섹션 확인
        assert "## 개요" in result
        
        # 단계별 실행 절차 확인
        assert "## 단계별 실행 절차" in result
        assert "1. 1단계" in result
        assert "2. 2단계" in result
        assert "3. 3단계" in result
    
    def test_format_skill_document_with_overview(self):
        """개요가 포함된 스킬 문서 생성 테스트"""
        skill_name = "테스트 스킬"
        steps = ["1단계", "2단계"]
        description = "기본 설명"
        overview = "이 스킬은 특정 작업을 수행하기 위한 상세한 절차입니다."
        
        result = _format_skill_document(skill_name, steps, description, overview)
        
        # 개요 섹션에 사용자 정의 overview가 포함되어야 함
        assert "## 개요" in result
        assert overview in result
        # description이 개요로 사용되지 않아야 함
        assert description not in result.split("## 개요")[1].split("## 단계별 실행 절차")[0]
    
    def test_format_skill_document_with_usage(self):
        """사용법이 포함된 스킬 문서 생성 테스트"""
        skill_name = "테스트 스킬"
        steps = ["1단계", "2단계"]
        description = "기본 설명"
        overview = "스킬 개요"
        usage = "이 스킬을 사용할 때는 주의사항을 확인해야 합니다."
        
        result = _format_skill_document(skill_name, steps, description, overview, usage)
        
        # 사용법 섹션 확인
        assert "## 사용법" in result
        assert usage in result
    
    def test_format_skill_document_without_usage(self):
        """사용법이 없는 경우 사용법 섹션이 없어야 함"""
        skill_name = "테스트 스킬"
        steps = ["1단계", "2단계"]
        description = "기본 설명"
        overview = "스킬 개요"
        
        result = _format_skill_document(skill_name, steps, description, overview, None)
        
        # 사용법 섹션이 없어야 함
        assert "## 사용법" not in result
    
    def test_format_skill_document_default_overview(self):
        """개요가 없으면 description을 개요로 사용"""
        skill_name = "테스트 스킬"
        steps = ["1단계", "2단계"]
        description = "기본 설명"
        
        result = _format_skill_document(skill_name, steps, description, None, None)
        
        # 개요 섹션에 description이 포함되어야 함
        assert "## 개요" in result
        assert description in result
    
    def test_format_skill_document_structure_order(self):
        """문서 구조 순서 확인: 제목 → 개요 → 단계별 실행 절차 → 사용법"""
        skill_name = "테스트 스킬"
        steps = ["1단계", "2단계"]
        description = "기본 설명"
        overview = "스킬 개요"
        usage = "사용법"
        
        result = _format_skill_document(skill_name, steps, description, overview, usage)
        
        # 순서 확인
        title_pos = result.find(f"# {skill_name}")
        overview_pos = result.find("## 개요")
        steps_pos = result.find("## 단계별 실행 절차")
        usage_pos = result.find("## 사용법")
        
        assert title_pos < overview_pos < steps_pos < usage_pos
    
    def test_format_skill_document_empty_steps(self):
        """빈 steps 처리 테스트"""
        skill_name = "테스트 스킬"
        steps = []
        description = "기본 설명"
        
        result = _format_skill_document(skill_name, steps, description)
        
        # frontmatter와 개요는 있음 (빈 steps면 단계별 실행 절차 섹션 없음)
        assert "---" in result
        assert "## 개요" in result
        assert "1. " not in result

    def test_format_skill_document_with_body_markdown(self):
        """body_markdown 제공 시 본문으로 사용되고 overview/steps/usage는 무시됨"""
        skill_name = "my-skill"
        steps = ["Step A", "Step B"]
        description = "Short description."
        overview = "Overview text"
        usage = "Usage text"
        body_markdown = """# My Skill

## Overview

Custom body with **markdown**.

## When to Use

Use when X.

## Steps

1. Do one.
2. Do two.

See `references/guide.md` for details.
"""
        result = _format_skill_document(
            skill_name,
            steps,
            description=description,
            overview=overview,
            usage=usage,
            body_markdown=body_markdown,
        )
        # Frontmatter
        assert "---" in result
        assert "name: my-skill" in result
        assert "description: Short description." in result
        # Body is body_markdown (no fixed 개요/단계별 실행 절차 from overview/steps)
        assert "Custom body with **markdown**" in result
        assert "When to Use" in result
        assert "references/guide.md" in result
        # overview/usage not used as section content
        assert "Overview text" not in result
        assert "Usage text" not in result
        assert "## 사용법" not in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

