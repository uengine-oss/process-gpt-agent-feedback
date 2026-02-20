"""
검색용 한→영 번역 유틸리티

- 이중 벡터 검색 전략의 영문 검색 쿼리 생성용
  · 한글 원문 검색: 업로드된 스킬(한글 설명)과의 매칭
  · 영문 번역 검색: 내장 스킬(영어 설명)과의 매칭
- 두 검색 결과를 병합하여 한글·영문 스킬 모두 커버
- 최종 저장(MEMORY, SKILL, DMN)에는 적용하지 않음
"""

import os
import re
from typing import Optional

from utils.logger import log


def _is_mainly_korean(text: str) -> bool:
    """한글이 주를 이루는지 판단. 히라가나/가타카나 등 다른 CJK는 제외."""
    if not text or not text.strip():
        return False
    # 한글 유니코드 범위: \uAC00-\uD7A3 (완성형), \u1100-\u11FF (자모)
    korean_pattern = re.compile(r"[\uAC00-\uD7A3\u1100-\u11FF]")
    letters = [c for c in text if c.isalnum() or c in " \t\n"]
    if not letters:
        return False
    korean_count = sum(1 for c in letters if korean_pattern.search(c))
    return korean_count / len(letters) >= 0.3


async def translate_ko_to_en_for_search(text: str) -> str:
    """
    검색용으로 한글 텍스트에서 핵심 키워드를 추출해 영어로만 출력.
    문장 번역 대신 역할·작업·도메인 키워드만 나열해 길이를 줄이고 임베딩 매칭을 유리하게 함.
    한글이 주를 이루지 않으면 원문 반환. 실패 시 원문 반환.

    Args:
        text: 원본 텍스트 (한글 또는 혼합)

    Returns:
        쉼표 구분 영어 키워드 문자열 또는 원문
    """
    if not text or not text.strip():
        return text

    if not _is_mainly_korean(text):
        return text

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        log("   ⚠️ OPENAI_API_KEY 없음, 검색용 번역 건너뜀")
        return text

    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=api_key)
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "You are a keyword extractor for skill search. Given Korean text (goal, persona, or task description), output only the key concepts in English as a single line: roles, tasks, domains, and deliverables. Use comma-separated keywords or short phrases only (e.g., market research, strategy report, data analysis, visual charts, PDF). No full sentences, no explanations. Keep under 300 characters.",
                },
                {"role": "user", "content": text},
            ],
            max_tokens=200,
        )
        translated = (
            response.choices[0].message.content.strip()
            if response.choices and response.choices[0].message
            else ""
        )
        if translated:
            # 한 줄로 정리 (줄바꿈을 쉼표+공백으로)
            translated = " ".join(translated.replace("\n", ", ").split())
            log(f"   🌐 검색용 한→영 키워드 적용 ({len(text)}자 → {len(translated)}자)")
            return translated
    except Exception as e:
        log(f"   ⚠️ 검색용 번역 실패 (원문 사용): {e}")

    return text
