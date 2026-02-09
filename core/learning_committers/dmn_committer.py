"""
DMN Rule 커밋 모듈
proc_def 테이블에 DMN Rule을 저장하는 로직
"""

import os
import uuid
import json
import re
from typing import Dict, Optional, Tuple
from datetime import datetime
from core.llm import create_llm
from utils.logger import log, handle_error
from dotenv import load_dotenv
from core.database import get_db_client, _get_agent_by_id, record_knowledge_history

load_dotenv()

# ============================================================================
# 유틸리티 함수
# ============================================================================

def _get_next_version(current_version: Optional[str], merge_mode: str) -> str:
    """
    현재 버전에서 다음 버전 번호 생성 (semantic versioning)
    
    Args:
        current_version: 현재 버전 (예: "1.0.0") 또는 None
        merge_mode: "REPLACE" | "EXTEND" | "REFINE"
    
    Returns:
        다음 버전 번호 (예: "1.0.1")
    """
    if not current_version:
        return "1.0.0"
    
    try:
        parts = current_version.split(".")
        major = int(parts[0]) if len(parts) > 0 and parts[0].isdigit() else 1
        minor = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        patch = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        
        if merge_mode == "REPLACE":
            # 대체: major 버전 증가
            return f"{major + 1}.0.0"
        elif merge_mode == "EXTEND":
            # 확장: minor 버전 증가
            return f"{major}.{minor + 1}.0"
        elif merge_mode == "REFINE":
            # 세밀한 수정: patch 버전 증가
            return f"{major}.{minor}.{patch + 1}"
        else:
            # 기본: patch 버전 증가
            return f"{major}.{minor}.{patch + 1}"
    except Exception as e:
        log(f"⚠️ 버전 파싱 실패: {current_version}, 기본값 1.0.0 사용. 오류: {e}")
        return "1.0.0"


def _generate_xml_diff(old_xml: str, new_xml: str) -> str:
    """
    두 XML 간의 차이점을 텍스트로 생성
    
    Args:
        old_xml: 이전 XML
        new_xml: 새로운 XML
    
    Returns:
        차이점 설명 텍스트
    """
    try:
        # 간단한 diff 생성: 길이, 규칙 수 등 비교
        old_len = len(old_xml)
        new_len = len(new_xml)
        
        # 규칙 수 추출
        old_rule_count = len(re.findall(r'<rule\s+id=', old_xml))
        new_rule_count = len(re.findall(r'<rule\s+id=', new_xml))
        
        # Hit policy 추출
        old_hit_policy_match = re.search(r'hitPolicy="([^"]+)"', old_xml)
        new_hit_policy_match = re.search(r'hitPolicy="([^"]+)"', new_xml)
        old_hit_policy = old_hit_policy_match.group(1) if old_hit_policy_match else "N/A"
        new_hit_policy = new_hit_policy_match.group(1) if new_hit_policy_match else "N/A"
        
        diff_parts = []
        
        if old_rule_count != new_rule_count:
            diff_parts.append(f"규칙 수 변경: {old_rule_count}개 → {new_rule_count}개")
        
        if old_hit_policy != new_hit_policy:
            diff_parts.append(f"Hit Policy 변경: {old_hit_policy} → {new_hit_policy}")
        
        if abs(new_len - old_len) > 100:
            diff_parts.append(f"XML 크기 변경: {old_len}자 → {new_len}자")
        
        if not diff_parts:
            diff_parts.append("규칙 내용 수정됨")
        
        return "; ".join(diff_parts)
    except Exception as e:
        log(f"⚠️ XML diff 생성 실패: {e}")
        return f"XML 변경됨 (이전: {len(old_xml)}자, 새: {len(new_xml)}자)"


async def _save_dmn_version(
    proc_def_id: str,
    version: str,
    dmn_xml: str,
    tenant_id: Optional[str],
    previous_xml: Optional[str] = None,
    merge_mode: str = "REPLACE",
    feedback_content: Optional[str] = None,
    source_todolist_id: Optional[str] = None
) -> str:
    """
    proc_def_version 테이블에 DMN 버전 정보 저장
    
    Args:
        proc_def_id: proc_def 테이블의 ID
        version: 버전 번호 (예: "1.0.0")
        dmn_xml: DMN XML 내용
        tenant_id: 테넌트 ID
        previous_xml: 이전 XML (diff 생성용)
        merge_mode: 병합 모드
        feedback_content: 피드백 내용 (message 필드용)
        source_todolist_id: 소스 todolist ID
    
    Returns:
        생성된 버전의 UUID
    """
    try:
        supabase = get_db_client()
        
        # 이전 버전 조회 (parent_version 찾기)
        parent_version = None
        try:
            latest_version = (
                supabase.table('proc_def_version')
                .select('version, uuid')
                .eq('proc_def_id', proc_def_id)
                .order('timeStamp', desc=True)
                .limit(1)
                .execute()
            )
            if latest_version.data:
                parent_version = latest_version.data[0].get('version')
        except Exception:
            pass
        
        # Diff 생성
        diff_text = None
        if previous_xml:
            diff_text = _generate_xml_diff(previous_xml, dmn_xml)
        
        # Message 생성
        if merge_mode == "INITIAL":
            message = "기존 규칙 초기 버전 생성"
        else:
            message = f"{merge_mode} 모드로 업데이트"
        if feedback_content:
            # 피드백 내용을 간단히 요약 (너무 길면 자름)
            feedback_summary = feedback_content[:200] + "..." if len(feedback_content) > 200 else feedback_content
            message = f"{message}: {feedback_summary}"
        
        # arcv_id 생성 (proc_def_id와 version 조합)
        arcv_id = f"{proc_def_id}_{version}"
        
        # 버전 정보 저장
        version_data = {
            'arcv_id': arcv_id,
            'proc_def_id': proc_def_id,
            'version': version,
            'version_tag': None,  # 필요시 나중에 추가
            'snapshot': dmn_xml,  # 전체 XML 스냅샷
            'definition': None,  # JSONB 필드 (필요시 사용)
            'timeStamp': datetime.now().isoformat(),
            'diff': diff_text,
            'message': message,
            'tenant_id': tenant_id,
            'parent_version': parent_version,
            'source_todolist_id': source_todolist_id
        }
        
        resp = supabase.table('proc_def_version').insert(version_data).execute()
        
        if resp.data and len(resp.data) > 0:
            version_uuid = resp.data[0].get('uuid')
            log(f"📦 DMN 버전 저장 완료: proc_def_id={proc_def_id}, version={version}, uuid={version_uuid}")
            return version_uuid
        else:
            log(f"⚠️ DMN 버전 저장 응답이 비어있음")
            return str(uuid.uuid4())
            
    except Exception as e:
        log(f"⚠️ DMN 버전 저장 실패: {e}")
        handle_error("DMN버전저장", e)
        # 버전 저장 실패해도 계속 진행
        return str(uuid.uuid4())


def _clean_json_response(content: str) -> str:
    """LLM 응답에서 백틱과 json 키워드 제거"""
    content = content.replace("```json", "").replace("```", "")
    return content.strip()


def _fix_dmn_xml_structure(dmn_xml: str) -> str:
    """
    생성된 DMN XML의 구조적 문제를 수정
    - <dmndi:DMNDiagram>에 id 속성 추가
    - <label> 위치 수정 (inputExpression 내부에서 input의 직접 자식으로 이동)
    - inputData, knowledgeSource, businessKnowledgeModel 요소의 dmnElementRef 매칭
    - DMNDI 섹션에 누락된 요소의 shape 추가
    
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
        
        # 3. 모든 요소의 DMNShape dmnElementRef 매칭
        # decision 요소
        decision_id_match = re.search(r'<decision\s+id="([^"]+)"', dmn_xml)
        if decision_id_match:
            decision_id = decision_id_match.group(1)
            # decision에 대한 DMNShape에서 dmnElementRef가 없는 경우 추가
            decision_shape_pattern = r'<dmndi:DMNShape[^>]*dmnElementRef="[^"]*"[^>]*>'
            decision_shapes = re.findall(r'<dmndi:DMNShape[^>]*>', dmn_xml)
            for shape in decision_shapes:
                if f'dmnElementRef="{decision_id}"' not in shape and 'dmnElementRef=' not in shape:
                    # decision에 대한 shape 찾기 (가장 가까운 shape 또는 첫 번째 shape)
                    dmn_xml = re.sub(
                        r'(<dmndi:DMNShape[^>]*)(>)',
                        rf'\1 dmnElementRef="{decision_id}"\2',
                        dmn_xml,
                        count=1  # 첫 번째만 수정
                    )
                    log(f'🔧 DMNShape에 dmnElementRef="{decision_id}" 추가됨 (decision)')
                    break
        
        # inputData 요소들
        input_data_matches = re.findall(r'<inputData\s+id="([^"]+)"', dmn_xml)
        for input_data_id in input_data_matches:
            # 해당 inputData에 대한 DMNShape가 있는지 확인
            shape_pattern = rf'<dmndi:DMNShape[^>]*dmnElementRef="{re.escape(input_data_id)}"'
            if not re.search(shape_pattern, dmn_xml):
                # inputData에 대한 shape가 없으면 추가 (간단한 방법: 마지막 shape 뒤에 추가)
                # 더 정교한 방법은 DMNDiagram 내부 구조를 파싱하는 것이지만, 여기서는 기본 수정만 수행
                log(f'   ℹ️ inputData "{input_data_id}"에 대한 DMNShape 확인 필요 (수동 검토 권장)')
        
        # knowledgeSource 요소들
        knowledge_source_matches = re.findall(r'<knowledgeSource\s+id="([^"]+)"', dmn_xml)
        for ks_id in knowledge_source_matches:
            shape_pattern = rf'<dmndi:DMNShape[^>]*dmnElementRef="{re.escape(ks_id)}"'
            if not re.search(shape_pattern, dmn_xml):
                log(f'   ℹ️ knowledgeSource "{ks_id}"에 대한 DMNShape 확인 필요 (수동 검토 권장)')
        
        # businessKnowledgeModel 요소들
        bkm_matches = re.findall(r'<businessKnowledgeModel\s+id="([^"]+)"', dmn_xml)
        for bkm_id in bkm_matches:
            shape_pattern = rf'<dmndi:DMNShape[^>]*dmnElementRef="{re.escape(bkm_id)}"'
            if not re.search(shape_pattern, dmn_xml):
                log(f'   ℹ️ businessKnowledgeModel "{bkm_id}"에 대한 DMNShape 확인 필요 (수동 검토 권장)')
        
        # 4. namespace 선언 확인 (di namespace가 필요한 경우)
        if '<dmndi:DMNEdge' in dmn_xml and 'xmlns:di=' not in dmn_xml:
            # di namespace 추가 (DMNEdge에 필요)
            definitions_match = re.search(r'(<definitions[^>]*)(>)', dmn_xml)
            if definitions_match:
                definitions_attrs = definitions_match.group(1)
                if 'xmlns:di=' not in definitions_attrs:
                    dmn_xml = re.sub(
                        r'(<definitions[^>]*)(>)',
                        r'\1 xmlns:di="http://www.omg.org/spec/DMN/20180521/DI/"\2',
                        dmn_xml,
                        count=1
                    )
                    log("🔧 definitions에 xmlns:di namespace 추가됨")
        
        return dmn_xml
        
    except Exception as e:
        log(f"⚠️ DMN XML 구조 수정 중 오류 발생: {e}, 원본 XML 사용")
        return dmn_xml


# ============================================================================
# DMN XML 생성
# ============================================================================

async def _generate_dmn_xml_llm(rule_name: str, condition: str, action: str, feedback_content: str = "") -> str:
    """
    LLM을 사용하여 DMN 1.3 XML 생성 (완전한 모델 구조 포함)
    
    조건과 규칙 간의 관계를 분석하여 inputData, knowledgeSource, businessKnowledgeModel을
    필요에 따라 생성하는 완전한 DMN 모델을 생성합니다.
    
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
Generate a **complete, well-structured DMN 1.3 XML model** from the business rule provided.

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

### 🧩 Complete DMN Model Structure

You MUST create a **complete DMN model** that includes:

1. **`<inputData>` elements** (REQUIRED):
   - Define ALL input data variables that are referenced in the decision table
   - Each `<inputData>` represents a data input to the decision model
   - Structure:
     ```xml
     <inputData id="input_data_order_amount" name="주문 금액">
       <variable id="var_order_amount" name="orderAmount" typeRef="number"/>
     </inputData>
     ```
   - **CRITICAL**: Analyze the condition to identify ALL input variables needed
   - Example: If condition is "orderAmount >= 700000 AND customerType == 'VIP'", create TWO inputData elements:
     - One for "orderAmount" (typeRef="number")
     - One for "customerType" (typeRef="string")

2. **`<decision>` element** (REQUIRED):
   - Contains the decision table with rules
   - MUST reference inputData elements using `<informationRequirement>` or direct variable references
   - Structure:
     ```xml
     <decision id="decision_id" name="Decision Name">
       <informationRequirement>
         <requiredInput href="#input_data_order_amount"/>
       </informationRequirement>
       <decisionTable id="table_id" hitPolicy="FIRST">
         <input id="input_1">
           <inputExpression id="input_expr_1" typeRef="number">
             <text>orderAmount</text>
           </inputExpression>
           <label>주문 금액</label>
         </input>
         <output id="output_1" name="결과" typeRef="string"/>
         <rule id="rule_1">
           <inputEntry id="input_entry_1">
             <text>&gt;= 700000</text>
           </inputEntry>
           <outputEntry id="output_entry_1">
             <text>승인 필요</text>
           </outputEntry>
         </rule>
       </decisionTable>
     </decision>
     ```

3. **`<knowledgeSource>` elements** (OPTIONAL, but include if relevant):
   - Define external knowledge sources that inform the decision
   - Use when the rule references policies, regulations, guidelines, or external data
   - Structure:
     ```xml
     <knowledgeSource id="ks_policy_1" name="정책 문서">
       <authorityRequirement>
         <requiredAuthority href="#decision_id"/>
       </authorityRequirement>
     </knowledgeSource>
     ```
   - Include if the feedback mentions policies, regulations, or external guidelines

4. **`<businessKnowledgeModel>` elements** (OPTIONAL, but include if reusable logic exists):
   - Define reusable business logic that can be invoked by decisions
   - Use when the rule contains complex calculations or reusable sub-decisions
   - Structure:
     ```xml
     <businessKnowledgeModel id="bkm_calculation_1" name="계산 로직">
       <functionKind>FEEL</functionKind>
       <encapsulatedLogic>
         <literalExpression id="expr_1" typeRef="number">
           <text>input * 0.1</text>
         </literalExpression>
       </encapsulatedLogic>
     </businessKnowledgeModel>
     ```
   - Include if the action involves calculations or complex transformations

5. **`<dmndi:DMNDI>` section** (REQUIRED):
   - Visual representation of all elements
   - MUST include shapes for ALL elements: inputData, decision, knowledgeSource, businessKnowledgeModel
   - Structure:
     ```xml
     <dmndi:DMNDI>
       <dmndi:DMNDiagram id="DMNDiagram_1">
         <dmndi:DMNShape id="DMNShape_input_data_1" dmnElementRef="input_data_order_amount">
           <dc:Bounds x="100" y="100" width="180" height="80"/>
         </dmndi:DMNShape>
         <dmndi:DMNShape id="DMNShape_decision_1" dmnElementRef="decision_id">
           <dc:Bounds x="400" y="100" width="180" height="80"/>
         </dmndi:DMNShape>
         <dmndi:DMNEdge id="DMNEdge_1" dmnElementRef="information_requirement_id">
           <di:waypoint x="280" y="140"/>
           <di:waypoint x="400" y="140"/>
         </dmndi:DMNEdge>
       </dmndi:DMNDiagram>
     </dmndi:DMNDI>
     ```

### 🔍 Analysis Requirements

**CRITICAL: You MUST analyze the condition and action to understand the relationships:**

1. **Input Data Analysis:**
   - Parse the condition to extract ALL variables (e.g., "orderAmount", "customerType", "age")
   - For each variable, determine:
     - Variable name (camelCase or snake_case)
     - Data type (number, string, boolean, date, etc.)
     - Display name (Korean, human-readable)
   - Create a `<inputData>` element for EACH unique variable

2. **Decision Table Structure:**
   - Map each inputData variable to a decision table `<input>` column
   - Use `<informationRequirement>` to link decision to inputData
   - Ensure `<inputExpression><text>` references the variable name from inputData
   - Example: If inputData has variable "orderAmount", then `<inputExpression><text>orderAmount</text></inputExpression>`

3. **Knowledge Source Analysis:**
   - Check if the feedback mentions:
     - Policies, regulations, guidelines
     - External data sources
     - Business rules from documents
   - If yes, create `<knowledgeSource>` elements and link them to the decision

4. **Business Knowledge Model Analysis:**
   - Check if the action involves:
     - Complex calculations (e.g., "10% discount", "amount * 0.1")
     - Reusable business logic
     - Transformations or validations
   - If yes, create `<businessKnowledgeModel>` elements and invoke them from the decision

5. **Relationship Mapping:**
   - Use `<informationRequirement>` to link decision to inputData
   - Use `<knowledgeRequirement>` to link decision to knowledgeSource
   - Use `<businessKnowledgeModel>` invocation to link decision to BKM
   - Ensure all relationships are properly defined in the XML

### 📋 XML Structure Requirements

**Root Element:**
```xml
<definitions xmlns="https://www.omg.org/spec/DMN/20191111/MODEL/" 
             xmlns:dmndi="https://www.omg.org/spec/DMN/20191111/DMNDI/" 
             xmlns:dc="http://www.omg.org/spec/DMN/20180521/DC/"
             xmlns:di="http://www.omg.org/spec/DMN/20180521/DI/"
             id="Definitions_1" 
             name="DRD" 
             namespace="http://camunda.org/schema/1.0/dmn">
  <!-- inputData elements -->
  <!-- knowledgeSource elements (if needed) -->
  <!-- businessKnowledgeModel elements (if needed) -->
  <!-- decision element -->
  <!-- dmndi:DMNDI section -->
</definitions>
```

**Element Order (within `<definitions>`):**
1. `<inputData>` elements (all input data definitions)
2. `<knowledgeSource>` elements (if any)
3. `<businessKnowledgeModel>` elements (if any)
4. `<decision>` element (the main decision logic)
5. `<dmndi:DMNDI>` section (visual representation)

**Input Element Structure:**
```xml
<input id="input_1">
  <inputExpression id="input_expr_1" typeRef="number">
    <text>orderAmount</text>
  </inputExpression>
  <label>주문 금액</label>
</input>
```
- `<label>` MUST be a direct child of `<input>`, NOT inside `<inputExpression>`
- `<text>` inside `<inputExpression>` MUST match the variable name from inputData

**Hit Policy:**
- Use full names: UNIQUE, ANY, FIRST, PRIORITY, OUTPUT ORDER, RULE ORDER, COLLECT
- Select based on rule structure:
  * UNIQUE: Each input combination matches exactly one rule
  * FIRST: Return the first matching rule
  * PRIORITY: Multiple rules can match, return highest priority
  * OUTPUT ORDER: Multiple rules can match, return in specified order
  * RULE ORDER: Multiple rules can match, return based on rule order
  * COLLECT: Multiple rules can match, return all as a list
  * ANY: Multiple rules can match with same output

**IDs / Naming:**
- All element IDs use lowercase_snake_case (e.g., `order_amount_decision`, `input_data_1`, `rule_1`)
- IDs should be meaningful to the business domain
- Display names (`name` attributes) should be short, human-readable Korean

### ✅ Validation Checklist

Before generating the XML, ensure:
- [ ] ALL input variables from the condition have corresponding `<inputData>` elements
- [ ] The `<decision>` has `<informationRequirement>` linking to each inputData
- [ ] Each `<input>` in the decision table references the correct inputData variable
- [ ] If policies/regulations are mentioned, `<knowledgeSource>` elements are created
- [ ] If calculations/transformations exist, `<businessKnowledgeModel>` elements are created
- [ ] All relationships are properly defined (informationRequirement, knowledgeRequirement, etc.)
- [ ] The `<dmndi:DMNDI>` section includes shapes for ALL elements
- [ ] All element IDs are unique across the document
- [ ] XML is well-formed with proper escaping

Generate the complete DMN XML model now and return ONLY the JSON object with dmnXml and description fields.
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
    LLM을 사용하여 기존 DMN XML에 새 규칙을 추가/확장 (완전한 모델 구조 유지)
    
    기존 모델 구조(inputData, knowledgeSource, businessKnowledgeModel)를 분석하고,
    조건과 규칙 간의 관계를 파악하여 새로운 규칙을 통합합니다.
    
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
Your task is to **EXTEND** an existing DMN model by adding new rules, while **PRESERVING the complete model structure**.

**CRITICAL: DO NOT REPLACE OR REMOVE EXISTING ELEMENTS. ADD NEW RULES AND EXTEND THE MODEL AS NEEDED.**

### Existing DMN XML:
```xml
{existing_xml}
```

### New Rule to Add:
- **Condition:** {new_condition}
- **Action/Result:** {new_action}
{f"- **Context from Feedback:** {feedback_content}" if feedback_content else ""}

### 🎯 Your Task:

1. **Analyze the existing model structure:**
   - Identify ALL existing `<inputData>` elements and their variables
   - Identify existing `<knowledgeSource>` elements (if any)
   - Identify existing `<businessKnowledgeModel>` elements (if any)
   - Identify existing `<decision>` structure and all existing rules
   - Understand the relationships between elements (informationRequirement, knowledgeRequirement, etc.)

2. **Analyze the new condition:**
   - Extract ALL variables from the new condition
   - Compare with existing inputData variables
   - Determine if new inputData elements are needed
   - Determine if existing inputData can be reused

3. **Analyze the new action:**
   - Check if the action requires calculations or transformations
   - Determine if a new `<businessKnowledgeModel>` is needed
   - Check if existing BKM can be reused

4. **Extend the model appropriately:**
   - **PRESERVE** all existing `<inputData>` elements
   - **ADD** new `<inputData>` elements ONLY if the new condition introduces new variables
   - **PRESERVE** all existing `<knowledgeSource>` elements
   - **ADD** new `<knowledgeSource>` elements if the feedback mentions new policies/regulations
   - **PRESERVE** all existing `<businessKnowledgeModel>` elements
   - **ADD** new `<businessKnowledgeModel>` elements if the new action requires new calculations
   - **PRESERVE** all existing `<rule>` elements in the decision table
   - **ADD** new `<rule>` element(s) for the new condition-action mapping
   - **UPDATE** the decision table structure if new input columns are needed
   - **UPDATE** `<informationRequirement>` if new inputData is added
   - **UPDATE** hitPolicy if the rule relationships change

5. **Ensure proper relationships:**
   - If new inputData is added, create `<informationRequirement>` linking decision to new inputData
   - If new knowledgeSource is added, create `<knowledgeRequirement>` linking decision to new knowledgeSource
   - If new BKM is added, invoke it from the decision
   - Update `<dmndi:DMNDI>` section to include shapes for all new elements

6. **Rule ID management:**
   - Preserve all existing rule IDs
   - Generate new unique rule IDs (e.g., if last rule is "rule_5", new rules should be "rule_6", "rule_7", etc.)
   - Ensure all element IDs are unique across the document

7. **Hit Policy analysis:**
   - Analyze if the new rules create overlapping conditions
   - If multiple rules can match with different outputs → Use PRIORITY, OUTPUT ORDER, or COLLECT
   - If rules are mutually exclusive → Use UNIQUE or FIRST
   - If rules can overlap but need specific ordering → Use PRIORITY or RULE ORDER
   - Update hitPolicy if the existing one is no longer appropriate
   - Document the reason for hitPolicy change in the "changes" field

### 📋 Model Extension Guidelines

**InputData Handling:**
- If new condition uses variable "customerAge" but existing model has "age", decide:
  - Reuse existing "age" inputData if semantically equivalent
  - Create new "customerAge" inputData if it represents different data
- Add new inputData elements BEFORE the decision element
- Ensure variable names match between inputData and decision table inputs

**Decision Table Extension:**
- If new condition requires new input columns, add them to the decision table
- Ensure all existing rules have entries for new input columns (use "-" for "don't care")
- Add new rule(s) with proper inputEntry and outputEntry
- Maintain consistency in input/output structure

**KnowledgeSource Handling:**
- If feedback mentions new policies/regulations, add new knowledgeSource elements
- Link new knowledgeSource to decision using `<knowledgeRequirement>`
- Preserve all existing knowledgeSource elements

**BusinessKnowledgeModel Handling:**
- If new action requires calculations, check if existing BKM can be reused
- If not, create new BKM with appropriate logic
- Invoke BKM from decision if needed
- Preserve all existing BKM elements

**Visual Representation:**
- Update `<dmndi:DMNDI>` to include shapes for all new elements
- Position new shapes appropriately (inputData on left, decision in center, etc.)
- Add edges (DMNEdge) for new relationships

### 🎯 Output format (STRICT)
Return **ONLY valid JSON** — no markdown fences, no comments, no extra text.
The JSON must exactly follow this schema:

{{
    "dmnXml": "<complete EXTENDED DMN XML as a single-line escaped string>",
    "description": "<brief explanation in Korean of what was added>",
    "changes": "<summary of changes: rules added, inputData added, relationships updated, hitPolicy changes, etc.>"
}}

Rules:
- The top-level value MUST be a valid JSON object.
- Do not wrap the JSON in ```.
- All double quotes inside dmnXml MUST be escaped as \\".
- All line breaks inside dmnXml MUST be escaped as \\n.
- **ALL EXISTING ELEMENTS MUST BE PRESERVED IN THE OUTPUT**
- **ONLY ADD NEW ELEMENTS OR UPDATE RELATIONSHIPS AS NEEDED**

### ✅ Validation Checklist

Before generating the XML, ensure:
- [ ] ALL existing inputData elements are preserved
- [ ] ALL existing knowledgeSource elements are preserved (if any)
- [ ] ALL existing businessKnowledgeModel elements are preserved (if any)
- [ ] ALL existing rules are preserved
- [ ] New inputData elements are added only if needed
- [ ] New rules are added with unique IDs
- [ ] Decision table structure is updated if new input columns are needed
- [ ] All existing rules have entries for new input columns (use "-" if not applicable)
- [ ] InformationRequirement relationships are updated if new inputData is added
- [ ] HitPolicy is updated if rule relationships change
- [ ] DMNDI section includes shapes for all elements (existing + new)
- [ ] All element IDs are unique across the document
- [ ] XML is well-formed with proper escaping

Generate the extended DMN XML model now, preserving all existing elements and adding the new ones.
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

async def commit_to_dmn_rule(agent_id: str, dmn_artifact: Dict, feedback_content: str = "", operation: str = "CREATE", rule_id: str = None, merge_mode: str = "REPLACE"):
    """
    DMN Rule을 proc_def 테이블에 CRUD 작업 수행
    
    Args:
        agent_id: 에이전트 ID (owner 필드에 저장)
        dmn_artifact: DMN 규칙 정보 {"condition": "...", "action": "...", "name": "..." (optional)}
        feedback_content: 원본 피드백 내용 (선택적, 더 정확한 XML 생성을 위해)
        operation: "CREATE" | "UPDATE" | "DELETE"
        rule_id: UPDATE/DELETE 시 기존 규칙 ID (필수)
        merge_mode: "REPLACE" | "EXTEND" | "REFINE" (기본값: REPLACE)
                    - REPLACE: 완전 대체 (기존 구조 변경 가능)
                    - EXTEND: 기존 규칙 보존 + 새 규칙 추가
                    - REFINE: 기존 규칙 참조 후 일부 수정
    
    Raises:
        ValueError: 필수 파라미터가 없거나 에이전트를 찾을 수 없을 때
        Exception: 작업 실패 시
    """
    try:
        supabase = get_db_client()

        # ⚠️ 방어 로직: rule_id가 있는데도 CREATE로 들어오면 실제로는 UPDATE로 처리
        # 상위 래퍼(commit_dmn_rule_wrapper)나 LLM이 operation을 잘못 넣어도,
        # 여기에서는 절대 새 규칙을 만들지 않고 기존 규칙을 수정하도록 강제한다.
        if operation == "CREATE" and rule_id:
            log(f"⚠️ DMN_RULE 커밋: operation='CREATE' 이지만 rule_id가 전달됨 → UPDATE로 강제 전환 (rule_id={rule_id})")
            operation = "UPDATE"
        
        if operation == "DELETE":
            if not rule_id:
                log(f"⚠️ DELETE 작업인데 rule_id가 없음")
                raise ValueError("DELETE 작업에는 rule_id가 필요합니다")
            
            # 삭제 전 이전 내용 조회 (변경 이력용)
            previous_content = None
            rule_name = ""
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
                    # 이전 내용은 XML 텍스트로 저장
                    previous_content = rule_data.data.get("bpmn", "")
                    rule_name = rule_data.data.get("name", "")
            except Exception:
                pass
            
            # 하드 삭제: 행 자체를 제거
            supabase.table('proc_def').delete().eq('id', rule_id).eq('owner', agent_id).execute()
            
            log(f"🗑️ DMN_RULE 하드 삭제 완료: 에이전트 {agent_id}, rule_id={rule_id}")
            
            # 변경 이력 기록 (실패 시 전체 작업 실패: "변경 이력에 저장 안되면 무조건 실패")
            try:
                agent_info = _get_agent_by_id(agent_id)
                tenant_id = agent_info.get("tenant_id") if agent_info else None
                
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
                log(f"   ❌ DMN_RULE 변경 이력 기록 실패: {e}")
                raise
            
            return
        
        # CREATE 또는 UPDATE인 경우
        condition = dmn_artifact.get("condition", "")
        action = dmn_artifact.get("action", "")
        # 기본 규칙 이름 (UPDATE의 경우 아래에서 기존 이름으로 override)
        # 이름이 비어 있으면 "이름 없는 DMN 규칙"으로 저장 (LLM 기본값에 의존하지 않음)
        rule_name = (dmn_artifact.get("name") or "").strip() or "이름 없는 DMN 규칙"
        
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
            
            # 업데이트 전 기존 규칙 조회 (변경 이력용 및 병합용)
            previous_content = None
            existing_xml = None
            current_version = None
            try:
                rule_data = (
                    supabase.table('proc_def')
                    .select('name, bpmn, prod_version')
                    .eq('id', rule_id)
                    .eq('owner', agent_id)
                    .single()
                    .execute()
                )
                if rule_data.data:
                    # 이전 내용은 XML 텍스트로 저장
                    previous_content = rule_data.data.get("bpmn", "")
                    existing_xml = previous_content
                    current_version = rule_data.data.get("prod_version")

                    # ⚠️ UPDATE 시 규칙 이름은 기본적으로 "기존 규칙 이름"을 유지한다.
                    # LLM이 dmn_artifact.name에 임의의 기본값을 넣더라도
                    # 기존 규칙명을 덮어쓰지 않도록 방어.
                    existing_name = rule_data.data.get("name")
                    if existing_name:
                        log(f"🔧 DMN_RULE UPDATE: 기존 규칙 이름 유지 → '{existing_name}'")
                        rule_name = existing_name
                    
                    # prod_version이 없거나 버전 테이블에 없는 경우 처리
                    if not current_version or current_version.strip() == "":
                        log(f"⚠️ 기존 규칙에 prod_version이 없음. 버전 테이블 확인 중...")
                        # 버전 테이블에 기존 버전이 있는지 확인
                        try:
                            existing_version = (
                                supabase.table('proc_def_version')
                                .select('version')
                                .eq('proc_def_id', rule_id)
                                .order('timeStamp', desc=True)
                                .limit(1)
                                .execute()
                            )
                            if existing_version.data and len(existing_version.data) > 0:
                                current_version = existing_version.data[0].get('version')
                                log(f"   버전 테이블에서 버전 발견: {current_version}")
                            else:
                                # 버전 테이블에도 없으면 기존 XML을 초기 버전으로 저장
                                log(f"   버전 테이블에 기존 버전이 없음. 기존 XML을 초기 버전(1.0.0)으로 저장")
                                if existing_xml:
                                    await _save_dmn_version(
                                        proc_def_id=rule_id,
                                        version="1.0.0",
                                        dmn_xml=existing_xml,
                                        tenant_id=tenant_id,
                                        previous_xml=None,
                                        merge_mode="INITIAL",
                                        feedback_content="기존 규칙 초기 버전 생성"
                                    )
                                    # proc_def의 prod_version도 업데이트
                                    supabase.table('proc_def').update({
                                        'prod_version': '1.0.0'
                                    }).eq('id', rule_id).eq('owner', agent_id).execute()
                                    current_version = "1.0.0"
                                    log(f"   초기 버전(1.0.0) 생성 완료")
                        except Exception as e:
                            log(f"   ⚠️ 버전 테이블 확인 중 오류 (무시하고 계속 진행): {e}")
            except Exception:
                pass
            
            # merge_mode에 따라 처리
            if dmn_artifact.get("bpmn") or dmn_artifact.get("full_xml"):
                # 에이전트가 이미 완성된 XML을 전달한 경우 (모든 merge_mode에서 우선)
                dmn_xml = dmn_artifact.get("bpmn") or dmn_artifact.get("full_xml")
                log(f"✅ 에이전트가 전달한 XML 사용 (길이: {len(dmn_xml)}자)")
            elif merge_mode == "EXTEND" and existing_xml:
                # EXTEND 모드: 기존 규칙 보존 + 새 규칙 추가
                log(f"🔄 EXTEND 모드: 기존 DMN XML에 새 규칙 추가: {rule_name}")
                log(f"   기존 XML 길이: {len(existing_xml)}자")
                log(f"   새 조건: {condition}")
                log(f"   새 결과: {action}")
                dmn_xml = await _extend_dmn_xml_llm(existing_xml, rule_name, condition, action, feedback_content)
            elif merge_mode == "REFINE" and existing_xml:
                # REFINE 모드: 기존 규칙 참조 후 일부 수정 (현재는 REPLACE와 동일하게 처리)
                log(f"🔧 REFINE 모드: 기존 DMN XML 참조 후 수정: {rule_name}")
                log(f"   기존 XML 길이: {len(existing_xml)}자")
                log(f"   새 조건: {condition}")
                log(f"   새 결과: {action}")
                # TODO: REFINE 모드의 세밀한 수정 로직 구현 (현재는 REPLACE와 동일)
                log(f"   ⚠️ REFINE 모드는 현재 REPLACE와 동일하게 처리됩니다.")
                dmn_xml = await _generate_dmn_xml_llm(rule_name, condition, action, feedback_content)
            else:
                # REPLACE 모드 또는 기존 XML이 없는 경우: 새 XML 생성 또는 대체
                if merge_mode == "REPLACE":
                    log(f"🔄 REPLACE 모드: DMN XML 새로 생성/대체: {rule_name}")
                else:
                    log(f"🤖 LLM을 사용하여 DMN XML 생성 시작: {rule_name}")
                    if not existing_xml:
                        log(f"   ⚠️ 기존 XML을 찾을 수 없어 새로 생성합니다.")
                dmn_xml = await _generate_dmn_xml_llm(rule_name, condition, action, feedback_content)
            
            # 다음 버전 번호 생성
            next_version = _get_next_version(current_version, merge_mode)
            
            # 버전 정보 저장
            version_uuid = await _save_dmn_version(
                proc_def_id=rule_id,
                version=next_version,
                dmn_xml=dmn_xml,
                tenant_id=tenant_id,
                previous_xml=previous_content,
                merge_mode=merge_mode,
                feedback_content=feedback_content
            )
            
            # 기존 규칙 업데이트 (prod_version 포함)
            resp = supabase.table('proc_def').update({
                'name': rule_name,
                'bpmn': dmn_xml,
                'prod_version': next_version,
            }).eq('id', rule_id).eq('owner', agent_id).execute()
            
            log(f"✏️ DMN_RULE 수정 완료: 에이전트 {agent_id}, rule_id={rule_id}")
            log(f"   규칙 이름: {rule_name}")
            log(f"   조건: {condition}")
            log(f"   결과: {action}")
            log(f"   버전: {current_version or '(없음)'} → {next_version}")
            
            # 변경 이력 기록 (버전 UUID를 변경 이력 UUID로 사용) - 실패 시 전체 작업 실패
            try:
                agent_info = _get_agent_by_id(agent_id)
                tenant_id = agent_info.get("tenant_id") if agent_info else None
                
                # 새 내용은 XML 텍스트로 저장
                new_content = dmn_xml
                
                record_knowledge_history(
                    knowledge_type="DMN_RULE",
                    knowledge_id=rule_id,
                    agent_id=agent_id,
                    tenant_id=tenant_id,
                    operation="UPDATE",
                    previous_content=previous_content,
                    new_content=new_content,
                    feedback_content=feedback_content,
                    knowledge_name=rule_name,
                    version_uuid=version_uuid  # 변경 이력 UUID = 버전 UUID (프론트엔드에서 버전 조회용)
                )
                log(f"   🔗 변경 이력과 버전 연결: history_uuid={version_uuid}")
            except Exception as e:
                log(f"   ❌ DMN_RULE 변경 이력 기록 실패: {e}")
                raise
            
        else:  # CREATE
            # LLM을 사용하여 새 DMN XML 생성
            log(f"🤖 LLM을 사용하여 DMN XML 생성 시작: {rule_name}")
            dmn_xml = await _generate_dmn_xml_llm(rule_name, condition, action, feedback_content)
            
            # UUID 생성
            rule_uuid = str(uuid.uuid4())
            new_rule_id = str(uuid.uuid4())
            
            # 초기 버전 번호 생성
            initial_version = "1.0.0"
            
            # proc_def 테이블에 저장 (prod_version 포함)
            resp = supabase.table('proc_def').insert({
                'id': new_rule_id,
                'name': rule_name,
                'definition': None,
                'bpmn': dmn_xml,
                'uuid': rule_uuid,
                'tenant_id': tenant_id,
                'isdeleted': False,
                'owner': agent_id,
                'type': 'dmn',
                'prod_version': initial_version
            }).execute()
            
            # 초기 버전 정보 저장
            version_uuid = await _save_dmn_version(
                proc_def_id=new_rule_id,
                version=initial_version,
                dmn_xml=dmn_xml,
                tenant_id=tenant_id,
                previous_xml=None,
                merge_mode="CREATE",
                feedback_content=feedback_content
            )
            
            log(f"✅ DMN_RULE 저장 완료: 에이전트 {agent_id}")
            log(f"   규칙 ID: {new_rule_id}")
            log(f"   규칙 이름: {rule_name}")
            log(f"   조건: {condition}")
            log(f"   결과: {action}")
            log(f"   초기 버전: {initial_version}")
            
            # 변경 이력 기록 (버전 UUID를 변경 이력 UUID로 사용) - 실패 시 전체 작업 실패
            try:
                # 새 내용은 XML 텍스트로 저장
                new_content = dmn_xml
                
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
                    batch_job_id=batch_job_id,
                    version_uuid=version_uuid  # 변경 이력 UUID = 버전 UUID (프론트엔드에서 버전 조회용)
                )
                log(f"   🔗 변경 이력과 버전 연결: history_uuid={version_uuid}")
            except Exception as e:
                log(f"   ❌ DMN_RULE 변경 이력 기록 실패: {e}")
                raise
        
    except Exception as e:
        handle_error(f"DMN_RULE{operation}", e)
        raise
