"""
배치 중복 분석 모듈
에이전트의 모든 지식을 분석하여 중복을 검출하고 정리 계획을 생성
"""

import json
from typing import Dict, List, Optional
from llm_factory import create_llm
from utils.logger import log, handle_error


def clean_json_response(content: str) -> str:
    """LLM 응답에서 백틱과 json 키워드 제거"""
    content = content.replace("```json", "").replace("```", "")
    return content.strip()


def _format_knowledge_for_analysis(knowledge_items: List[Dict], storage_type: str) -> str:
    """
    지식 항목들을 분석용 텍스트로 포맷팅
    
    Args:
        knowledge_items: 지식 항목 리스트
        storage_type: 저장소 타입 ("MEMORY", "DMN_RULE", "SKILL")
    
    Returns:
        포맷팅된 텍스트
    """
    if not knowledge_items:
        return "**지식 없음**"
    
    formatted = []
    for idx, item in enumerate(knowledge_items, 1):
        if storage_type == "MEMORY":
            memory_id = item.get("id", "")
            memory_content = item.get("memory", item.get("content", ""))
            # full_content에 전체 내용 저장 (이동 시 필요)
            formatted.append(f"{idx}. ID: {memory_id}\n   내용: {memory_content}")
        
        elif storage_type == "DMN_RULE":
            rule_id = item.get("id", "")
            rule_name = item.get("name", "")
            # DMN XML에서 조건과 액션 추출 시도 (간단한 파싱)
            bpmn = item.get("bpmn", "")
            # full_content에 전체 XML 저장 (이동 시 필요)
            formatted.append(f"{idx}. ID: {rule_id}\n   이름: {rule_name}\n   XML: {bpmn[:200]}..." if len(bpmn) > 200 else f"{idx}. ID: {rule_id}\n   이름: {rule_name}\n   XML: {bpmn}")
        
        elif storage_type == "SKILL":
            skill_id = item.get("id", item.get("name", ""))
            skill_name = item.get("name", skill_id)
            skill_desc = item.get("description", "")
            skill_content = item.get("content", "")
            skill_overview = item.get("overview", "")
            skill_steps = item.get("steps", [])
            
            # 스킬의 전체 정보를 포함하여 포맷팅
            # content가 있으면 전체 사용 (제한 없음), 없으면 description과 steps 조합
            if skill_content:
                content_text = skill_content
            else:
                content_parts = []
                if skill_overview:
                    content_parts.append(f"개요: {skill_overview}")
                if skill_desc:
                    content_parts.append(f"설명: {skill_desc}")
                if skill_steps:
                    steps_text = "\n".join([f"  {i+1}. {step}" for i, step in enumerate(skill_steps)])
                    content_parts.append(f"단계:\n{steps_text}")
                content_text = "\n".join(content_parts) if content_parts else skill_desc
            
            # steps 정보도 별도로 포함 (있는 경우)
            steps_info = ""
            if skill_steps and isinstance(skill_steps, list):
                steps_info = f"\n   단계별 절차:\n" + "\n".join([f"     {i+1}. {step}" for i, step in enumerate(skill_steps)])
            
            formatted.append(f"{idx}. ID: {skill_id}\n   이름: {skill_name}\n   설명: {skill_desc}\n   전체 내용:\n{content_text}{steps_info}")
    
    return "\n\n".join(formatted)


async def analyze_cross_storage_duplicates(
    memories: List[Dict],
    dmn_rules: List[Dict],
    skills: List[Dict]
) -> Dict:
    """
    교차 저장소 중복 분석 (LLM 기반)
    
    Args:
        memories: MEMORY 항목 리스트
        dmn_rules: DMN_RULE 항목 리스트
        skills: SKILL 항목 리스트
    
    Returns:
        {
            "duplicate_groups": [
                {
                    "items": [
                        {"id": "...", "storage": "DMN_RULE", "content_summary": "..."},
                        {"id": "...", "storage": "MEMORY", "content_summary": "..."}
                    ],
                    "similarity_score": 0.95,
                    "recommended_action": "KEEP_DMN_RULE_DELETE_MEMORY"
                }
            ]
        }
    """
    try:
        # 지식이 하나도 없으면 빈 결과 반환
        total_count = len(memories) + len(dmn_rules) + len(skills)
        if total_count == 0:
            log("📝 분석할 지식이 없음")
            return {"duplicate_groups": []}
        
        # 지식이 너무 많으면 배치로 나누어 처리하는 것이 좋지만, 일단 전체를 한 번에 분석
        memories_text = _format_knowledge_for_analysis(memories, "MEMORY")
        dmn_rules_text = _format_knowledge_for_analysis(dmn_rules, "DMN_RULE")
        skills_text = _format_knowledge_for_analysis(skills, "SKILL")
        
        llm = create_llm(model="gpt-4o", streaming=False, temperature=0)
        
        prompt = f"""당신은 지식 저장소 간/내부 중복을 분석하는 전문가입니다.

다음은 하나의 에이전트가 보유한 모든 지식입니다:

**MEMORY (기억) 항목:**
{memories_text}

**DMN_RULE (의사결정 규칙) 항목:**
{dmn_rules_text}

**SKILL (실행 규칙) 항목:**
{skills_text}

**작업:**
위 지식들을 분석하여 다음 세 가지를 수행해주세요:
1. 동일한 의미를 가진 지식이 **서로 다른 저장소**에 중복 저장되어 있는지 분석 (외부 중복)
2. 동일한 의미를 가진 지식이 **같은 저장소 내부**에서 여러 번 저장되어 있는지 분석 (내부 중복)
3. 각 지식이 적절한 저장소에 있는지 평가 (잘못된 저장소에 있는 경우 이동 필요)

**저장소별 적합성 기준:**
- **DMN_RULE (의사결정 규칙)**: "만약 X라면 Y한다" 형태의 조건-행동 규칙
  - 예: "만약 사용자 유형이 VIP라면 부장님이라고 호칭한다"
  - 예: "만약 주문 금액이 10만원 이상이면 할인을 적용한다"
- **SKILL (실행 규칙)**: 단계별 절차, 실행 방법, "먼저 X하고, 그 다음 Y한다"
  - 예: "주문 처리 절차: 1단계 주문 확인, 2단계 결제 처리, 3단계 배송 준비"
  - 예: "데이터 분석 방법: 1) 데이터 수집, 2) 데이터 전처리, 3) 모델 학습"
- **MEMORY (기억)**: 개인의 선호도, 경험, 가이드라인, 맥락 정보 (가장 낮은 우선순위)
  - 예: "특정 사용자 A는 부장님이라고 불러야 함" (개인 선호도)
  - 예: "프로젝트 진행 시 주의사항" (가이드라인)

**중복 판단 기준:**
- 동일한 의미를 가진 지식이 여러 저장소에 있으면 외부 중복으로 간주
- 동일한 의미를 가진 지식이 같은 저장소 내에 여러 개 있으면 내부 중복으로 간주
- **언어 차이 무시**: 영문/한글 차이는 무시하고 의미적 동일성만 판단
  - 예: "Use 'Manager' for user X" (영문)과 "사용자 X에게는 부장님이라고 호칭한다" (한글)은 의미가 같으므로 중복으로 간주
  - 예: "If user type is VIP, apply discount" (영문)과 "사용자 유형이 VIP이면 할인을 적용한다" (한글)은 의미가 같으므로 중복으로 간주
  - 예: "Order processing: Step 1 confirm, Step 2 payment" (영문)과 "주문 처리: 1단계 확인, 2단계 결제" (한글)은 의미가 같으므로 중복으로 간주
- **스킬 이름 차이 무시**: SKILL의 경우 이름이 다르더라도 내용(description, steps, content)이 동일하면 중복으로 간주
  - 예: "Document Summarization and Storage" (영문 이름)과 "문서 요약 및 저장" (한글 이름)이 같은 단계별 절차를 설명하면 중복으로 간주
  - 예: "Order Processing" (영문)과 "주문 처리" (한글)이 같은 절차를 설명하면 중복으로 간주
  - **중요**: 스킬 이름보다 description, steps, content의 의미적 동일성을 우선 판단
- 예: "특정 사용자에게 부장님이라는 호칭 사용"이라는 규칙이 DMN_RULE과 MEMORY에 모두 있으면 외부 중복
- 예: 동일한 DMN 규칙이 DMN_RULE에 여러 개 존재하면 내부 중복

**저장소 우선순위:**
- DMN_RULE (의사결정 규칙) > SKILL (실행 규칙) > MEMORY (기억)
- 중복된 경우 가장 높은 우선순위 저장소의 지식만 유지하고 나머지는 삭제해야 함

**이동(MOVE) 필요 판단:**
- 지식이 적절하지 않은 저장소에 있는 경우 이동이 필요함
- 예: "만약 사용자 유형이 VIP라면 부장님이라고 호칭한다"라는 규칙이 MEMORY에 있으면 DMN_RULE로 이동 필요
- 예: "주문 처리 절차: 1단계... 2단계..."가 DMN_RULE에 있으면 SKILL로 이동 필요

**응답 형식:**
JSON 형식으로만 응답하세요. 마크다운 코드블록은 사용하지 마세요.

{{
  "duplicate_groups": [
    {{
      "items": [
        {{"id": "지식ID", "storage": "MEMORY|DMN_RULE|SKILL", "content_summary": "지식 내용 요약", "full_content": "전체 내용"}},
        {{"id": "지식ID", "storage": "MEMORY|DMN_RULE|SKILL", "content_summary": "지식 내용 요약", "full_content": "전체 내용"}}
      ],
      "similarity_score": 0.95,
      "recommended_action": "KEEP_DMN_RULE_DELETE_MEMORY|KEEP_SKILL_DELETE_MEMORY|KEEP_DMN_RULE_DELETE_SKILL"
    }}
  ],
  "internal_duplicate_groups": [
    {{
      "storage": "MEMORY|DMN_RULE|SKILL",
      "items": [
        {{"id": "지식ID", "content_summary": "지식 내용 요약", "full_content": "전체 내용"}},
        {{"id": "지식ID", "content_summary": "지식 내용 요약", "full_content": "전체 내용"}}
      ],
      "similarity_score": 0.98,
      "keep_ids": ["유지할 지식ID1", "유지할 지식ID2"],
      "delete_ids": ["삭제할 지식ID1", "삭제할 지식ID2"]
    }}
  ],
  "mismatch_items": [
    {{
      "id": "지식ID",
      "storage": "MEMORY|DMN_RULE|SKILL",
      "content_summary": "지식 내용 요약",
      "full_content": "전체 내용",
      "current_storage": "MEMORY",
      "recommended_storage": "DMN_RULE",
      "reason": "조건-행동 규칙이므로 DMN_RULE이 적합"
    }}
  ]
}}

**중요:**
- recommended_action 형식: KEEP_[높은우선순위저장소]_DELETE_[낮은우선순위저장소]
- 중복 그룹이 없으면 duplicate_groups를 빈 배열로 반환
- internal_duplicate_groups는 같은 저장소 내부에서 의미적으로 중복된 항목 그룹
- internal_duplicate_groups의 keep_ids에는 "유지해야 할" 항목 ID, delete_ids에는 "삭제해도 되는" 항목 ID만 포함
- mismatch_items: 현재 저장소가 적합하지 않은 지식 목록 (이동 필요)
- mismatch_items의 recommended_storage는 current_storage와 달라야 함
- full_content는 나중에 이동 시 필요한 전체 내용을 포함해야 함 (가능한 경우)
- **언어 차이 무시**: 영문/한글 차이는 무시하고 의미적 동일성만 판단 (외부 중복, 내부 중복 모두 적용)
- **스킬 이름 차이 무시**: SKILL의 경우 이름이 다르더라도 내용이 동일하면 중복으로 판단 (내부 중복에 특히 중요)
- **SKILL 중복 판단 시**: 이름보다 description, steps, content의 의미적 동일성을 우선 판단
- 확실하지 않은 경우는 중복으로 판단하지 않거나 이동을 추천하지 않음
- 각 그룹은 2개 이상의 항목을 포함해야 함
"""

        response = await llm.ainvoke(prompt)
        cleaned_content = clean_json_response(response.content)
        
        log(f"🤖 교차 저장소 중복 분석 LLM 응답 (일부): {cleaned_content[:500]}...")
        
        parsed_result = json.loads(cleaned_content)
        duplicate_groups = parsed_result.get("duplicate_groups", [])
        mismatch_items = parsed_result.get("mismatch_items", [])
        internal_duplicate_groups = parsed_result.get("internal_duplicate_groups", [])
        
        log(
            f"📊 교차/내부 중복 분석 완료: "
            f"교차중복={len(duplicate_groups)}개 그룹, "
            f"내부중복={len(internal_duplicate_groups)}개 그룹, "
            f"이동필요={len(mismatch_items)}개"
        )
        
        return {
            "duplicate_groups": duplicate_groups,
            "mismatch_items": mismatch_items,
            "internal_duplicate_groups": internal_duplicate_groups,
        }
        
    except json.JSONDecodeError as e:
        log(f"❌ 교차 저장소 중복 분석 JSON 파싱 실패: {e}")
        handle_error("교차저장소중복분석 JSON 파싱", e)
        return {"duplicate_groups": []}
    except Exception as e:
        handle_error("교차저장소중복분석", e)
        return {"duplicate_groups": []}


async def generate_deduplication_plan(
    agent_id: str,
    memories: List[Dict],
    dmn_rules: List[Dict],
    skills: List[Dict]
) -> Dict:
    """
    중복 제거 계획 생성
    
    Args:
        agent_id: 에이전트 ID
        memories: MEMORY 항목 리스트
        dmn_rules: DMN_RULE 항목 리스트
        skills: SKILL 항목 리스트
    
    Returns:
        {
            "agent_id": "...",
            "total_knowledge_count": {
                "memory": 10,
                "dmn_rule": 5,
                "skill": 3
            },
            "duplicate_groups": [...],
            "actions": [
                {"operation": "DELETE", "storage": "MEMORY", "id": "..."},
                {"operation": "KEEP", "storage": "DMN_RULE", "id": "..."}
            ],
            "summary": {
                "to_delete": 5,
                "to_keep": 13
            }
        }
    """
    try:
        log(f"📋 중복 제거 계획 생성 시작: agent_id={agent_id}")
        
        # 교차 저장소 중복 분석 및 저장소 적합성 평가
        cross_storage_result = await analyze_cross_storage_duplicates(
            memories, dmn_rules, skills
        )
        duplicate_groups = cross_storage_result.get("duplicate_groups", [])
        mismatch_items = cross_storage_result.get("mismatch_items", [])
        internal_duplicate_groups = cross_storage_result.get("internal_duplicate_groups", [])
        
        # 정리 작업 계획 생성
        actions = []
        items_to_delete = set()  # 삭제할 항목 추적 (중복 제거용)
        items_to_move = {}  # 이동할 항목 추적 (원본 ID -> 이동 정보)
        
        for group in duplicate_groups:
            items = group.get("items", [])
            recommended_action = group.get("recommended_action", "")
            
            if len(items) < 2:
                continue
            
            # recommended_action 파싱: "KEEP_DMN_RULE_DELETE_MEMORY"
            parts = recommended_action.split("_")
            if len(parts) >= 4 and parts[0] == "KEEP" and parts[2] == "DELETE":
                keep_storage = parts[1]  # "DMN_RULE", "SKILL", "MEMORY"
                delete_storage = parts[3]  # "MEMORY", "SKILL", "DMN_RULE"
                
                # KEEP 항목 찾기
                keep_items = [item for item in items if item.get("storage") == keep_storage]
                delete_items = [item for item in items if item.get("storage") == delete_storage]
                
                # KEEP 항목은 유지
                for item in keep_items:
                    item_id = item.get("id")
                    if item_id and item_id not in items_to_delete:
                        actions.append({
                            "operation": "KEEP",
                            "storage": keep_storage,
                            "id": item_id,
                            "content_summary": item.get("content_summary", "")
                        })
                
                # DELETE 항목은 삭제
                for item in delete_items:
                    item_id = item.get("id")
                    if item_id:
                        items_to_delete.add(item_id)
                        actions.append({
                            "operation": "DELETE",
                            "storage": delete_storage,
                            "id": item_id,
                            "content_summary": item.get("content_summary", ""),
                            "reason": f"중복 제거: {keep_storage}에 동일한 내용이 있음"
                        })
        
        # 저장소 불일치 항목 처리 (이동 필요)
        for mismatch in mismatch_items:
            item_id = mismatch.get("id")
            current_storage = mismatch.get("current_storage")
            recommended_storage = mismatch.get("recommended_storage")
            content_summary = mismatch.get("content_summary", "")
            full_content = mismatch.get("full_content", "")
            
            if not item_id or not current_storage or not recommended_storage:
                continue
            
            if current_storage == recommended_storage:
                continue  # 이미 적절한 저장소에 있음
            
            # 이동 작업 추가 (원본은 삭제, 대상 저장소에 생성)
            if item_id not in items_to_delete:
                items_to_delete.add(item_id)
                items_to_move[item_id] = {
                    "from_storage": current_storage,
                    "to_storage": recommended_storage,
                    "content_summary": content_summary,
                    "full_content": full_content,
                    "reason": mismatch.get("reason", f"{recommended_storage}가 더 적합한 저장소")
                }
                
                actions.append({
                    "operation": "MOVE",
                    "from_storage": current_storage,
                    "to_storage": recommended_storage,
                    "id": item_id,
                    "content_summary": content_summary,
                    "full_content": full_content,
                    "reason": mismatch.get("reason", f"{recommended_storage}가 더 적합한 저장소")
                })
        
        # -------------------------------
        # 3. 동일 저장소 내부 중복 제거 (LLM 결과 기반)
        # -------------------------------
        # internal_duplicate_groups는 storage별로 의미적으로 중복된 항목들을 묶어주고,
        # 그 안에서 어떤 ID를 유지하고 어떤 ID를 삭제할지(keep_ids/delete_ids)를 알려준다.

        for group in internal_duplicate_groups:
            storage = group.get("storage")
            if storage not in ["MEMORY", "DMN_RULE", "SKILL"]:
                continue

            keep_ids = group.get("keep_ids", []) or []
            delete_ids = group.get("delete_ids", []) or []

            # KEEP 항목 추가 (이미 DELETE/MOVE로 표시되지 않은 경우만)
            for kid in keep_ids:
                if not kid or kid in items_to_delete or kid in items_to_move:
                    continue
                existing = any(
                    a.get("id") == kid and a.get("storage") == storage and a.get("operation") == "KEEP"
                    for a in actions
                )
                if not existing:
                    actions.append({
                        "operation": "KEEP",
                        "storage": storage,
                        "id": kid,
                    })

            # DELETE 항목 추가
            for did in delete_ids:
                if not did:
                    continue
                if did in items_to_delete:
                    continue
                items_to_delete.add(did)
                actions.append({
                    "operation": "DELETE",
                    "storage": storage,
                    "id": did,
                    "reason": "동일 저장소 내부 의미 중복 (LLM 판별)"
                })

        # -------------------------------
        # 4. 삭제되지 않은 모든 항목은 유지 (이동 대상 제외)
        # -------------------------------
        all_items = []
        for memory in memories:
            all_items.append({"storage": "MEMORY", "id": memory.get("id", ""), "item": memory})
        for dmn_rule in dmn_rules:
            all_items.append({"storage": "DMN_RULE", "id": dmn_rule.get("id", ""), "item": dmn_rule})
        for skill in skills:
            skill_id = skill.get("id", skill.get("name", ""))
            all_items.append({"storage": "SKILL", "id": skill_id, "item": skill})
        
        for item_info in all_items:
            item_id = item_info.get("id")
            # 삭제 대상이 아니고 이동 대상도 아닌 경우에만 유지
            if item_id and item_id not in items_to_delete and item_id not in items_to_move:
                # 이미 actions에 추가되지 않은 경우만 추가
                existing = any(
                    action.get("id") == item_id and action.get("storage") == item_info.get("storage")
                    for action in actions
                )
                if not existing:
                    actions.append({
                        "operation": "KEEP",
                        "storage": item_info.get("storage"),
                        "id": item_id
                    })
        
        # 요약 생성
        to_delete_count = len([a for a in actions if a.get("operation") == "DELETE"])
        to_keep_count = len([a for a in actions if a.get("operation") == "KEEP"])
        
        plan = {
            "agent_id": agent_id,
            "total_knowledge_count": {
                "memory": len(memories),
                "dmn_rule": len(dmn_rules),
                "skill": len(skills)
            },
            "duplicate_groups": duplicate_groups,
            "actions": actions,
            "summary": {
                "to_delete": to_delete_count,
                "to_keep": to_keep_count,
                "total": len(actions)
            }
        }
        
        log(f"✅ 중복 제거 계획 생성 완료: agent_id={agent_id}, 삭제={to_delete_count}, 유지={to_keep_count}")
        
        return plan
        
    except Exception as e:
        handle_error("중복제거계획생성", e)
        raise

