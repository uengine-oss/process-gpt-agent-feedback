"""
배치 중복 제거 스케줄러 모듈
주기적으로 배치 중복 제거를 실행하는 스케줄러
"""

import os
import asyncio
from utils.logger import log, handle_error
from core.batch_deduplicator import BatchDeduplicator
from core.database import get_all_agents


async def start_batch_deduplication():
    """
    주기적 배치 중복 제거 스케줄러 시작
    
    환경 변수:
    - BATCH_DEDUP_ENABLED: 활성화 여부 (기본: true)
    - BATCH_DEDUP_INTERVAL_SECONDS: 실행 주기 (기본: 3600)
    - BATCH_DEDUP_DRY_RUN: DRY_RUN 모드 (기본: false)
    """
    enabled = os.getenv("BATCH_DEDUP_ENABLED", "true").lower() == "true"
    if not enabled:
        log("배치 중복 제거 스케줄러 비활성화됨 (BATCH_DEDUP_ENABLED=false)")
        return
    
    interval = int(os.getenv("BATCH_DEDUP_INTERVAL_SECONDS", "3600"))
    dry_run = os.getenv("BATCH_DEDUP_DRY_RUN", "false").lower() == "true"
    
    log(f"🕐 배치 중복 제거 스케줄러 시작: 간격={interval}초, DRY_RUN={dry_run}")
    
    while True:
        try:
            await run_batch_deduplication_once(dry_run=dry_run)
        except Exception as e:
            log(f"⚠️ 배치 중복 제거 실행 중 에러 (다음 주기까지 대기): {e}")
            handle_error("배치스케줄러", e)
        
        await asyncio.sleep(interval)


async def run_batch_deduplication_once(dry_run: bool = False):
    """모든 에이전트에 대해 에이전트별 배치 중복 제거 실행"""
    try:
        log(f"🔄 배치 중복 제거 실행 시작 (DRY_RUN={dry_run}, 에이전트별 배치)")
        deduplicator = BatchDeduplicator()

        agents = get_all_agents()
        if not agents:
            log("⚠️ 배치 중복 제거 대상 에이전트가 없음")
            return

        total_processed = 0
        total_deleted = 0
        total_kept = 0

        for agent in agents:
            agent_id = agent.get("id")
            if not agent_id:
                continue

            try:
                result = await deduplicator.execute_batch_deduplication(agent_id=agent_id, dry_run=dry_run)
                if result.get("success"):
                    total_processed += result.get("processed_agents", 0)
                    total_deleted += result.get("total_deleted", 0)
                    total_kept += result.get("total_kept", 0)
                else:
                    log(f"⚠️ 에이전트 {agent_id} 배치 중복 제거 실패: {result.get('error', 'Unknown error')}")
            except Exception as e:
                log(f"⚠️ 에이전트 {agent_id} 배치 중복 제거 실행 중 에러: {e}")
                handle_error(f"배치중복제거_에이전트_{agent_id}", e)

        log(f"✅ 배치 중복 제거 실행 완료 (에이전트별): 처리={total_processed}, 삭제={total_deleted}, 유지={total_kept}")
            
    except Exception as e:
        log(f"❌ 배치 중복 제거 실행 중 예상치 못한 에러: {e}")
        handle_error("배치중복제거실행", e)
        raise

