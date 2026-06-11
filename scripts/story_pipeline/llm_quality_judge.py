#!/usr/bin/env python3
"""LLM-based sampled semantic QA cho bản dịch/polish.

Đây là tầng QA cuối bổ sung cho deterministic checks (check_translation_quality):
chấm các lỗi mà regex không bắt được — dịch word-for-word, sai nghĩa, văn cứng,
đại từ không nhất quán. KHÔNG phải chapter-level omission proof: judge chỉ sample
vài window; omission tổng thể do length-ratio/structure checks đảm nhiệm.

Library use:
    result = judge_chapter_quality(source_text, polished_text, genre=..., ...)
    result.issues  → ["judge:word_for_word", ...] (chỉ severity=major)
    result.verdicts → chi tiết evidence để log/debug

CLI use (qua check_translation_quality.py --llm-judge).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

import requests

# Issue codes mà judge có thể trả — map sang repair hints trong issue_to_repair_hint.
JUDGE_ISSUE_TYPES = frozenset({
    "word_for_word",      # dịch bám từng chữ, câu Việt cứng/ngược cú pháp
    "omission",           # window output thiếu ý so với source
    "mistranslation",     # sai nghĩa
    "wrong_pronoun",      # xưng hô sai/không nhất quán trong window
    "unnatural",          # văn không tự nhiên (không hẳn word-for-word)
})

JUDGE_PROMPT = """/no_think
Bạn là biên tập viên QA bản dịch truyện (nguồn → tiếng Việt) cho audiobook.
Dưới đây là {n} cặp đoạn trích: SOURCE (nguyên bản) và OUTPUT (bản dịch tiếng Việt đã polish).
Các cặp được cắt theo vị trí tương đối nên biên có thể lệch vài câu — chỉ đánh giá phần nội dung trùng nhau, KHÔNG báo omission chỉ vì biên cắt lệch.

Tìm các lỗi sau trong OUTPUT:
- word_for_word: dịch bám từng chữ — cú pháp tiếng Việt cứng, ngược, nghe như dịch máy.
- omission: cả câu/ý quan trọng trong SOURCE bị mất hẳn ở phần nội dung trùng.
- mistranslation: dịch sai nghĩa so với SOURCE.
- wrong_pronoun: xưng hô/đại từ sai hoặc đổi bất nhất trong cùng đoạn.
- unnatural: câu tiếng Việt lủng củng, không tự nhiên (dù không hẳn word-for-word).

severity: "major" = người nghe nhận ra ngay / sai nghĩa; "minor" = nhỏ, chấp nhận được.

Trả về DUY NHẤT một JSON object, không markdown, không giải thích:
{{"verdicts": [{{"issue": "<loại>", "severity": "minor|major", "evidence": "<trích ngắn từ OUTPUT>", "window": <số>}}]}}
Nếu không có lỗi: {{"verdicts": []}}

{windows}"""


@dataclass
class JudgeResult:
    verdicts: list[dict] = field(default_factory=list)
    error: str = ""

    @property
    def issues(self) -> list[str]:
        """Issue codes cho quality gate — chỉ severity=major."""
        out: list[str] = []
        for v in self.verdicts:
            issue = str(v.get("issue") or "").strip()
            if issue in JUDGE_ISSUE_TYPES and str(v.get("severity") or "") == "major":
                code = f"judge:{issue}"
                if code not in out:
                    out.append(code)
        return out

    @property
    def warnings(self) -> list[str]:
        out: list[str] = []
        for v in self.verdicts:
            issue = str(v.get("issue") or "").strip()
            if issue in JUDGE_ISSUE_TYPES and str(v.get("severity") or "") != "major":
                code = f"judge_minor:{issue}"
                if code not in out:
                    out.append(code)
        if self.error:
            out.append(f"judge_error:{self.error}")
        return out


def _rotating_offsets(n_windows: int, seed: str, attempt: int = 0) -> list[float]:
    """Vị trí tương đối (0..1) của các window — rotate deterministic theo
    hash(seed, attempt) để re-scan/retry phủ vùng khác nhau."""
    h = int(hashlib.sha256(f"{seed}:{attempt}".encode()).hexdigest()[:8], 16)
    base = (h % 1000) / 1000.0 / max(n_windows, 1)  # jitter trong 1/n đầu tiên
    return [min(base + i / n_windows, 0.98) for i in range(n_windows)]


def _window_at(text: str, rel_pos: float, width: int) -> str:
    """Cắt window ~width chars tại vị trí tương đối, mở rộng về boundary từ."""
    if len(text) <= width:
        return text
    start = int(rel_pos * max(len(text) - width, 0))
    chunk = text[start:start + width]
    # Bỏ từ/câu cụt ở hai đầu cho dễ đọc
    if start > 0:
        chunk = chunk.split(" ", 1)[-1]
    return chunk.strip()


def build_judge_prompt(
    source_text: str,
    polished_text: str,
    *,
    seed: str = "",
    attempt: int = 0,
    n_windows: int = 3,
    window_chars: int = 700,
) -> str:
    offsets = _rotating_offsets(n_windows, seed or str(len(polished_text)), attempt)
    blocks: list[str] = []
    for i, pos in enumerate(offsets, start=1):
        src_w = _window_at(source_text, pos, window_chars)
        out_w = _window_at(polished_text, pos, window_chars)
        blocks.append(f"=== CẶP {i} ===\nSOURCE:\n{src_w}\n\nOUTPUT:\n{out_w}")
    return JUDGE_PROMPT.format(n=len(offsets), windows="\n\n".join(blocks))


def _parse_judge_json(content: str) -> list[dict]:
    """Parse JSON từ model output — chịu được markdown fence và text thừa."""
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    # Lấy object JSON ngoài cùng nếu model in thêm chữ
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not m:
        raise ValueError("no JSON object in judge output")
    data = json.loads(m.group(0))
    verdicts = data.get("verdicts")
    if not isinstance(verdicts, list):
        raise ValueError("judge output missing 'verdicts' list")
    return [v for v in verdicts if isinstance(v, dict)]


def judge_chapter_quality(
    source_text: str,
    polished_text: str,
    *,
    genre: str = "",
    ollama_url: str = "http://127.0.0.1:11434",
    model: str = "qwen3:14b",
    num_ctx: int = 8192,
    timeout: int = 300,
    seed: str = "",
    attempt: int = 0,
    n_windows: int = 3,
    window_chars: int = 700,
    session: requests.Session | None = None,
) -> JudgeResult:
    """Sampled semantic QA — 1 Ollama call. Lỗi judge KHÔNG raise: trả
    JudgeResult(error=...) để caller log warning, không chặn pipeline."""
    if not source_text.strip() or not polished_text.strip():
        return JudgeResult(error="empty_input")
    prompt = build_judge_prompt(
        source_text,
        polished_text,
        seed=seed,
        attempt=attempt,
        n_windows=n_windows,
        window_chars=window_chars,
    )
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0, "num_ctx": num_ctx},
        "keep_alive": "30m",
    }
    try:
        http = session or requests
        response = http.post(f"{ollama_url.rstrip('/')}/api/chat", json=payload, timeout=timeout)
        response.raise_for_status()
        content = response.json().get("message", {}).get("content", "")
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
        verdicts = _parse_judge_json(content)
        return JudgeResult(verdicts=verdicts)
    except Exception as exc:  # noqa: BLE001 — judge là enhancement, không chặn pipeline
        return JudgeResult(error=type(exc).__name__)
