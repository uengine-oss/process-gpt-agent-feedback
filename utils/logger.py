# ============================================================================
# 간단한 로깅 시스템 - 에러와 일반 로그만
# ============================================================================

import traceback
import sys


def _safe_print(prefix: str, message: str) -> None:
    """
    Windows 콘솔(cp949 등) 환경에서도 깨지지 않도록 안전하게 출력.
    - 이모지 등 인코딩이 안 되는 문자는 제거/대체합니다.
    """
    text = f"{prefix} {message}" if prefix else message
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        # 인코딩 불가 문자를 대체 문자로 바꿔서 다시 출력
        safe_text = text.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
            sys.stdout.encoding or "utf-8", errors="replace"
        )
        print(safe_text, flush=True)


def log(message: str) -> None:
    """일반 로그"""
    _safe_print("LOG:", message)


def handle_error(operation: str, error: Exception, raise_exception: bool = False) -> None:
    """에러 처리
    
    Args:
        operation: 작업 이름
        error: 발생한 예외
        raise_exception: True이면 예외를 다시 발생시킴 (기본값: False)
    """
    _safe_print("ERROR:", f"[{operation}] 오류: {str(error)}")
    _safe_print("ERROR:", f"상세: {traceback.format_exc()}")
    if raise_exception:
        raise Exception(f"{operation} 실패: {error}")