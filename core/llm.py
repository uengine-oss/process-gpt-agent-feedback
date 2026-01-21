"""
Local LLM helper (no external llm_factory dependency).

We intentionally avoid `llm_factory` and build ChatOpenAI directly.
This also hardens against transient streaming/SSE transport errors like:
  httpx.RemoteProtocolError: incomplete chunked read

Key point: LangChain agents may use `.astream()` internally even when `streaming=False`.
Setting `disable_streaming=True` forces the underlying model to not use streaming transport.
"""

from __future__ import annotations

from typing import Optional, Tuple, Union


TimeoutType = Union[float, Tuple[float, float]]


def create_llm(
    model: str = "gpt-4o",
    streaming: bool = False,  # kept for compatibility; we always disable transport streaming
    temperature: float = 0.0,
    timeout: Optional[TimeoutType] = (10.0, 120.0),  # connect, read
    max_retries: int = 6,
):
    """
    Standard ChatOpenAI constructor wrapper used across the project.
    """
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=model,
        temperature=temperature,
        streaming=False,
        disable_streaming=True,
        timeout=timeout,
        max_retries=max_retries,
    )


