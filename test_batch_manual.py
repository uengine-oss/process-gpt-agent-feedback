"""
배치 중복 제거 수동 테스트 스크립트
"""

import sys
import os
import asyncio

# 환경 설정
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.logger import log
from core.database import initialize_db, get_all_agents
from core.batch_deduplicator import BatchDeduplicator, collect_agent_knowledge, process_agent
from core.batch_analyzer import generate_deduplication_plan


async def test_collect_knowledge():
    """지식 수집 테스트"""
    log("\n" + "="*60)
    log("테스트 1: 에이전트 지식 수집")
    log("="*60)
    
    try:
        initialize_db()
        
        # 모든 에이전트 조회
        agents = get_all_agents()
        log(f"📋 조회된 에이전트 수: {len(agents)}")
        
        if not agents:
            log("⚠️ 테스트할 에이전트가 없습니다.")
            return
        
        # 첫 번째 에이전트로 테스트
        test_agent = agents[0]
        agent_id = test_agent.get("id")
        agent_name = test_agent.get("name", test_agent.get("username", ""))
        
        log(f"🧪 테스트 에이전트: {agent_name} (ID: {agent_id})")
        
        # 지식 수집
        knowledge = await collect_agent_knowledge(agent_id)
        
        memories_count = len(knowledge.get("memories", []))
        dmn_rules_count = len(knowledge.get("dmn_rules", []))
        skills_count = len(knowledge.get("skills", []))
        
        log(f"✅ 지식 수집 완료:")
        log(f"   - MEMORY: {memories_count}개")
        log(f"   - DMN_RULE: {dmn_rules_count}개")
        log(f"   - SKILL: {skills_count}개")
        
        return agent_id, knowledge
        
    except Exception as e:
        log(f"❌ 지식 수집 테스트 실패: {e}")
        import traceback
        traceback.print_exc()
        return None, None


async def test_generate_plan(agent_id, knowledge):
    """중복 제거 계획 생성 테스트"""
    log("\n" + "="*60)
    log("테스트 2: 중복 제거 계획 생성")
    log("="*60)
    
    if not agent_id or not knowledge:
        log("⚠️ 이전 테스트가 실패하여 건너뜁니다.")
        return None
    
    try:
        memories = knowledge.get("memories", [])
        dmn_rules = knowledge.get("dmn_rules", [])
        skills = knowledge.get("skills", [])
        
        log(f"🧪 계획 생성 시작: memories={len(memories)}, dmn_rules={len(dmn_rules)}, skills={len(skills)}")
        
        # 중복 제거 계획 생성
        plan = await generate_deduplication_plan(agent_id, memories, dmn_rules, skills)
        
        duplicate_groups = plan.get("duplicate_groups", [])
        actions = plan.get("actions", [])
        summary = plan.get("summary", {})
        
        log(f"✅ 계획 생성 완료:")
        log(f"   - 중복 그룹: {len(duplicate_groups)}개")
        log(f"   - 작업 계획: {len(actions)}개")
        log(f"   - 삭제 예정: {summary.get('to_delete', 0)}개")
        log(f"   - 유지 예정: {summary.get('to_keep', 0)}개")
        
        if duplicate_groups:
            log(f"\n📋 중복 그룹 상세:")
            for idx, group in enumerate(duplicate_groups, 1):
                items = group.get("items", [])
                action = group.get("recommended_action", "")
                log(f"   {idx}. {action}")
                for item in items:
                    log(f"      - {item.get('storage')}: {item.get('id')}")
        
        return plan
        
    except Exception as e:
        log(f"❌ 계획 생성 테스트 실패: {e}")
        import traceback
        traceback.print_exc()
        return None


async def test_process_agent_dry_run(agent_id):
    """에이전트 배치 처리 DRY_RUN 모드 테스트"""
    log("\n" + "="*60)
    log("테스트 3: 에이전트 배치 처리 (DRY_RUN)")
    log("="*60)
    
    if not agent_id:
        log("⚠️ 이전 테스트가 실패하여 건너뜁니다.")
        return
    
    try:
        log(f"🧪 DRY_RUN 모드로 배치 처리 시작: agent_id={agent_id}")
        
        result = await process_agent(agent_id, dry_run=True)
        
        if result.get("skipped"):
            log(f"⏭️ 에이전트 건너뛰어짐: {result.get('reason', 'Unknown')}")
        else:
            log(f"✅ DRY_RUN 모드 처리 완료:")
            log(f"   - 삭제 예정: {result.get('to_delete', 0)}개")
            log(f"   - 유지 예정: {result.get('to_keep', 0)}개")
        
        return result
        
    except Exception as e:
        log(f"❌ DRY_RUN 모드 테스트 실패: {e}")
        import traceback
        traceback.print_exc()
        return None


async def test_batch_deduplicator_dry_run():
    """BatchDeduplicator 전체 DRY_RUN 모드 테스트"""
    log("\n" + "="*60)
    log("테스트 4: BatchDeduplicator 전체 실행 (DRY_RUN)")
    log("="*60)
    
    try:
        deduplicator = BatchDeduplicator()
        
        log(f"🧪 모든 에이전트에 대해 DRY_RUN 모드로 실행")
        
        result = await deduplicator.execute_batch_deduplication(agent_id=None, dry_run=True)
        
        if result.get("success"):
            log(f"✅ 전체 배치 처리 완료 (DRY_RUN):")
            log(f"   - 처리된 에이전트: {result.get('processed_agents', 0)}개")
            log(f"   - 총 삭제 예정: {result.get('total_deleted', 0)}개")
            log(f"   - 총 유지 예정: {result.get('total_kept', 0)}개")
            log(f"   - 에러: {result.get('total_errors', 0)}개")
        else:
            log(f"❌ 배치 처리 실패: {result.get('error', 'Unknown error')}")
        
        return result
        
    except Exception as e:
        log(f"❌ 전체 배치 처리 테스트 실패: {e}")
        import traceback
        traceback.print_exc()
        return None


async def main():
    """메인 테스트 함수"""
    log("\n" + "="*60)
    log("배치 중복 제거 테스트 시작")
    log("="*60)
    
    try:
        # 테스트 1: 지식 수집
        agent_id, knowledge = await test_collect_knowledge()
        
        # 테스트 2: 계획 생성
        plan = await test_generate_plan(agent_id, knowledge)
        
        # 테스트 3: 에이전트 배치 처리 (DRY_RUN)
        await test_process_agent_dry_run(agent_id)
        
        # 테스트 4: 전체 배치 처리 (DRY_RUN)
        await test_batch_deduplicator_dry_run()
        
        log("\n" + "="*60)
        log("✅ 모든 테스트 완료!")
        log("="*60)
        
    except Exception as e:
        log(f"\n❌ 테스트 중 오류 발생: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())

