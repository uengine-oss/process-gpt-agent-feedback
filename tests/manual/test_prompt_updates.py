"""
프롬프트 업데이트 검증 스크립트
모든 프롬프트에 SKILL 생성 시 필수 포함 사항이 있는지 확인
"""

import sys
import os

# 프로젝트 루트를 PYTHONPATH에 추가
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def check_file_for_keywords(file_path, keywords, description):
    """파일에서 키워드가 포함되어 있는지 확인"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        missing = []
        for keyword in keywords:
            if keyword not in content:
                missing.append(keyword)
        
        if missing:
            print(f"❌ {description}")
            print(f"   파일: {file_path}")
            print(f"   누락된 키워드: {missing}")
            return False
        else:
            print(f"✅ {description}")
            print(f"   파일: {file_path}")
            return True
    except Exception as e:
        print(f"❌ 파일 읽기 실패: {file_path}")
        print(f"   에러: {e}")
        return False


def main():
    """모든 프롬프트 파일 검증"""
    print("=" * 60)
    print("프롬프트 업데이트 검증 시작")
    print("=" * 60)
    
    # 검증할 키워드들
    skill_keywords = [
        "description",
        "overview",
        "steps",
        "usage",
        "additional_files",
        "scripts"
    ]
    
    # 검증할 파일들
    files_to_check = [
        {
            "path": os.path.join(PROJECT_ROOT, "core", "learning_router.py"),
            "description": "learning_router.py - SKILL 생성 필수 포함 사항"
        },
        {
            "path": os.path.join(PROJECT_ROOT, "core", "feedback_chain.py"),
            "description": "feedback_chain.py - SKILL 생성 필수 포함 사항"
        },
        {
            "path": os.path.join(PROJECT_ROOT, "core", "react_agent.py"),
            "description": "react_agent.py - SKILL 생성 필수 포함 사항"
        },
        {
            "path": os.path.join(PROJECT_ROOT, "core", "react_tools.py"),
            "description": "react_tools.py - CommitSkillInput 설명"
        }
    ]
    
    results = []
    for file_info in files_to_check:
        result = check_file_for_keywords(
            file_info["path"],
            skill_keywords,
            file_info["description"]
        )
        results.append(result)
        print()
    
    print("=" * 60)
    if all(results):
        print("✅ 모든 프롬프트가 올바르게 업데이트되었습니다!")
    else:
        print("❌ 일부 프롬프트에 문제가 있습니다.")
    print("=" * 60)
    
    return all(results)


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)

