#!/usr/bin/env python3
"""
실제 데이터로 todo 피드백 테스트
"""

import asyncio
from core.database import initialize_db, fetch_feedback_task_by_id
from core.polling_manager import process_feedback_task
from utils.logger import log

# ============================================================================
# 여기에 테스트할 TODO ID 입력
# ============================================================================
TODO_ID = "실제_todo_id_입력"

async def test_single_todo():
    """실제 DB의 단일 todo 피드백 처리 테스트"""
    
    print(f"🧪 TODO {TODO_ID} 피드백 처리 테스트 시작")
    
    # 1. DB 연결
    try:
        initialize_db()
        print("✅ DB 연결 성공")
    except Exception as e:
        print(f"❌ DB 연결 실패: {e}")
        return
    
    # 2. todo 조회
    try:
        row = await fetch_feedback_task_by_id(TODO_ID)
        if not row:
            print(f"❌ TODO {TODO_ID}를 찾을 수 없습니다")
            return
        
        print("✅ TODO 조회 성공")
        print("-" * 50)
        print(f"📋 작업지시사항: {row.get('description', 'N/A')}")
        print(f"💬 피드백: {row.get('feedback', 'N/A')}")
        print(f"👥 에이전트: {row.get('user_id', 'N/A')}")
        print(f"📊 상태: {row.get('draft_status', 'N/A')}")
        print("-" * 50)
        
    except Exception as e:
        print(f"❌ TODO 조회 실패: {e}")
        return
    
    # 3. 피드백 처리 실행
    try:
        print("⚙️  피드백 처리 시작...")
        await process_feedback_task(row)
        print("🎉 피드백 처리 완료!")
        
    except Exception as e:
        print(f"❌ 피드백 처리 실패: {e}")

if __name__ == "__main__":
    if TODO_ID == "실제_todo_id_입력":
        print("❌ TODO_ID를 입력하세요")
        print("   예시: TODO_ID = '12345'")
    else:
        asyncio.run(test_single_todo())