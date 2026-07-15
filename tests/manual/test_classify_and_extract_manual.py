"""
classify_and_extract_proposal() 수동 검증 스크립트 (실제 LLM 호출)

목적: 하나의 결합된 분류+생성 프롬프트가 SKILL/DMN_RULE/PROCESS_DEFINITION을 실제로
구분해내는지 사람이 눈으로 확인한다 (openspec add-feedback-batching tasks.md 4a.6).
CI에서 자동 채점하지 않는다 — LLM 호출 비용과 판단의 주관성 때문에 사람이 출력을 읽고
"이 배치가 이 타입으로 분류된 게 말이 되는가"를 확인하는 용도다.

실행: SUPABASE_URL/KEY 불필요, LLM 자격증명(.env의 LLM_PROXY_URL 또는 OPENAI_API_KEY)만 필요.
    python tests/manual/test_classify_and_extract_manual.py
"""

import asyncio
import json
import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from core.feedback_processor import classify_and_extract_proposal


def _items(*contents):
    return [
        {"todo_id": f"todo-{i}", "content": c, "time": f"2026-07-{10 + i:02d}T09:00:00Z", "user_id": "user-1"}
        for i, c in enumerate(contents)
    ]


CASES = [
    {
        "name": "SKILL 예상 (절차 문제)",
        "expected_types": {"SKILL"},
        "items": _items(
            "보고서를 작성할 때 표 형식 대신 항상 bullet로 정리해줘",
            "이번에도 표로 나왔어요. bullet 형식으로 다시 부탁드려요",
            "여전히 표 형식입니다. 반드시 bullet list로 작성해야 합니다",
        ),
        "task_description": "월간 실적 보고서 작성",
    },
    {
        "name": "DMN_RULE 예상 (조건-결과 비즈니스 규칙)",
        "expected_types": {"DMN_RULE"},
        "items": _items(
            "100만원 넘는 주문은 팀장 승인을 꼭 받아야 해요",
            "이번 건도 150만원인데 승인 없이 진행됐네요, 금액 기준 승인 룰을 지켜주세요",
            "고액 주문(100만원 이상)은 예외 없이 추가 승인 필요합니다",
        ),
        "task_description": "주문 처리",
    },
    {
        "name": "PROCESS_DEFINITION 예상 (흐름/구조 문제)",
        "expected_types": {"PROCESS_DEFINITION"},
        "items": _items(
            "이 단계 앞에 팀장 검토 단계가 아예 빠져있어요, 흐름에 추가해야 합니다",
            "여전히 팀장 검토 없이 바로 다음 단계로 넘어가고 있습니다. 프로세스에 단계를 넣어주세요",
            "구조적으로 검토 활동이 없는 게 문제입니다, 프로세스 정의 자체를 고쳐야 해요",
        ),
        "task_description": "휴가 신청 프로세스",
    },
    {
        "name": "MIXED 예상 (절차 + 비즈니스 규칙 혼합)",
        "expected_types": {"SKILL", "DMN_RULE"},
        "items": _items(
            "응답할 때 항상 결론을 먼저 쓰고 근거를 나중에 써줘",
            "금액이 500만원 넘으면 자동으로 반려 처리해야 해요",
            "이번에도 근거부터 나왔어요, 결론 먼저 부탁",
            "600만원짜리 건이 반려 안 되고 통과됐어요, 500만원 기준 꼭 지켜주세요",
        ),
        "task_description": "지출 결의 검토",
    },
    {
        "name": "무관 피드백 (공통 관심사 없음, discard 예상)",
        "expected_types": set(),
        "items": _items(
            "오늘 날씨가 좋네요",
            "이 메뉴 색깔이 마음에 안 들어요",
            "다른 부서 얘기인데 참고만 하세요",
        ),
        "task_description": "",
    },
]


async def run_case(case: dict) -> bool:
    print("=" * 70)
    print(f"케이스: {case['name']}")
    print(f"기대 target 종류: {sorted(case['expected_types']) or '(없음, discard)'}")
    targets = await classify_and_extract_proposal(case["items"], case["task_description"])
    got_types = {t.get("type") for t in targets}
    print(f"실제 target 종류: {sorted(got_types) or '(없음, discard)'}")
    for t in targets:
        artifact = t.get("artifact")
        preview = json.dumps(artifact, ensure_ascii=False)[:300] if isinstance(artifact, dict) else str(artifact)[:300]
        print(f"  - {t.get('type')}: {preview}")
    ok = got_types == case["expected_types"]
    print("✅ 일치" if ok else "❌ 불일치 (사람 판단으로 재검토 필요 — LLM 분류는 완벽하지 않음)")
    print()
    return ok


async def main():
    results = [await run_case(case) for case in CASES]
    print("=" * 70)
    print(f"{sum(results)}/{len(results)} 케이스가 기대한 target 종류와 일치했습니다.")
    print("불일치가 있어도 실패로 보지 마세요 — 분류 프롬프트를 다듬을 신호로 활용하세요.")


if __name__ == "__main__":
    asyncio.run(main())
