#!/usr/bin/env python3
"""browser-use local agent — fallback cho JS-heavy hoặc bị block.

Chạy hoàn toàn local: Playwright browser + Ollama LLM. Không cần API key.

Cài đặt (một lần):
    pip install browser-use langchain-ollama
    playwright install chromium

Dùng khi nào:
    - fetch_text_for_source() thất bại do site cần JS navigation phức tạp
    - Site cần login bằng tài khoản hợp lệ trước khi đọc nội dung
    - Playwright raw không đủ (cần agent quyết định click gì)

Không dùng cho:
    - CAPTCHA bypass (agent sẽ dừng và báo lỗi)
    - Paid content / ad-gate bypass
"""
from __future__ import annotations

import asyncio
from typing import Any


# ---------------------------------------------------------------------------
# LLM factory — dùng Ollama local
# ---------------------------------------------------------------------------

def _make_ollama_llm(ollama_url: str, model: str) -> Any:
    try:
        from langchain_ollama import ChatOllama
    except ImportError as exc:
        raise SystemExit(
            "Thiếu langchain-ollama. Cài bằng:\n"
            "  pip install langchain-ollama"
        ) from exc
    return ChatOllama(base_url=ollama_url, model=model, temperature=0)


def _ensure_browser_use() -> None:
    try:
        import browser_use  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            "Thiếu browser-use. Cài bằng:\n"
            "  pip install browser-use\n"
            "  playwright install chromium"
        ) from exc


# ---------------------------------------------------------------------------
# Core async API
# ---------------------------------------------------------------------------

async def fetch_chapter_content_async(
    url: str,
    *,
    ollama_url: str = "http://127.0.0.1:11434",
    model: str = "qwen3:14b",
    max_steps: int = 15,
    extra_instruction: str = "",
) -> str:
    """Dùng browser-use agent để lấy nội dung chapter từ URL.

    Agent tự navigate đến URL và trả về phần text chính của chapter.
    Chỉ dùng khi fetch thông thường (requests/Playwright) không hoạt động.

    Args:
        url: URL của chapter cần lấy nội dung
        ollama_url: Địa chỉ Ollama server local
        model: Tên model Ollama (qwen3:14b hoạt động tốt cho navigation)
        max_steps: Số bước tối đa agent được thực hiện
        extra_instruction: Hướng dẫn bổ sung nếu site có flow đặc biệt

    Returns:
        Nội dung text của chapter đã được extract
    """
    _ensure_browser_use()
    from browser_use import Agent

    task = (
        f"Go to this URL: {url}\n"
        "Extract the full chapter story text — all paragraphs of the chapter body. "
        "Do NOT include: navigation menus, ads, comments, headers, footers, "
        "reading settings, or any UI elements. "
        "Return ONLY the story text content.\n"
    )
    if extra_instruction:
        task += f"\nAdditional instructions: {extra_instruction}"

    llm = _make_ollama_llm(ollama_url, model)
    agent = Agent(task=task, llm=llm, max_actions_per_step=max_steps)
    result = await agent.run()
    content = result.final_result() or ""
    if not content:
        raise RuntimeError(f"browser-use agent returned empty result for {url}")
    return content


async def navigate_and_extract_async(
    start_url: str,
    navigation_steps: list[str],
    *,
    ollama_url: str = "http://127.0.0.1:11434",
    model: str = "qwen3:14b",
    max_steps: int = 25,
) -> str:
    """Agent thực hiện multi-step navigation rồi trả về nội dung.

    Dùng cho flow phức tạp: login → navigate → lấy content.

    Args:
        start_url: URL bắt đầu
        navigation_steps: List hướng dẫn từng bước bằng tiếng Anh
        ollama_url: Địa chỉ Ollama server
        model: Tên model Ollama
        max_steps: Số bước tối đa

    Returns:
        Nội dung text được extract sau khi hoàn thành navigation
    """
    _ensure_browser_use()
    from browser_use import Agent

    steps_text = "\n".join(f"{i + 1}. {step}" for i, step in enumerate(navigation_steps))
    task = (
        f"Starting at: {start_url}\n"
        f"Follow these steps:\n{steps_text}\n"
        "After completing all steps, return the main story/chapter text content."
    )

    llm = _make_ollama_llm(ollama_url, model)
    agent = Agent(task=task, llm=llm, max_actions_per_step=max_steps)
    result = await agent.run()
    return result.final_result() or ""


# ---------------------------------------------------------------------------
# Sync wrappers (drop-in cho non-async code)
# ---------------------------------------------------------------------------

def fetch_chapter_content(
    url: str,
    *,
    ollama_url: str = "http://127.0.0.1:11434",
    model: str = "qwen3:14b",
    max_steps: int = 15,
    extra_instruction: str = "",
) -> str:
    """Sync wrapper của fetch_chapter_content_async."""
    return asyncio.run(
        fetch_chapter_content_async(
            url,
            ollama_url=ollama_url,
            model=model,
            max_steps=max_steps,
            extra_instruction=extra_instruction,
        )
    )


def navigate_and_extract(
    start_url: str,
    navigation_steps: list[str],
    *,
    ollama_url: str = "http://127.0.0.1:11434",
    model: str = "qwen3:14b",
    max_steps: int = 25,
) -> str:
    """Sync wrapper của navigate_and_extract_async."""
    return asyncio.run(
        navigate_and_extract_async(
            start_url,
            navigation_steps,
            ollama_url=ollama_url,
            model=model,
            max_steps=max_steps,
        )
    )


# ---------------------------------------------------------------------------
# CLI — test nhanh từ terminal
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Test browser-use agent để lấy nội dung chapter."
    )
    parser.add_argument("url", help="URL của chapter cần lấy nội dung")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="qwen3:14b")
    parser.add_argument("--max-steps", type=int, default=15)
    parser.add_argument("--extra-instruction", default="")
    args = parser.parse_args()

    print(f"[browser-use] fetching: {args.url}", flush=True)
    content = fetch_chapter_content(
        args.url,
        ollama_url=args.ollama_url,
        model=args.model,
        max_steps=args.max_steps,
        extra_instruction=args.extra_instruction,
    )
    print(f"[browser-use] chars={len(content)}")
    print("---")
    print(content[:2000])
    if len(content) > 2000:
        print(f"... (truncated, total {len(content)} chars)")


if __name__ == "__main__":
    main()
