"""
Local LLM helper (no external llm_factory dependency).

We intentionally avoid `llm_factory` and build ChatOpenAI directly.
This also hardens against transient streaming/SSE transport errors like:
  httpx.RemoteProtocolError: incomplete chunked read

Key point: LangChain agents may use `.astream()` internally even when `streaming=False`.
Setting `disable_streaming=True` forces the underlying model to not use streaming transport.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple, Union
import os

TimeoutType = Union[float, Tuple[float, float]]


def get_llm_model(default: str = "gpt-4o") -> str:
    """
    Resolve the LLM model name from environment.

    Priority:
    - LLM_MODEL (preferred)
    - OPENAI_MODEL (fallback)
    - default (when unset/blank)
    """
    model = (os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL") or "").strip()
    return model or default


def create_llm(
    model: Optional[str] = None,
    streaming: bool = False,  # kept for compatibility; we always disable transport streaming
    temperature: float = 0.0,
    timeout: Optional[TimeoutType] = (10.0, 120.0),  # connect, read
    max_retries: int = 6,
):
    """
    Standard ChatOpenAI constructor wrapper used across the project.
    """
    from langchain_openai import ChatOpenAI

    base_url = os.getenv("LLM_PROXY_URL", "http://litellm-proxy:4000")
    api_key = os.getenv("LLM_PROXY_API_KEY") or os.getenv("OPENAI_API_KEY", "")
    resolved_model = model or get_llm_model(default="gpt-4o")

    return ChatOpenAI(
        base_url=base_url,
        api_key=api_key,
        model=resolved_model,
        temperature=temperature,
        streaming=False,
        disable_streaming=True,
        timeout=timeout,
        max_retries=max_retries,
    )


