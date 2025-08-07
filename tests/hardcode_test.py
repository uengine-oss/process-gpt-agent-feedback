#!/usr/bin/env python3
"""
단순한 하드코딩 데이터 피드백 테스트
"""

import asyncio
import sys
import os

# 상위 디렉토리를 Python 경로에 추가
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.polling_manager import process_feedback_task
from core.database import initialize_db

# ============================================================================
# 하드코딩된 테스트 데이터
# ============================================================================

TEST_DATA = {
    'id': 'test_001',
    'user_id': '3b1fe7df-413e-8e3e-9d39-2d018a0c1f58',
    'description': 'orders 테이블에 주문 정보를 저장하고, product 테이블에서 주문된 제품 재고를 조회.',
    'feedback': [
        {
            "시간": "2024-12-20 14:30:15",
            "내용": "주문 정보가 저장이 되지 않고 조회만 되었습니다."
        },
        {
            "시간": "2024-12-20 15:45:22", 
            "내용": "주문 정보가 잘못 저장이 되었습니다. 다른 테이블들의 데이터를 모두 참고해서 orders 테이블의 모든 컬럼값을을 올바르게 저장해주세요. 만약 다른 테이블로도 조회가 불가능한 필드는 임의의 값으로 저장해주세요."
        }
    ]
}

async def main():
    """단순 테스트 실행"""
    
    print("🧪 피드백 처리 테스트 시작")
    print("-" * 40)
    print(f"📋 작업지시: {TEST_DATA['description']}")
    print(f"💬 피드백:")
    for i, fb in enumerate(TEST_DATA['feedback'], 1):
        print(f"   {i}차 ({fb['시간']}): {fb['내용']}")
    print(f"👥 에이전트: {TEST_DATA['user_id']}")
    print("-" * 40)
    
    try:
        # DB 초기화
        initialize_db()
        print("✅ DB 초기화 완료")
        
        # 피드백 처리 실행
        await process_feedback_task(TEST_DATA)
        print("✅ 테스트 완료!")
    except Exception as e:
        print(f"❌ 테스트 실패: {e}")

if __name__ == "__main__":
    asyncio.run(main())