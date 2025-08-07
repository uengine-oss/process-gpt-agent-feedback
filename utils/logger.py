# ============================================================================
# 간단한 로깅 시스템 - 에러와 일반 로그만
# ============================================================================

import traceback

def log(message: str) -> None:
    """일반 로그"""
    print(f"📝 {message}", flush=True)

def handle_error(operation: str, error: Exception) -> None:
    """에러 처리"""
    print(f"❌ [{operation}] 오류: {str(error)}", flush=True)
    print(f"❌ 상세: {traceback.format_exc()}", flush=True)
    raise Exception(f"{operation} 실패: {error}")