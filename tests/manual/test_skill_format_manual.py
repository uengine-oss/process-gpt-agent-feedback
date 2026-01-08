"""
Skill 마크다운 포맷팅 수동 테스트
개요, 사용법, 스크립트 파일 지원 검증
"""

import sys
import os

# 프로젝트 루트를 PYTHONPATH에 추가
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.learning_committers.skill_committer import _format_skill_document


def test_basic_format():
    """기본 스킬 문서 생성 테스트"""
    print("\n=== 테스트 1: 기본 스킬 문서 생성 ===")
    skill_name = "테스트 스킬"
    steps = ["1단계", "2단계", "3단계"]
    description = "기본 설명"
    
    result = _format_skill_document(skill_name, steps, description)
    print(result)
    
    # 검증
    assert "---" in result, "Frontmatter가 없습니다"
    assert f"name: {skill_name}" in result, "스킬 이름이 없습니다"
    assert f"description: {description}" in result, "설명이 없습니다"
    assert f"# {skill_name}" in result, "제목이 없습니다"
    assert "## 개요" in result, "개요 섹션이 없습니다"
    assert "## 단계별 실행 절차" in result, "단계별 실행 절차 섹션이 없습니다"
    assert "1. 1단계" in result, "1단계가 없습니다"
    print("✅ 기본 스킬 문서 생성 테스트 통과")


def test_with_overview():
    """개요가 포함된 스킬 문서 생성 테스트"""
    print("\n=== 테스트 2: 개요 포함 스킬 문서 생성 ===")
    skill_name = "테스트 스킬"
    steps = ["1단계", "2단계"]
    description = "기본 설명"
    overview = "이 스킬은 특정 작업을 수행하기 위한 상세한 절차입니다."
    
    result = _format_skill_document(skill_name, steps, description, overview)
    print(result)
    
    # 검증
    assert "## 개요" in result, "개요 섹션이 없습니다"
    assert overview in result, "사용자 정의 개요가 없습니다"
    print("✅ 개요 포함 스킬 문서 생성 테스트 통과")


def test_with_usage():
    """사용법이 포함된 스킬 문서 생성 테스트"""
    print("\n=== 테스트 3: 사용법 포함 스킬 문서 생성 ===")
    skill_name = "테스트 스킬"
    steps = ["1단계", "2단계"]
    description = "기본 설명"
    overview = "스킬 개요"
    usage = "이 스킬을 사용할 때는 주의사항을 확인해야 합니다."
    
    result = _format_skill_document(skill_name, steps, description, overview, usage)
    print(result)
    
    # 검증
    assert "## 사용법" in result, "사용법 섹션이 없습니다"
    assert usage in result, "사용법 내용이 없습니다"
    print("✅ 사용법 포함 스킬 문서 생성 테스트 통과")


def test_without_usage():
    """사용법이 없는 경우 사용법 섹션이 없어야 함"""
    print("\n=== 테스트 4: 사용법 없음 테스트 ===")
    skill_name = "테스트 스킬"
    steps = ["1단계", "2단계"]
    description = "기본 설명"
    overview = "스킬 개요"
    
    result = _format_skill_document(skill_name, steps, description, overview, None)
    print(result)
    
    # 검증
    assert "## 사용법" not in result, "사용법 섹션이 있어서는 안 됩니다"
    print("✅ 사용법 없음 테스트 통과")


def test_structure_order():
    """문서 구조 순서 확인"""
    print("\n=== 테스트 5: 문서 구조 순서 확인 ===")
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
    
    assert title_pos < overview_pos < steps_pos < usage_pos, "문서 구조 순서가 잘못되었습니다"
    print("✅ 문서 구조 순서 확인 테스트 통과")


def test_complete_skill_artifact():
    """완전한 skill artifact 예시"""
    print("\n=== 테스트 6: 완전한 skill artifact 예시 ===")
    skill_name = "데이터 처리 스킬"
    description = "데이터를 처리하는 스킬"
    overview = "이 스킬은 다양한 소스에서 데이터를 수집하고, 분석하며, 결과를 보고하는 전체 프로세스를 다룹니다. 특히 대용량 데이터를 효율적으로 처리하는 데 중점을 둡니다."
    usage = "이 스킬을 사용하기 전에 데이터 소스의 접근 권한을 확인하세요. 또한 충분한 디스크 공간이 있는지 확인해야 합니다."
    steps = [
        "데이터 소스에서 원시 데이터를 수집합니다",
        "수집된 데이터를 검증하고 정제합니다",
        "정제된 데이터를 분석합니다",
        "분석 결과를 시각화합니다",
        "결과를 보고서로 작성합니다"
    ]
    
    result = _format_skill_document(skill_name, steps, description, overview, usage)
    print(result)
    
    # 검증
    assert "## 개요" in result
    assert overview in result
    assert "## 사용법" in result
    assert usage in result
    assert "## 단계별 실행 절차" in result
    for i, step in enumerate(steps, start=1):
        assert f"{i}. {step}" in result
    print("✅ 완전한 skill artifact 예시 테스트 통과")


if __name__ == "__main__":
    print("=" * 60)
    print("Skill 마크다운 포맷팅 테스트 시작")
    print("=" * 60)
    
    try:
        test_basic_format()
        test_with_overview()
        test_with_usage()
        test_without_usage()
        test_structure_order()
        test_complete_skill_artifact()
        
        print("\n" + "=" * 60)
        print("✅ 모든 테스트 통과!")
        print("=" * 60)
    except AssertionError as e:
        print(f"\n❌ 테스트 실패: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 예상치 못한 에러: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

