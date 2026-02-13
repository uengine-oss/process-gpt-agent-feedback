# 스킬 크리에이터 워크플로우 동작 구조

피드백이 **스킬(SKILL)** 관련일 때, **computer-use**와 **claude-skills의 skill-creator**를 사용해 스킬을 새로 만들거나 갱신하는 end-to-end 구조입니다.  
MEMORY·DMN_RULE은 기존 경로 그대로, SKILL만 플래그에 따라 skill-creator 경로로 분기합니다.

---

## 1. 전체 구조 개요

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│  피드백 폴링 (polling_manager)                                                    │
│    → match_feedback_to_agents → process_feedback_with_react (ReAct)               │
└─────────────────────────────────────────────────────────────────────────────────┘
                                          │
                      ┌───────────────────┼───────────────────┐
                      ▼                   ▼                   ▼
              commit_to_memory    commit_to_dmn_rule    commit_to_skill
               (기존)                 (기존)                    │
                                                               │
                                    ┌──────────────────────────┴──────────────────────────┐
                                    │  commit_to_skill (skill_committer)                    │
                                    │  • DELETE → 항상 skill_api_client (기존 HTTP)         │
                                    │  • CREATE/UPDATE + USE_SKILL_CREATOR + COMPUTER_USE?  │
                                    └──────────────────────────┬──────────────────────────┘
                                                               │
              ┌────────────────────────────────────────────────┼────────────────────────────────────────────────┐
              │ NO (플래그 꺼짐 또는 URL 없음)                   │ YES (skill-creator 경로)                         │
              ▼                                                ▼                                                  │
   skill_api_client (HTTP)                    commit_to_skill_via_skill_creator                                    │
   • upload_skill / update_skill_file         • computer-use Pod에서 init_skill / package_skill / quick_validate  │
   • 기존과 동일                              • .skill zip → skill_api_client (upload/update) + DB/이력            │
                                              • 실패 시 예외 전파 (HTTP 폴백 없음)                                 │
```

---

## 2. 진입 경로 (피드백 → 스킬 커밋까지)

| 단계 | 모듈 | 역할 |
|------|------|------|
| 1 | `polling_manager` | `fetch_feedback_task`로 피드백 조회, `match_feedback_to_agents`로 에이전트별 학습 후보 생성 |
| 2 | `react_agent.process_feedback_with_react` | ReAct 에이전트 실행. `feedback_content`를 `create_feedback_react_agent` → `create_react_tools`에 전달 |
| 3 | `react_tools` | 도구: `search_similar_knowledge`, `get_knowledge_detail`, `analyze_knowledge_conflict`, `commit_to_skill` 등 |
| 4 | `commit_skill_wrapper` | `_commit_skill_tool(agent_id, operation, skill_id, merge_mode, feedback_content, relationship_analysis, related_skill_ids)` 호출. `feedback_content`는 클로저에서. |
| 5 | `_commit_skill_tool` | `commit_to_skill(agent_id, skill_artifact=None, operation, skill_id, feedback_content, merge_mode, relationship_analysis, related_skill_ids)` 호출 |
| 6 | `skill_committer.commit_to_skill` | **분기**: skill-creator 조건 충족 시 `commit_to_skill_via_skill_creator(..., skill_artifact=skill_artifact)`, 아니면 기존 HTTP |

**ReAct 에이전트 역할 (스킬): 저장소·관계만. 스킬 내용은 skill-creator가 생성.**
- ReAct은 **(1) 저장소=SKILL (2) CREATE vs UPDATE vs DELETE (3) UPDATE/DELETE 시 skill_id** 만 판단. `commit_to_skill(operation=..., skill_id=..., relationship_analysis=..., related_skill_ids=...)` 호출. `skill_artifact_json`·`steps`·`additional_files`는 **넘기지 않음**. 피드백은 `create_react_tools(feedback_content)`에서 자동 전달.
- `search_similar_knowledge`에서 **관련 스킬이 하나라도 있으면**: `commit_to_skill(operation="UPDATE", skill_id=기존스킬이름)`. **CREATE는 관련 기존 스킬이 전혀 없을 때만.**
- **관련 스킬을 찾았으면** `related_skill_ids`에 해당 스킬 이름/ID를 쉼표 구분으로 전달하면, skill-creator가 해당 스킬의 요약·경로·앞부분을 컨텍스트로 받아 **스킬 간 참조(링크/경로)**를 생성하고 참조 그래프에 스킬 간 엣지가 표시되도록 할 수 있음. 참조 경로 규칙: 같은 스킬 내부는 `폴더/파일명`, 관련 스킬(외부)은 반드시 `스킬명/폴더/파일명` 형식으로 작성.
- **스킬 내용(SKILL.md, steps, additional_files)** 생성·병합은 **skill-creator**가 피드백과(UPDATE 시) 기존 스킬을 받아 **LLM으로** 수행.
- **`commit_to_skill`을 호출하지 않고** Final Answer만 쓰면 `no_commit` 실패.

---

## 3. skill_committer 분기 로직

```
commit_to_skill(agent_id, skill_artifact=None, operation, skill_id, feedback_content, merge_mode, relationship_analysis, related_skill_ids)
  ※ ReAct 경로: skill_artifact=None. learning_committer 등: skill_artifact 전달. related_skill_ids는 관련 스킬 이름 쉼표 구분.
    │
    ├─ operation == "DELETE"
    │      → skill_api_client.delete_skill + update_agent_and_tenant_skills + record_knowledge_history
    │      → (skill-creator 미사용)
    │
    ├─ USE_SKILL_CREATOR_WORKFLOW and operation in ("CREATE","UPDATE") and COMPUTER_USE_MCP_URL
    │      │
    │      ├─ try: commit_to_skill_via_skill_creator(...) → return
    │      └─ except: 로그 후 raise (HTTP 폴백 없음)
    │
    ├─ USE_SKILL_CREATOR_WORKFLOW and operation in ("CREATE","UPDATE") and not COMPUTER_USE_MCP_URL
    │      → 로그 "COMPUTER_USE_MCP_URL 없음 → 기존 HTTP 사용" 후 기존 HTTP
    │
    └─ 그 외 (기존 HTTP)
           → CREATE: upload_skill + update_agent_and_tenant_skills + record_knowledge_history
           → UPDATE: update_skill_file(SKILL.md + additional_files) + 동기화 + 이력
```

---

## 4. skill_creator_committer 내부 흐름

`commit_to_skill_via_skill_creator`는 CREATE/UPDATE만 처리합니다.

### 4.1 입력·사전 준비

**skill_artifact가 None (ReAct 경로)일 때:** `_generate_skill_artifact_from_feedback`가 **skill-creator 가이드에 따라 LLM**을 호출해 피드백과(UPDATE 시) 기존 SKILL.md·additional_files로부터 `{name, description, overview, steps, usage, body_markdown?, additional_files}` JSON을 생성. `related_skill_ids`가 있으면 해당 스킬들의 요약·파일 경로·파일 앞부분을 `[관련 스킬 참고]` 컨텍스트로 넣어 LLM이 스킬 간 참조(링크·경로)를 생성하도록 함. 본문에서 관련 스킬 파일을 참조할 때는 반드시 `스킬명/폴더/파일명` 형식(예: `skill-creator/references/workflows.md`)을 사용하고, 같은 스킬 내부만 `폴더/파일명`(예: `references/guide.md`)을 사용함. 생성 후 아래와 동일하게 진행.

**SKILL.md 본문 품질:** 목표는 내장 스킬(skill-creator, doc-coauthoring 등)과 유사한 수준. LLM은 선택적으로 `body_markdown`을 출력할 수 있으며, 제공 시 `_format_skill_document`는 frontmatter만 붙이고 본문은 `body_markdown` 그대로 사용(이중 모드). `body_markdown`이 없으면 기존처럼 `overview` + `steps` + `usage`로 3섹션 조립. `body_markdown` 사용 시 Overview, When to Use, Core Principles/Capabilities, 단계별 절차(서브섹션), references/·scripts 참조 문구, 코드 블록·표 등을 권장.

**스킬 내부 파일 품질:** `additional_files`의 references/는 전문 레퍼런스 수준(섹션 구조, when-to-read, 코드 예시·표), scripts/는 실행 가능·docstring·에러 처리 갖춘 완전한 구현, assets/는 에이전트가 실제 참조·수정할 수 있는 완성도로 작성되도록 프롬프트에 반영됨.

| 항목 | CREATE | UPDATE |
|------|--------|--------|
| skill_name | `skill_artifact["name"]` (LLM 생성 또는 `skill_id`) | `skill_id` (필수) |
| 기존 파일 | 없음 | `get_skill_files` + `get_skill_file_content`로 SKILL.md, scripts/, references/ 수집 (LLM 입력·병합용) |
| additional_files | `skill_artifact["additional_files"]` | `existing_files` 위에 `skill_artifact["additional_files"]`로 덮어쓰기 |
| SKILL.md 본문 | `_format_skill_document(..., body_markdown=artifact.get("body_markdown"))` — body_markdown 있으면 본문으로 사용, 없으면 overview+steps+usage | 동일 |

### 4.2 MCP 사용

| MCP | 도구 | 용도 |
|-----|------|------|
| **claude-skills** | `read_skill_document` | `skill-creator` 스킬에서 `scripts/init_skill.py`, `scripts/package_skill.py` 문자열 조회 |
| **computer-use** | `create_session` | Pod 세션 생성 (ttl=600) |
| **computer-use** | `create_file` | Pod 내 파일 생성 (스크립트, SKILL.md, additional_files) |
| **computer-use** | `run_shell` | `mkdir`, `init_skill`, `quick_validate`, `package_skill`, base64 출력 |
| **computer-use** | `delete_file` | init_skill 예제 파일 정리 (CREATE 시) |
| **computer-use** | `delete_session` | 세션 정리 (finally) |

### 4.3 Pod 내 작업 순서

```
1. create_session(ttl=600) → session_id

2. /tmp/skill_work/ 생성
   • quick_validate.py  ← skill_quick_validate.get_quick_validate_script()
   • package_skill.py   ← read_skill_document("skill-creator", "scripts/package_skill.py")
   • init_skill.py      ← read_skill_document("skill-creator", "scripts/init_skill.py")

3. [CREATE]
   • run: init_skill.py <skill_name> --path /tmp  → /tmp/<skill_name>/
   • create_file: /tmp/<skill_name>/SKILL.md (덮어쓰기)
   • create_file: /tmp/<skill_name>/{path} for additional_files
   • delete_file: scripts/example.py, references/api_reference.md, assets/example_asset.txt (선택)

4. [UPDATE]
   • mkdir -p /tmp/<skill_name>/{scripts,references,assets}
   • create_file: SKILL.md, additional_files 전체

5. quick_validate
   • run: cd /tmp/skill_work && python3 quick_validate.py /tmp/<skill_name>
   • "Skill is valid" 등 포함 여부로 성공 판단, 실패 시 예외

6. package_skill
   • run: cd /tmp/skill_work && python3 package_skill.py /tmp/<skill_name> /tmp
   • → /tmp/<skill_name>.skill (zip)

7. .skill 회수
   • run: python3 -c 'import base64; print(base64.b64encode(open("/tmp/<skill_name>.skill","rb").read()).decode("ascii"), end="")'
   • stdout 텍스트 → _normalize_and_decode_base64 (정규화·패딩 보정) → zip_bytes. 실패 시 예외 전파.

8. delete_session (finally)
```

### 4.4 .skill zip → API 형식

- `package_skill` 산출 zip: 루트에 `<skill_name>/` 아래 `SKILL.md`, `scripts/`, `references/` 등.
- 파싱:
  - `<skill_name>/SKILL.md` → `skill_content` (문자열)
  - `<skill_name>/scripts/*`, `references/*` 등 → `additional_files: Dict[str, str]` (예: `"scripts/foo.py"`)

### 4.5 최종 반영 (skill_api_client + DB)

| operation | 호출 | 이어서 |
|-----------|------|--------|
| CREATE | `upload_skill(skill_name, skill_content, tenant_id, additional_files)` | `update_agent_and_tenant_skills(agent_id, skill_name, "CREATE")`, `record_knowledge_history(..., operation="CREATE", new_content=..., feedback_content=...)` |
| UPDATE | `update_skill_file(skill_name, "SKILL.md", skill_content)`, `update_skill_file(skill_name, path, content)` for each in additional_files | `update_agent_and_tenant_skills(..., "UPDATE")`, `record_knowledge_history(..., previous_content=..., new_content=..., feedback_content=...)` |

**실패 시 이력 기록:** base64·zip·upload 등에서 예외가 나도, 의도된 수정 내역(`skill_content`, `previous_content`)이 있으면 `_record_attempted_skill_history`로 `record_knowledge_history`를 호출한 뒤 예외를 전파. `new_content` 끝에 `<!-- [이력] skill-creator 반영 실패: {error} -->`를 붙여 미반영 상태를 표시.

---

## 5. 환경 변수

| 변수 | 기본값 | 의미 |
|------|--------|------|
| `USE_SKILL_CREATOR_WORKFLOW` | `false` | `true`일 때만 CREATE/UPDATE에 skill-creator 경로 사용 |
| `COMPUTER_USE_MCP_URL` | `""` | computer-use MCP 서버 URL. 없으면 skill-creator 미사용, 기존 HTTP만 사용 |

---

## 6. MCP 클라이언트 (mcp_client)

- **claude-skills**: `MCP_SERVER_URL` / `MCP_SERVER_NAME` (기존)
- **computer-use**: `COMPUTER_USE_MCP_URL`이 있으면 `connections["computer-use"]`로 추가
- `get_mcp_tool_by_name_async(name)`: 비동기로 도구 조회 → `tool.ainvoke(kwargs)` 호출  
  - 이름 변형: `create_session`, `mcp_computer-use_create_session`, `mcp_cursor-computer-use_create_session` 등 시도

---

## 7. skill_quick_validate

- **로컬**: `core.skill_quick_validate.validate_skill(skill_path)`  
  - `SKILL.md` 존재, YAML frontmatter, `name`/`description` 필수, hyphen-case·길이·꺾쇠 금지 등 (PyYAML 없이 단순 파싱)
- **Pod용**: `get_quick_validate_script()`  
  - `quick_validate.py` 전체 내용 문자열 반환. `package_skill.py`의 `from quick_validate import validate_skill`와 함께 `/tmp/skill_work`에 두고 실행.

---

## 8. feedback_content 전달 경로

`record_knowledge_history`의 `feedback_content`를 위해, ReAct 쪽에서 다음처럼 전달합니다.

```
process_feedback_with_react(feedback_content=...)
    → create_feedback_react_agent(agent_id, feedback_content=feedback_content)
        → create_react_tools(agent_id, feedback_content=feedback_content)
            → commit_skill_wrapper (클로저로 feedback_content 캡처)
                → _commit_skill_tool(..., feedback_content=feedback_content or "")
                    → commit_to_skill(..., feedback_content=feedback_content)
                        → commit_to_skill_via_skill_creator(..., feedback_content=...)
                            → record_knowledge_history(..., feedback_content=...)
```

---

## 9. 경로별 요약

| 구분 | MEMORY / DMN_RULE | SKILL (기존 HTTP) | SKILL (skill-creator) |
|------|-------------------|-------------------|------------------------|
| 사용 조건 | 타겟이 MEMORY/DMN_RULE일 때 | `USE_SKILL_CREATOR_WORKFLOW` 꺼짐 또는 `COMPUTER_USE_MCP_URL` 없음, 또는 DELETE | `USE_SKILL_CREATOR_WORKFLOW` + `COMPUTER_USE_MCP_URL` 있고 CREATE/UPDATE |
| 기존 스킬 | - | 기존 conflict/HTTP 로직 | UPDATE 시 `get_skill_files` / `get_skill_file_content`로 수집 후 artifact와 병합 |
| 스킬 산출 | - | `_format_skill_document` 등으로 바로 HTTP | Pod에서 init_skill / quick_validate / package_skill → .skill zip |
| 최종 반영 | memory / dmn API | `skill_api_client` (upload/update) | .skill 파싱 후 동일한 `skill_api_client` (upload/update) |
| 이력 | 각 committer | `record_knowledge_history` | `record_knowledge_history` (feedback_content 포함) |

---

## 10. 관련 파일

| 파일 | 역할 |
|------|------|
| `core/mcp_client.py` | `USE_SKILL_CREATOR_*`, `COMPUTER_USE_MCP_URL`, computer-use connection, `get_mcp_tool_by_name_async` |
| `core/skill_quick_validate.py` | `validate_skill`, `get_quick_validate_script` |
| `core/skill_creator_committer.py` | `commit_to_skill_via_skill_creator` (Pod 워크플로우, zip 파싱, upload/update, 이력) |
| `core/learning_committers/skill_committer.py` | `commit_to_skill` 분기, skill-creator 호출 (실패 시 예외, HTTP 폴백 없음) |
| `core/react_tools.py` | `create_react_tools(agent_id, feedback_content)`, `_commit_skill_tool(..., feedback_content)`, `commit_skill_wrapper` → `commit_to_skill` |
| `core/react_agent.py` | `create_feedback_react_agent(agent_id, feedback_content)`, `process_feedback_with_react` → `feedback_content` 전달. **스킬: 기존 스킬 UPDATE 우선, CREATE/UPDATE/DELETE 시 반드시 `commit_to_skill` 호출 후 Final Answer** (no_commit 방지) |
