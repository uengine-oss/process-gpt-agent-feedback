"""
의미적 지식 유사도 분석기
언어/형식이 달라도 의미적으로 동일한 지식을 인식하는 모듈
"""

import json
from typing import Dict, List, Optional, Tuple
from core.llm import create_llm
from utils.logger import log, handle_error


def clean_json_response(content: str) -> str:
    """LLM 응답에서 백틱과 json 키워드 제거"""
    content = content.replace("```json", "").replace("```", "")
    return content.strip()


class SemanticKnowledgeMatcher:
    """의미 기반 지식 유사도 분석기"""
    
    def __init__(self):
        self.llm = create_llm(model="gpt-4o", streaming=False, temperature=0)
    
    async def find_similar_knowledge(
        self,
        new_knowledge: str,
        existing_knowledge: List[Dict],
        knowledge_type: str,
        threshold: float = 0.5  # 낮춰서 더 많은 관련 지식을 반환
    ) -> List[Dict]:
        """
        새 지식과 의미적으로 유사한 기존 지식 검색
        에이전트가 직접 판단할 수 있도록 충분한 정보 제공
        
        Args:
            new_knowledge: 새로운 지식 내용 (텍스트)
            existing_knowledge: 기존 지식 목록
            knowledge_type: 지식 타입 (MEMORY | DMN_RULE | SKILL)
            threshold: 유사도 임계값 (0.0-1.0)
        
        Returns:
            유사한 지식 목록 (관계 분석 포함, 작업 추천 없음)
        """
        if not existing_knowledge:
            return []
        
        try:
            # 기존 지식 포맷팅 (더 상세하게)
            existing_formatted = self._format_existing_knowledge_detailed(existing_knowledge, knowledge_type)
            
            prompt = f"""다음 새로운 지식과 기존 지식들의 **관계**를 분석해주세요.
에이전트가 직접 판단할 수 있도록 상세한 분석 정보를 제공하세요.

**새로운 지식:**
{new_knowledge}

**기존 지식 목록:**
{existing_formatted}

**관계 유형 정의:**
- DUPLICATE: 완전히 동일한 내용 (언어/표현만 다름)
- EXTENDS: 새 지식이 기존 지식에 조건/규칙을 추가 (기존 유지 + 확장)
- REFINES: 새 지식이 기존 지식의 세부 값을 변경
- CONFLICTS: 새 지식이 기존 지식과 모순/상충
- EXCEPTION: 새 지식이 기존 규칙의 예외 케이스
- SUPERSEDES: 새 지식이 기존 지식을 완전히 대체
- COMPLEMENTS: 서로 다른 측면을 다룸 (관련은 있으나 독립적)
- UNRELATED: 관계 없음

**응답 형식:**
JSON 형식으로만 응답하세요. 마크다운 코드블록은 사용하지 마세요.

{{
  "similar_items": [
    {{
      "id": "기존 지식 ID",
      "name": "기존 지식 이름/제목",
      "similarity_score": 0.0-1.0,
      "relationship": "위 관계 유형 중 하나",
      "relationship_reason": "이 관계로 판단한 구체적인 이유",
      "key_differences": ["기존 지식과 새 지식의 핵심 차이점 목록"],
      "key_similarities": ["기존 지식과 새 지식의 핵심 유사점 목록"],
      "content_summary": "기존 지식의 핵심 내용 요약"
    }}
  ]
}}

유사도가 {threshold} 이상인 항목만 포함하세요. 관련성이 있으면 낮은 유사도도 포함하세요."""

            response = await self.llm.ainvoke(prompt)
            cleaned_content = clean_json_response(response.content)
            
            result = json.loads(cleaned_content)
            similar_items = result.get("similar_items", [])
            
            # 원본 지식 정보 추가 (에이전트가 상세 조회 가능하도록)
            for item in similar_items:
                item_id = item.get("id")
                for existing in existing_knowledge:
                    existing_id = existing.get("id", existing.get("name", ""))
                    if existing_id == item_id:
                        item["original"] = existing
                        # 전체 내용도 포함 (에이전트가 직접 비교할 수 있도록)
                        item["full_content"] = self._get_knowledge_content(existing, knowledge_type)
                        break
            
            log(f"🔍 유사 지식 분석 완료: {len(similar_items)}개 발견 (임계값: {threshold})")
            return similar_items
            
        except json.JSONDecodeError as e:
            log(f"❌ 유사도 분석 JSON 파싱 실패: {e}")
            return []
        except Exception as e:
            handle_error("의미적유사도분석", e)
            return []
    
    def _format_existing_knowledge_detailed(self, knowledge_list: List[Dict], knowledge_type: str) -> str:
        """기존 지식 목록을 상세 분석용 텍스트로 포맷팅"""
        formatted = []
        
        for idx, item in enumerate(knowledge_list, start=1):
            item_id = item.get("id", item.get("name", f"item_{idx}"))
            content = self._get_knowledge_content(item, knowledge_type)
            
            formatted.append(f"[{idx}] ID: {item_id}")
            
            if knowledge_type == "DMN_RULE":
                name = item.get("name", "")
                formatted.append(f"    이름: {name}")
                # DMN XML의 경우 더 많은 내용 포함
                formatted.append(f"    내용: {content[:1000]}...")
            elif knowledge_type == "SKILL":
                name = item.get("name", item.get("skill_name", ""))
                formatted.append(f"    이름: {name}")
                formatted.append(f"    내용: {content}")
            else:  # MEMORY
                formatted.append(f"    내용: {content}")
            
            formatted.append("")
        
        return "\n".join(formatted)
    
    async def verify_duplicate(
        self,
        new_knowledge: str,
        candidate: Dict,
        knowledge_type: str
    ) -> Dict:
        """
        특정 지식이 중복인지 상세 검증
        
        Args:
            new_knowledge: 새로운 지식 내용
            candidate: 중복 후보 지식
            knowledge_type: 지식 타입
        
        Returns:
            검증 결과
        """
        try:
            candidate_content = self._get_knowledge_content(candidate, knowledge_type)
            
            prompt = f"""다음 두 지식이 의미적으로 동일한지 상세 분석해주세요.

**새로운 지식:**
{new_knowledge}

**기존 지식:**
ID: {candidate.get("id", candidate.get("name", "Unknown"))}
내용: {candidate_content}

**분석 요청:**
1. 두 지식이 의미적으로 동일한지 판단
2. 동일하다면 어떤 부분이 같은지 설명
3. 다르다면 어떤 점이 다른지 설명
4. 권장 작업 결정 (UPDATE | CREATE | IGNORE)

**응답 형식:**
JSON 형식으로만 응답하세요. 마크다운 코드블록은 사용하지 마세요.

{{
  "is_duplicate": true/false,
  "confidence": 0.0-1.0,
  "same_aspects": ["동일한 부분 목록"],
  "different_aspects": ["다른 부분 목록"],
  "recommended_operation": "UPDATE | CREATE | IGNORE",
  "reason": "판단 이유"
}}"""

            response = await self.llm.ainvoke(prompt)
            cleaned_content = clean_json_response(response.content)
            
            result = json.loads(cleaned_content)
            result["candidate_id"] = candidate.get("id", candidate.get("name", ""))
            
            log(f"🔎 중복 검증 완료: is_duplicate={result.get('is_duplicate')}, confidence={result.get('confidence')}")
            return result
            
        except json.JSONDecodeError as e:
            log(f"❌ 중복 검증 JSON 파싱 실패: {e}")
            return {
                "is_duplicate": False,
                "confidence": 0.0,
                "recommended_operation": "CREATE",
                "reason": f"검증 실패: {str(e)}"
            }
        except Exception as e:
            handle_error("중복검증", e)
            return {
                "is_duplicate": False,
                "confidence": 0.0,
                "recommended_operation": "CREATE",
                "reason": f"검증 에러: {str(e)}"
            }
    
    async def analyze_relationship(
        self,
        new_knowledge: str,
        similar_items: List[Dict],
        knowledge_type: str
    ) -> Dict:
        """
        유사 지식과의 관계를 분석하여 정보 제공 (결정은 에이전트가 함)
        
        Args:
            new_knowledge: 새로운 지식
            similar_items: 유사 지식 목록 (find_similar_knowledge 결과)
            knowledge_type: 지식 타입
        
        Returns:
            관계 분석 정보 (작업 추천 없음, 에이전트가 판단)
        """
        if not similar_items:
            return {
                "has_related_knowledge": False,
                "analysis": "기존에 관련된 지식이 없습니다. 새로운 지식으로 판단됩니다.",
                "related_items": []
            }
        
        # 관계별로 분류
        analysis_result = {
            "has_related_knowledge": True,
            "total_related": len(similar_items),
            "related_items": [],
            "analysis": ""
        }
        
        relationship_groups = {}
        for item in similar_items:
            rel = item.get("relationship", "UNKNOWN")
            if rel not in relationship_groups:
                relationship_groups[rel] = []
            relationship_groups[rel].append(item)
        
        # 분석 텍스트 생성
        analysis_lines = []
        for rel_type, items in relationship_groups.items():
            analysis_lines.append(f"- {rel_type} 관계: {len(items)}개")
            for item in items:
                item_id = item.get("id", "unknown")
                item_name = item.get("name", item_id)
                reason = item.get("relationship_reason", "")
                analysis_lines.append(f"  * {item_name} (ID: {item_id}): {reason}")
        
        analysis_result["analysis"] = "\n".join(analysis_lines)
        analysis_result["relationship_summary"] = {k: len(v) for k, v in relationship_groups.items()}
        
        # 에이전트가 판단할 수 있도록 상세 정보 포함
        for item in similar_items:
            related_item = {
                "id": item.get("id"),
                "name": item.get("name", item.get("id")),
                "relationship": item.get("relationship"),
                "relationship_reason": item.get("relationship_reason", ""),
                "similarity_score": item.get("similarity_score", 0),
                "key_differences": item.get("key_differences", []),
                "key_similarities": item.get("key_similarities", []),
                "content_summary": item.get("content_summary", ""),
                "full_content": item.get("full_content", "")
            }
            analysis_result["related_items"].append(related_item)
        
        return analysis_result
    
    # 하위 호환성을 위해 기존 함수 유지 (deprecated)
    async def determine_operation(
        self,
        new_knowledge: str,
        similar_items: List[Dict],
        knowledge_type: str
    ) -> Dict:
        """
        [DEPRECATED] analyze_relationship 사용 권장
        하위 호환성을 위해 유지되지만, 결정은 에이전트가 해야 함
        """
        analysis = await self.analyze_relationship(new_knowledge, similar_items, knowledge_type)
        
        # 하위 호환성을 위한 최소한의 응답 (에이전트 판단 유도)
        if not analysis.get("has_related_knowledge"):
            return {
                "suggested_action": "CREATE (관련 지식 없음)",
                "analysis": analysis,
                "note": "⚠️ 이것은 참고용 정보입니다. 최종 결정은 에이전트가 관계를 분석하여 직접 판단해야 합니다."
            }
        
        return {
            "suggested_action": "에이전트가 관계를 분석하여 판단 필요",
            "analysis": analysis,
            "note": "⚠️ 이것은 참고용 정보입니다. 관계 유형(EXTENDS, REFINES, CONFLICTS 등)을 분석하여 적절한 처리 방법을 직접 결정하세요."
        }
    
    def _format_existing_knowledge(self, knowledge_list: List[Dict], knowledge_type: str) -> str:
        """기존 지식 목록을 분석용 텍스트로 포맷팅"""
        formatted = []
        
        for idx, item in enumerate(knowledge_list, start=1):
            item_id = item.get("id", item.get("name", f"item_{idx}"))
            content = self._get_knowledge_content(item, knowledge_type)
            
            formatted.append(f"[{idx}] ID: {item_id}")
            formatted.append(f"    내용: {content}")
            formatted.append("")
        
        return "\n".join(formatted)
    
    def _get_knowledge_content(self, item: Dict, knowledge_type: str) -> str:
        """지식 항목에서 내용 추출"""
        if knowledge_type == "MEMORY":
            return item.get("memory", item.get("content", ""))
        
        elif knowledge_type == "DMN_RULE":
            name = item.get("name", "")
            bpmn = item.get("bpmn", "")
            # DMN XML에서 핵심 내용 추출 시도
            if bpmn:
                return f"규칙명: {name}, XML: {bpmn[:500]}..."
            return f"규칙명: {name}"
        
        elif knowledge_type == "SKILL":
            name = item.get("name", item.get("skill_name", ""))
            description = item.get("description", "")
            content = item.get("content", "")
            steps = item.get("steps", [])
            
            parts = [f"스킬명: {name}"]
            if description:
                parts.append(f"설명: {description}")
            if steps:
                parts.append(f"단계: {', '.join(steps[:5])}")
            if content and not steps:
                parts.append(f"내용: {content[:500]}...")
            
            return "\n".join(parts)
        
        return str(item)


# 싱글톤 인스턴스
_matcher_instance = None


def get_semantic_matcher() -> SemanticKnowledgeMatcher:
    """SemanticKnowledgeMatcher 싱글톤 인스턴스 반환"""
    global _matcher_instance
    if _matcher_instance is None:
        _matcher_instance = SemanticKnowledgeMatcher()
    return _matcher_instance

