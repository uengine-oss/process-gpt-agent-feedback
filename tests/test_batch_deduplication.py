"""
배치 중복 제거 테스트
"""

import pytest
import asyncio
from core.batch_deduplicator import BatchDeduplicator, collect_agent_knowledge, process_agent
from core.database import get_all_agents, _get_agent_by_id
from core.batch_analyzer import generate_deduplication_plan
from utils.logger import log


@pytest.mark.asyncio
async def test_collect_agent_knowledge():
    """에이전트 지식 수집 테스트"""
    # 모든 에이전트 조회
    agents = get_all_agents()
    
    if not agents:
        log("⚠️ 테스트할 에이전트가 없습니다. 테스트를 건너뜁니다.")
        pytest.skip("테스트할 에이전트가 없음")
    
    # 첫 번째 에이전트로 테스트
    test_agent = agents[0]
    agent_id = test_agent.get("id")
    
    log(f"🧪 테스트 에이전트: {agent_id}")
    
    # 지식 수집
    knowledge = await collect_agent_knowledge(agent_id)
    
    assert "memories" in knowledge
    assert "dmn_rules" in knowledge
    assert "skills" in knowledge
    
    log(f"✅ 지식 수집 테스트 성공: memories={len(knowledge['memories'])}, dmn_rules={len(knowledge['dmn_rules'])}, skills={len(knowledge['skills'])}")


@pytest.mark.asyncio
async def test_generate_deduplication_plan():
    """중복 제거 계획 생성 테스트"""
    # 모든 에이전트 조회
    agents = get_all_agents()
    
    if not agents:
        log("⚠️ 테스트할 에이전트가 없습니다. 테스트를 건너뜁니다.")
        pytest.skip("테스트할 에이전트가 없음")
    
    # 첫 번째 에이전트로 테스트
    test_agent = agents[0]
    agent_id = test_agent.get("id")
    
    log(f"🧪 테스트 에이전트: {agent_id}")
    
    # 지식 수집
    knowledge = await collect_agent_knowledge(agent_id)
    
    memories = knowledge.get("memories", [])
    dmn_rules = knowledge.get("dmn_rules", [])
    skills = knowledge.get("skills", [])
    
    # 중복 제거 계획 생성
    plan = await generate_deduplication_plan(agent_id, memories, dmn_rules, skills)
    
    assert "agent_id" in plan
    assert "total_knowledge_count" in plan
    assert "duplicate_groups" in plan
    assert "actions" in plan
    assert "summary" in plan
    
    log(f"✅ 중복 제거 계획 생성 테스트 성공: 삭제={plan['summary'].get('to_delete', 0)}, 유지={plan['summary'].get('to_keep', 0)}")


@pytest.mark.asyncio
async def test_process_agent_dry_run():
    """에이전트 배치 처리 DRY_RUN 모드 테스트"""
    # 모든 에이전트 조회
    agents = get_all_agents()
    
    if not agents:
        log("⚠️ 테스트할 에이전트가 없습니다. 테스트를 건너뜁니다.")
        pytest.skip("테스트할 에이전트가 없음")
    
    # 첫 번째 에이전트로 테스트
    test_agent = agents[0]
    agent_id = test_agent.get("id")
    
    log(f"🧪 테스트 에이전트: {agent_id} (DRY_RUN 모드)")
    
    # DRY_RUN 모드로 처리
    result = await process_agent(agent_id, dry_run=True)
    
    assert "agent_id" in result
    assert result["agent_id"] == agent_id
    
    if result.get("skipped"):
        log(f"⏭️ 에이전트 {agent_id}는 건너뛰어짐: {result.get('reason', 'Unknown')}")
    else:
        assert "dry_run" in result
        assert result.get("dry_run") == True
        assert "plan" in result
        
        log(f"✅ DRY_RUN 모드 테스트 성공: 삭제 예정={result.get('to_delete', 0)}, 유지 예정={result.get('to_keep', 0)}")


@pytest.mark.asyncio
async def test_batch_deduplicator_dry_run():
    """BatchDeduplicator DRY_RUN 모드 테스트"""
    deduplicator = BatchDeduplicator()
    
    # DRY_RUN 모드로 실행
    result = await deduplicator.execute_batch_deduplication(agent_id=None, dry_run=True)
    
    assert "success" in result
    assert result.get("dry_run") == True
    assert "processed_agents" in result
    assert "results" in result
    
    log(f"✅ BatchDeduplicator DRY_RUN 테스트 성공: 처리된 에이전트={result.get('processed_agents', 0)}")


@pytest.mark.asyncio
async def test_batch_deduplicator_single_agent():
    """특정 에이전트만 처리 테스트"""
    # 모든 에이전트 조회
    agents = get_all_agents()
    
    if not agents:
        log("⚠️ 테스트할 에이전트가 없습니다. 테스트를 건너뜁니다.")
        pytest.skip("테스트할 에이전트가 없음")
    
    # 첫 번째 에이전트로 테스트
    test_agent = agents[0]
    agent_id = test_agent.get("id")
    
    log(f"🧪 특정 에이전트 배치 처리 테스트: {agent_id} (DRY_RUN 모드)")
    
    deduplicator = BatchDeduplicator()
    
    # 특정 에이전트만 DRY_RUN 모드로 실행
    result = await deduplicator.execute_batch_deduplication(agent_id=agent_id, dry_run=True)
    
    assert "success" in result
    assert result.get("dry_run") == True
    assert result.get("processed_agents", 0) >= 0
    
    log(f"✅ 특정 에이전트 배치 처리 테스트 성공: 처리된 에이전트={result.get('processed_agents', 0)}")


if __name__ == "__main__":
    # 직접 실행 시
    import sys
    import os
    
    # 환경 설정
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    
    async def run_tests():
        log("🧪 배치 중복 제거 테스트 시작")
        
        try:
            # 테스트 1: 지식 수집
            log("\n=== 테스트 1: 지식 수집 ===")
            await test_collect_agent_knowledge()
            
            # 테스트 2: 중복 제거 계획 생성
            log("\n=== 테스트 2: 중복 제거 계획 생성 ===")
            await test_generate_deduplication_plan()
            
            # 테스트 3: 에이전트 배치 처리 (DRY_RUN)
            log("\n=== 테스트 3: 에이전트 배치 처리 (DRY_RUN) ===")
            await test_process_agent_dry_run()
            
            # 테스트 4: BatchDeduplicator (DRY_RUN)
            log("\n=== 테스트 4: BatchDeduplicator (DRY_RUN) ===")
            await test_batch_deduplicator_dry_run()
            
            # 테스트 5: 특정 에이전트 처리
            log("\n=== 테스트 5: 특정 에이전트 처리 ===")
            await test_batch_deduplicator_single_agent()
            
            log("\n✅ 모든 테스트 완료!")
            
        except Exception as e:
            log(f"\n❌ 테스트 실패: {e}")
            import traceback
            traceback.print_exc()
    
    asyncio.run(run_tests())

