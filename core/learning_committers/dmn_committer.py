"""
DMN Rule 커밋 모듈
proc_def 테이블에 DMN Rule을 저장하는 로직
"""

import os
import uuid
import json
import re
from typing import Dict
from llm_factory import create_llm
from utils.logger import log, handle_error
from dotenv import load_dotenv
from core.database import get_db_client, _get_agent_by_id, record_knowledge_history

load_dotenv()

# ============================================================================
# 유틸리티 함수
# ============================================================================

def _clean_json_response(content: str) -> str:
    """LLM 응답에서 백틱과 json 키워드 제거"""
    content = content.replace("```json", "").replace("```", "")
    return content.strip()


def _fix_dmn_xml_structure(dmn_xml: str) -> str:
    """
    생성된 DMN XML의 구조적 문제를 수정
    - <dmndi:DMNDiagram>에 id 속성 추가
    - <label> 위치 수정 (inputExpression 내부에서 input의 직접 자식으로 이동)
    
    Args:
        dmn_xml: 원본 DMN XML 문자열
    
    Returns:
        수정된 DMN XML 문자열
    """
    try:
        # 1. DMNDiagram에 id 추가 (없는 경우)
        diagram_match = re.search(r'<dmndi:DMNDiagram([^>]*)>', dmn_xml)
        if diagram_match and 'id=' not in diagram_match.group(0):
            dmn_xml = re.sub(
                r'<dmndi:DMNDiagram([^>]*)>',
                r'<dmndi:DMNDiagram id="DMNDiagram_1"\1>',
                dmn_xml
            )
            log("🔧 DMNDiagram에 id 속성 추가됨")
        
        # 2. <label>이 <inputExpression> 내부에 있는 경우 수정
        # 패턴: <inputExpression ...><text>...</text><label>...</label></inputExpression>
        # -> <inputExpression ...><text>...</text></inputExpression><label>...</label>
        pattern = r'(<inputExpression[^>]*>)(.*?<text>.*?</text>)(\s*<label>.*?</label>)(\s*</inputExpression>)'
        def fix_label_position(match):
            input_expr_start = match.group(1)
            text_content = match.group(2)
            label_content = match.group(3)
            input_expr_end = match.group(4)
            # label을 inputExpression 밖으로 이동
            return f'{input_expr_start}{text_content}{input_expr_end}{label_content}'
        
        if re.search(pattern, dmn_xml, re.DOTALL):
            dmn_xml = re.sub(pattern, fix_label_position, dmn_xml, flags=re.DOTALL)
            log("🔧 <label> 위치 수정됨 (inputExpression 밖으로 이동)")
        
        # 3. DMNShape의 dmnElementRef가 없는 경우 decision id와 매칭
        decision_id_match = re.search(r'<decision\s+id="([^"]+)"', dmn_xml)
        if decision_id_match:
            decision_id = decision_id_match.group(1)
            # DMNShape에서 dmnElementRef가 없는 경우 추가
            dmn_shape_pattern = r'<dmndi:DMNShape[^>]*dmnElementRef="[^"]*"'
            if not re.search(dmn_shape_pattern, dmn_xml):
                # dmnElementRef가 없는 경우 추가
                dmn_xml = re.sub(
                    r'(<dmndi:DMNShape[^>]*)(>)',
                    rf'\1 dmnElementRef="{decision_id}"\2',
                    dmn_xml
                )
                log(f'🔧 DMNShape에 dmnElementRef="{decision_id}" 추가됨')
        
        return dmn_xml
        
    except Exception as e:
        log(f"⚠️ DMN XML 구조 수정 중 오류 발생: {e}, 원본 XML 사용")
        return dmn_xml


# ============================================================================
# DMN XML 생성
# ============================================================================

async def _generate_dmn_xml_llm(rule_name: str, condition: str, action: str, feedback_content: str = "") -> str:
    """
    LLM을 사용하여 DMN 1.3 XML 생성 (JavaScript 프롬프트 기반)
    
    Args:
        rule_name: 규칙 이름
        condition: 조건 (예: "age < 18")
        action: 결과 (예: "20% 할인")
        feedback_content: 원본 피드백 내용 (선택적, 더 정확한 XML 생성을 위해)
    
    Returns:
        DMN XML 문자열
    """
    llm = create_llm(model="gpt-4o", streaming=False, temperature=0)
    
    prompt = f"""You are a **DMN (Decision Model and Notation) 1.3 expert**. 
Generate a valid DMN 1.3 XML decision table from the business rule provided.

**Rule Name:** {rule_name}
**Condition:** {condition}
**Action/Result:** {action}
{f"**Original Feedback:** {feedback_content}" if feedback_content else ""}

### 🎯 Output format (STRICT)
Return **ONLY valid JSON** — no markdown fences, no comments, no extra text.
The JSON must exactly follow this schema:

{{
    "dmnXml": "<complete DMN XML as a single-line escaped string (escape all double quotes and line breaks)>",
    "description": "<brief explanation in Korean>"
}}

Rules:
- The top-level value MUST be a valid JSON object.
- Do not wrap the JSON in ```.
- All double quotes inside dmnXml MUST be escaped as \\".
- All line breaks inside dmnXml MUST be escaped as \\n.
- No trailing commas.

### 🧩 XML Schema Constraints
You MUST return a complete, importable DMN 1.3 XML model that displays correctly in DMN modelers.

Required:
- Root element: `<definitions>` with proper DMN 1.3 namespace declarations and a unique `id`.
- Required namespaces:
  - `xmlns="https://www.omg.org/spec/DMN/20191111/MODEL/"` (default namespace)
  - `xmlns:dmndi="https://www.omg.org/spec/DMN/20191111/DMNDI/"`
  - `xmlns:dc="http://www.omg.org/spec/DMN/20180521/DC/"`
- Must include: `definitions`, `decision`, `decisionTable`, `rule`, `input`, `output`, `dmndi:DMNDI`.

**CRITICAL: Input Element Structure**
The `<input>` element MUST follow this exact structure:
```xml
<input id="input_1">
  <inputExpression id="input_expr_1" typeRef="number">
    <text>variableName</text>
  </inputExpression>
  <label>Display Label</label>
</input>
```
- `<label>` MUST be a direct child of `<input>`, NOT inside `<inputExpression>`.
- `<label>` and `<inputExpression>` are siblings at the same level.
- The `<text>` inside `<inputExpression>` should contain the variable name or expression (e.g., "orderAmount", not a full condition).

**CRITICAL: DMNDI Diagram Structure**
The `<dmndi:DMNDI>` section MUST include:
```xml
<dmndi:DMNDI>
  <dmndi:DMNDiagram id="DMNDiagram_1">
    <dmndi:DMNShape id="DMNShape_decision_id" dmnElementRef="decision_id">
      <dc:Bounds x="100" y="100" width="180" height="80"/>
    </dmndi:DMNShape>
  </dmndi:DMNDiagram>
</dmndi:DMNDI>
```
- `<dmndi:DMNDiagram>` MUST have an `id` attribute (e.g., "DMNDiagram_1").
- `<dmndi:DMNShape>` MUST have both `id` and `dmnElementRef` attributes.
- `dmnElementRef` MUST match the `<decision>` element's `id` exactly.

Hit Policy:
- Use full names only: UNIQUE, ANY, FIRST, PRIORITY, OUTPUT ORDER, RULE ORDER, COLLECT.
- For single condition-action rules, FIRST is typically appropriate.

IDs / Naming:
- All element IDs use lowercase_snake_case (e.g. `customer_risk_assessment`, `input_1`, `rule_1`).
- IDs should be meaningful to the business domain, not random UUIDs.
- Display names (`name` attributes) should be short, human-readable Korean.

Inputs / Outputs:
- Declare each input with a clear variable name in `<inputExpression><text>` and typeRef (string, number, boolean, etc.).
- Use `<label>` for human-readable display names.
- Rules must map input conditions → output values explicitly.
- In `<inputEntry>`, use comparison expressions like ">= 700000", "< 18", "== \"active\"", etc.
- Based on the condition provided, infer appropriate input variable names and types.
- Based on the action provided, infer appropriate output variable names and types.

### 🎨 Rule Generation Guidelines
1. Analyze the condition to determine:
   - What input variables are needed (e.g., "orderAmount", "age", "status")
   - What data types they should be (boolean, number, string, etc.)
   - Extract the variable name and the comparison operator separately
   - Example: "orderAmount >= 700000" → variable: "orderAmount", typeRef: "number", condition in rule: ">= 700000"

2. Analyze the action to determine:
   - What output variables are needed
   - What data types they should be
   - What the output value should be

3. Generate a proper decision table with:
   - **Input structure**: `<input>` with `<inputExpression>` containing just the variable name (e.g., "orderAmount"), and `<label>` for display
   - **Rule structure**: `<inputEntry>` contains the comparison expression (e.g., ">= 700000", not the full condition)
   - Appropriate input columns based on the condition
   - Appropriate output columns based on the action
   - At least one rule that represents the condition-action mapping
   - Consider adding a default/fallback rule if appropriate (with "-" or empty inputEntry)

4. Ensure XML is well-formed:
   - All tags properly closed
   - All attribute values properly quoted
   - All element `id` values unique across the document
   - Proper XML escaping for special characters (< → &lt;, > → &gt;, & → &amp;)
   - `<dmndi:DMNDiagram>` MUST have an `id` attribute
   - `<label>` MUST be outside `<inputExpression>`, as a sibling element

Generate the DMN XML now and return ONLY the JSON object with dmnXml and description fields.
"""
    
    try:
        response = await llm.ainvoke(prompt)
        cleaned_content = _clean_json_response(response.content)
        
        log(f"🤖 DMN 생성 LLM 응답 (일부): {cleaned_content[:500]}...")
        
        parsed_result = json.loads(cleaned_content)
        dmn_xml_escaped = parsed_result.get("dmnXml", "")
        description = parsed_result.get("description", "")
        
        # 이스케이프된 문자열을 원래 XML로 변환
        dmn_xml = dmn_xml_escaped.replace('\\n', '\n').replace('\\"', '"')
        
        # XML 구조 문제 수정
        dmn_xml = _fix_dmn_xml_structure(dmn_xml)
        
        log(f"📄 DMN XML 생성 완료: {description}")
        
        return dmn_xml
        
    except json.JSONDecodeError as e:
        log(f"❌ DMN 생성 JSON 파싱 실패 - 응답: {response.content if 'response' in locals() else 'None'}")
        handle_error("DMN생성 JSON 파싱", f"응답 파싱 실패: {e}")
        # Fallback: 간단한 XML 생성
        return _generate_dmn_xml_fallback(rule_name, condition, action)
    except Exception as e:
        handle_error("DMN생성", e)
        # Fallback: 간단한 XML 생성
        return _generate_dmn_xml_fallback(rule_name, condition, action)


async def _extend_dmn_xml_llm(existing_xml: str, rule_name: str, new_condition: str, new_action: str, feedback_content: str = "") -> str:
    """
    LLM을 사용하여 기존 DMN XML에 새 규칙을 추가/확장 (병합)
    
    기존 규칙을 보존하면서 새로운 조건-결과 규칙을 추가합니다.
    
    Args:
        existing_xml: 기존 DMN XML
        rule_name: 규칙 이름
        new_condition: 새로 추가할 조건
        new_action: 새로 추가할 결과
        feedback_content: 원본 피드백 내용 (선택적)
    
    Returns:
        확장된 DMN XML 문자열
    """
    llm = create_llm(model="gpt-4o", streaming=False, temperature=0)
    
    prompt = f"""You are a **DMN (Decision Model and Notation) 1.3 expert**. 
Your task is to **EXTEND** an existing DMN decision table by adding new rules, while **PRESERVING all existing rules**.

**CRITICAL: DO NOT REPLACE OR REMOVE EXISTING RULES. ADD NEW RULES TO THE EXISTING TABLE.**

### Existing DMN XML:
```xml
{existing_xml}
```

### New Rule to Add:
- **Condition:** {new_condition}
- **Action/Result:** {new_action}
{f"- **Context from Feedback:** {feedback_content}" if feedback_content else ""}

### 🎯 Your Task:
1. **Analyze** the existing decision table structure (inputs, outputs, existing rules)
2. **PRESERVE** all existing `<rule>` elements exactly as they are
3. **ADD** new `<rule>` element(s) that represent the new condition-action mapping
4. If the new condition adds specificity to existing rules (e.g., "for amounts under 500K"), add it as additional rules, not replacement
5. Ensure all rule IDs are unique (append new unique IDs like rule_N+1, rule_N+2, etc.)
6. Keep the hitPolicy as is (usually FIRST or UNIQUE)

### 🎯 Output format (STRICT)
Return **ONLY valid JSON** — no markdown fences, no comments, no extra text.
The JSON must exactly follow this schema:

{{
    "dmnXml": "<complete EXTENDED DMN XML as a single-line escaped string>",
    "description": "<brief explanation in Korean of what was added>",
    "changes": "<summary of rules added vs. preserved>"
}}

Rules:
- The top-level value MUST be a valid JSON object.
- Do not wrap the JSON in ```.
- All double quotes inside dmnXml MUST be escaped as \\".
- All line breaks inside dmnXml MUST be escaped as \\n.
- **EXISTING RULES MUST BE PRESERVED IN THE OUTPUT**

Generate the extended DMN XML now, preserving all existing rules and adding the new one(s).
"""
    
    try:
        response = await llm.ainvoke(prompt)
        cleaned_content = _clean_json_response(response.content)
        
        log(f"🤖 DMN 확장 LLM 응답 (일부): {cleaned_content[:500]}...")
        
        parsed_result = json.loads(cleaned_content)
        dmn_xml_escaped = parsed_result.get("dmnXml", "")
        description = parsed_result.get("description", "")
        changes = parsed_result.get("changes", "")
        
        # 이스케이프된 문자열을 원래 XML로 변환
        dmn_xml = dmn_xml_escaped.replace('\\n', '\n').replace('\\"', '"')
        
        # XML 구조 문제 수정
        dmn_xml = _fix_dmn_xml_structure(dmn_xml)
        
        log(f"📄 DMN XML 확장 완료: {description}")
        log(f"   변경 사항: {changes}")
        
        return dmn_xml
        
    except json.JSONDecodeError as e:
        log(f"❌ DMN 확장 JSON 파싱 실패 - 응답: {response.content if 'response' in locals() else 'None'}")
        handle_error("DMN확장 JSON 파싱", f"응답 파싱 실패: {e}")
        # Fallback: 기존 XML 그대로 반환 (손상 방지)
        log(f"⚠️ Fallback: 기존 DMN XML 유지 (손상 방지)")
        return existing_xml
    except Exception as e:
        handle_error("DMN확장", e)
        # Fallback: 기존 XML 그대로 반환 (손상 방지)
        log(f"⚠️ Fallback: 기존 DMN XML 유지 (손상 방지)")
        return existing_xml


def _generate_dmn_xml_fallback(rule_name: str, condition: str, action: str) -> str:
    """
    Fallback: 간단한 DMN XML 생성 (LLM 실패 시 사용)
    
    Args:
        rule_name: 규칙 이름
        condition: 조건
        action: 결과
    
    Returns:
        DMN XML 문자열
    """
    # snake_case로 변환
    decision_id = rule_name.lower().replace(" ", "_").replace("-", "_")
    rule_id = f"rule_1"
    table_id = f"decision_table_{uuid.uuid4().hex[:8]}"
    
    # XML 이스케이프
    condition_escaped = condition.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    action_escaped = action.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    
    dmn_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="https://www.omg.org/spec/DMN/20191111/MODEL/" xmlns:dmndi="https://www.omg.org/spec/DMN/20191111/DMNDI/" xmlns:dc="http://www.omg.org/spec/DMN/20180521/DC/" id="Definitions_1" name="DRD" namespace="http://camunda.org/schema/1.0/dmn" exporter="process-gpt-agent-feedback" exporterVersion="1.0.0">
  <decision id="{decision_id}" name="{rule_name}">
    <decisionTable id="{table_id}" hitPolicy="FIRST">
      <input id="input_1">
        <inputExpression id="input_expr_1" typeRef="boolean">
          <text>{condition_escaped}</text>
        </inputExpression>
      </input>
      <output id="output_1" name="결과" typeRef="string" />
      <rule id="{rule_id}">
        <inputEntry id="input_entry_1">
          <text>true</text>
        </inputEntry>
        <outputEntry id="output_entry_1">
          <text>{action_escaped}</text>
        </outputEntry>
      </rule>
    </decisionTable>
  </decision>
  <dmndi:DMNDI>
    <dmndi:DMNDiagram id="DMNDiagram_1">
      <dmndi:DMNShape id="DMNShape_{decision_id}" dmnElementRef="{decision_id}">
        <dc:Bounds height="80" width="180" x="200" y="64" />
      </dmndi:DMNShape>
    </dmndi:DMNDiagram>
  </dmndi:DMNDI>
</definitions>'''
    
    return dmn_xml


# ============================================================================
# DMN Rule 커밋
# ============================================================================

async def commit_to_dmn_rule(agent_id: str, dmn_artifact: Dict, feedback_content: str = "", operation: str = "CREATE", rule_id: str = None):
    """
    DMN Rule을 proc_def 테이블에 CRUD 작업 수행
    
    Args:
        agent_id: 에이전트 ID (owner 필드에 저장)
        dmn_artifact: DMN 규칙 정보 {"condition": "...", "action": "...", "name": "..." (optional)}
        feedback_content: 원본 피드백 내용 (선택적, 더 정확한 XML 생성을 위해)
        operation: "CREATE" | "UPDATE" | "DELETE"
        rule_id: UPDATE/DELETE 시 기존 규칙 ID (필수)
    
    Raises:
        ValueError: 필수 파라미터가 없거나 에이전트를 찾을 수 없을 때
        Exception: 작업 실패 시
    """
    try:
        supabase = get_db_client()
        
        if operation == "DELETE":
            if not rule_id:
                log(f"⚠️ DELETE 작업인데 rule_id가 없음")
                raise ValueError("DELETE 작업에는 rule_id가 필요합니다")
            
            # 삭제 전 이전 내용 조회 (변경 이력용)
            previous_content = None
            try:
                rule_data = (
                    supabase.table('proc_def')
                    .select('*')
                    .eq('id', rule_id)
                    .eq('owner', agent_id)
                    .single()
                    .execute()
                )
                if rule_data.data:
                    previous_content = {
                        "name": rule_data.data.get("name", ""),
                        "bpmn": rule_data.data.get("bpmn", ""),
                        "condition": "",  # XML에서 추출 가능하지만 여기서는 생략
                        "action": ""
                    }
            except Exception:
                pass
            
            # 하드 삭제: 행 자체를 제거
            supabase.table('proc_def').delete().eq('id', rule_id).eq('owner', agent_id).execute()
            
            log(f"🗑️ DMN_RULE 하드 삭제 완료: 에이전트 {agent_id}, rule_id={rule_id}")
            
            # 변경 이력 기록
            try:
                agent_info = _get_agent_by_id(agent_id)
                tenant_id = agent_info.get("tenant_id") if agent_info else None
                
                rule_name = previous_content.get("name", "") if previous_content else ""
                
                # feedback_content에서 batch_job_id 추출 시도 (필요 시 확장)
                batch_job_id = None
                if feedback_content and ("배치" in feedback_content or "batch" in feedback_content.lower()):
                    # 배치 작업으로 삭제된 경우 (개선 여지)
                    pass
                
                record_knowledge_history(
                    knowledge_type="DMN_RULE",
                    knowledge_id=rule_id,
                    agent_id=agent_id,
                    tenant_id=tenant_id,
                    operation="DELETE",
                    previous_content=previous_content,
                    feedback_content=feedback_content,
                    knowledge_name=rule_name,
                    batch_job_id=batch_job_id
                )
            except Exception as e:
                log(f"   ⚠️ DMN_RULE 변경 이력 기록 실패 (무시하고 계속 진행): {e}")
            
            return
        
        # CREATE 또는 UPDATE인 경우
        condition = dmn_artifact.get("condition", "")
        action = dmn_artifact.get("action", "")
        rule_name = dmn_artifact.get("name", "피드백 기반 규칙")
        
        if not condition or not action:
            log(f"⚠️ DMN_RULE 저장/수정 실패: condition이나 action이 비어있음")
            raise ValueError("DMN Rule의 condition과 action은 필수입니다")
        
        # 에이전트 정보 조회 (tenant_id 확인)
        agent_info = _get_agent_by_id(agent_id)
        if not agent_info:
            log(f"⚠️ 에이전트 정보를 찾을 수 없음: {agent_id}")
            raise ValueError(f"에이전트를 찾을 수 없습니다: {agent_id}")
        
        tenant_id = agent_info.get("tenant_id")
        if not tenant_id:
            log(f"⚠️ 에이전트에 tenant_id가 없음: {agent_id}")
            tenant_id = "default"  # 기본값 사용
        
        if operation == "UPDATE":
            if not rule_id:
                log(f"⚠️ UPDATE 작업인데 rule_id가 없음")
                raise ValueError("UPDATE 작업에는 rule_id가 필요합니다")
            
            # 업데이트 전 기존 규칙 조회 (변경 이력용)
            previous_content = None
            try:
                rule_data = (
                    supabase.table('proc_def')
                    .select('name, bpmn')
                    .eq('id', rule_id)
                    .eq('owner', agent_id)
                    .single()
                    .execute()
                )
                if rule_data.data:
                    previous_content = {
                        "name": rule_data.data.get("name", ""),
                        "bpmn": rule_data.data.get("bpmn", ""),
                        "condition": "",
                        "action": ""
                    }
            except Exception:
                pass
            
            # ⚠️ 자동 확장 로직 제거: 에이전트가 완성된 내용을 전달하면 저장만 함
            # 에이전트가 이미 완성된 XML을 전달한 경우 (bpmn 또는 full_xml 필드)
            if dmn_artifact.get("bpmn") or dmn_artifact.get("full_xml"):
                dmn_xml = dmn_artifact.get("bpmn") or dmn_artifact.get("full_xml")
                log(f"✅ 에이전트가 전달한 XML 사용 (길이: {len(dmn_xml)}자)")
            else:
                # 에이전트가 condition/action만 전달한 경우 새 XML 생성
                # ⚠️ 주의: 기존 XML과 자동 병합하지 않음. 에이전트가 병합을 원하면 직접 수행해야 함
                log(f"🤖 LLM을 사용하여 DMN XML 생성 시작: {rule_name}")
                log(f"   ⚠️ 주의: 기존 XML과 자동 병합하지 않습니다. 병합이 필요하면 get_knowledge_detail로 기존 내용을 조회하여 직접 구성하세요.")
                dmn_xml = await _generate_dmn_xml_llm(rule_name, condition, action, feedback_content)
            
            # 기존 규칙 업데이트
            resp = supabase.table('proc_def').update({
                'name': rule_name,
                'bpmn': dmn_xml,
            }).eq('id', rule_id).eq('owner', agent_id).execute()
            
            log(f"✏️ DMN_RULE 수정 완료: 에이전트 {agent_id}, rule_id={rule_id}")
            log(f"   규칙 이름: {rule_name}")
            log(f"   조건: {condition}")
            log(f"   결과: {action}")
            
            # 변경 이력 기록
            try:
                agent_info = _get_agent_by_id(agent_id)
                tenant_id = agent_info.get("tenant_id") if agent_info else None
                
                new_content = {
                    "name": rule_name,
                    "bpmn": dmn_xml,
                    "condition": condition,
                    "action": action
                }
                
                record_knowledge_history(
                    knowledge_type="DMN_RULE",
                    knowledge_id=rule_id,
                    agent_id=agent_id,
                    tenant_id=tenant_id,
                    operation="UPDATE",
                    previous_content=previous_content,
                    new_content=new_content,
                    feedback_content=feedback_content,
                    knowledge_name=rule_name
                )
            except Exception as e:
                log(f"   ⚠️ DMN_RULE 변경 이력 기록 실패 (무시하고 계속 진행): {e}")
            
        else:  # CREATE
            # LLM을 사용하여 새 DMN XML 생성
            log(f"🤖 LLM을 사용하여 DMN XML 생성 시작: {rule_name}")
            dmn_xml = await _generate_dmn_xml_llm(rule_name, condition, action, feedback_content)
            
            # UUID 생성
            rule_uuid = str(uuid.uuid4())
            new_rule_id = str(uuid.uuid4())
            
            # proc_def 테이블에 저장
            resp = supabase.table('proc_def').insert({
                'id': new_rule_id,
                'name': rule_name,
                'definition': None,
                'bpmn': dmn_xml,
                'uuid': rule_uuid,
                'tenant_id': tenant_id,
                'isdeleted': False,
                'owner': agent_id,
                'type': 'dmn'
            }).execute()
            
            log(f"✅ DMN_RULE 저장 완료: 에이전트 {agent_id}")
            log(f"   규칙 ID: {new_rule_id}")
            log(f"   규칙 이름: {rule_name}")
            log(f"   조건: {condition}")
            log(f"   결과: {action}")
            
            # 변경 이력 기록
            try:
                new_content = {
                    "name": rule_name,
                    "bpmn": dmn_xml,
                    "condition": condition,
                    "action": action
                }
                
                # feedback_content에서 batch_job_id 추출 시도
                batch_job_id = None
                if "배치" in feedback_content or "batch" in feedback_content.lower():
                    # 배치 작업으로 생성된 경우 (개선 가능)
                    pass
                
                record_knowledge_history(
                    knowledge_type="DMN_RULE",
                    knowledge_id=new_rule_id,
                    agent_id=agent_id,
                    tenant_id=tenant_id,
                    operation="CREATE",
                    new_content=new_content,
                    feedback_content=feedback_content,
                    knowledge_name=rule_name,
                    batch_job_id=batch_job_id
                )
            except Exception as e:
                log(f"   ⚠️ DMN_RULE 변경 이력 기록 실패 (무시하고 계속 진행): {e}")
        
    except Exception as e:
        handle_error(f"DMN_RULE{operation}", e)
        raise
