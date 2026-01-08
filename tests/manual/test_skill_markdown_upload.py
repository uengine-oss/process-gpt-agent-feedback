"""
SKILL.md 생성 → ZIP 패키징 → (가능하면) API 업로드까지 테스트하는 스크립트.

목표:
- core.learning_committers.skill_committer._format_skill_document 이 생성한 SKILL.md가
  create_skill_zip / upload_skill 흐름에서 문제 없이 동작하는지 확인.
"""

import json
import os
import sys

# tests/manual/ 기준으로 프로젝트 루트를 PYTHONPATH에 추가
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.learning_committers.skill_committer import _format_skill_document
from core.skill_api_client import create_skill_zip, upload_skill, _get_base_url
from utils.logger import log


def main() -> None:
    skill_name = "테스트 업로드 스킬"
    steps = [
        "첫 번째 단계를 실행한다.",
        "두 번째 단계를 실행한다.",
        "세 번째 단계를 실행한다.",
    ]
    description = "테스트용 스킬로, SKILL.md → ZIP → API 업로드까지의 흐름을 검증합니다."

    # 1) SKILL.md 생성
    skill_md = _format_skill_document(skill_name, steps, description)
    print("==== 생성된 SKILL.md 내용 ====")
    print(skill_md)
    print("================================\n")

    # 2) ZIP 패키징 (HTTP 호출 없이 포맷 검증)
    zip_buffer = create_skill_zip(skill_name, skill_md, additional_files=None)
    print(f"ZIP 바이트 크기: {len(zip_buffer.getvalue())} bytes")

    # 3) 실제 API 업로드 시도 (서버가 떠 있지 않으면 예외를 로깅만 하고 종료)
    base_url = _get_base_url()
    print(f"시도할 SKILL API base URL: {base_url}")

    try:
        result = upload_skill(
            skill_name=skill_name,
            skill_content=skill_md,
            tenant_id="test-agent",
            additional_files=None,
        )
        print("\n[OK] 업로드 성공:")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        # 여기서 나는 에러는 대부분 서버 연결/응답 문제일 수 있으므로,
        # SKILL.md 포맷/ZIP 포맷 문제인지 구분하기 위해 메시지만 보여준다.
        print("\n[WARN] 업로드 중 예외 발생 (대부분 서버 연결 문제일 수 있음):")
        print(repr(e))


if __name__ == "__main__":
    main()


