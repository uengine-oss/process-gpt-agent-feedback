"""
승인된 SKILL target 적용 중에 일어난 스킬 커밋을 모은다.

스킬 커밋은 Deep Agent가 도구(commit_to_skill)로 호출하므로, 적용을 시작한 쪽
(feedback_batch_manager.apply_approved_proposal)은 무엇이 커밋됐는지 직접 받지 못한다.
적용 태스크 안에서 ContextVar에 목록을 걸어 두면, 그 태스크에서 파생된 도구 호출이
같은 목록에 결과를 남긴다 — asyncio 태스크는 생성 시점의 컨텍스트를 복사하므로 목록
객체 자체는 공유된다.
"""

from contextvars import ContextVar
from typing import Any, Dict, List, Optional

_skill_commit_log: ContextVar[Optional[List[Dict[str, Any]]]] = ContextVar("skill_commit_log", default=None)


def start_skill_commit_log() -> List[Dict[str, Any]]:
    log: List[Dict[str, Any]] = []
    _skill_commit_log.set(log)
    return log


def record_skill_commit(entry: Dict[str, Any]) -> None:
    log = _skill_commit_log.get()
    if log is not None:
        log.append(entry)
