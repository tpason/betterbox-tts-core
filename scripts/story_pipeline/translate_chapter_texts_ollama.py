#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any

import requests

from genre_prompts import (
    apply_aliases,
    clean_source_noise,
    get_translate_genre_addendum,
    infer_genre_from_char_map,
    filter_char_map_for_text,
    inject_char_map_into_system,
    inject_genre_into_system,
    load_char_map,
    parse_aliases,
)
from story_memory import (
    apply_story_memory_replacements,
    build_story_memory_prompt,
    find_story_memory_quality_issues,
    load_story_memory,
    story_memory_status,
)


CHAPTER_PATTERN = re.compile(r"chapter(\d+)\.txt$", re.IGNORECASE)

QWEN_SYSTEM_PROMPT = """Bạn là dịch giả chuyên nghiệp cho truyện audio tiếng Việt, nhiều thể loại: tiên hiệp, huyền huyễn, hệ thống Trung Quốc, fantasy phương Tây, Korean light novel, hiện đại.

Nhiệm vụ: dịch truyện tiếng Trung, tiếng Hàn hoặc tiếng Anh sang tiếng Việt — đọc như văn xuôi sáng tác của người Việt, không phải dịch máy. Ưu tiên nghĩa tự nhiên trong ngữ cảnh, không dịch từng chữ.

Quy tắc nội dung:
- Giữ đầy đủ nội dung, không thêm tình tiết, không lược bỏ.
- Giữ tên riêng, địa danh, cảnh giới, vật phẩm, công pháp nhất quán.
- Giữ lời thoại trong dấu ngoặc kép.
- Dòng hệ thống trong 【 】 dịch ngắn gọn, rõ nghĩa.
- Không giải thích, không markdown, không tiêu đề, không ghi chú; chỉ trả về bản tiếng Việt.

Nghĩa và văn phong — ưu tiên rõ nghĩa trước:
- Câu tối nghĩa hoặc idiom trong nguyên bản: dịch nghĩa hợp lý nhất trong ngữ cảnh — không dịch literal để ra câu tối nghĩa tiếng Việt. Câu tối nghĩa trong nguyên bản KHÔNG có nghĩa là phải ra câu tối nghĩa trong bản dịch.
- Khi nguyên bản dùng idiom, thành ngữ, hoặc câu văn đặc thù văn hóa nguồn: diễn giải thành câu tiếng Việt rõ ràng giữ đúng ý nghĩa; không giữ cấu trúc dịch máy cứng nhắc.
- Câu văn mạch lạc, có nhịp điệu, đọc mượt khi TTS; không dính liền, không cụt lủn.
- Thêm dấu câu hợp lý để tạo nhịp nghỉ tự nhiên cho người nghe.
- Xen kẽ câu dài tả cảnh và câu ngắn gọn trong cảnh hành động hoặc cảm xúc cao trào.
- Ưu tiên câu chủ động, động từ cụ thể mạnh hơn câu bị động khi không thay đổi nghĩa.
- Giữ sắc thái biểu cảm của nguyên bản: cảm thán, hào hứng, khinh thường, lo âu — không làm phẳng cảm xúc.
- Dùng từ tượng hình, tượng thanh, tượng cảnh nếu phù hợp với nội dung gốc.
- Tả cảnh: giữ chi tiết màu sắc, âm thanh, cảm giác vật lý nếu có trong nguyên bản; dùng tính từ và động từ cụ thể.
- Hành động: câu ngắn, nhịp nhanh, động từ mạnh.
- Nội tâm nhân vật: nhẹ nhàng, giữ giọng điệu và cảm xúc nhân vật.

Nhân vật và nhất quán giọng văn (bắt buộc):
- Nếu có character map (inject phía trên): TUÂN THỦ TUYỆT ĐỐI ngôi thứ ba, cách tự xưng, tính cách và giọng thoại từng nhân vật — đây là ưu tiên cao hơn mọi quy tắc chung.
- Giọng thoại từng nhân vật phải nhất quán: nhân vật lạnh lùng dùng câu ngắn, không cảm thán; nhân vật kiệm lời không đột nhiên giải thích dài dòng.
- Xác định giới tính nhân vật từ ngữ cảnh (tên, đại từ gốc 他/她/그녀/he/she, chức danh, quan hệ) trước khi chọn đại từ.
- Nhân vật nam không có trong map: "hắn", "y", "gã", "lão" tùy tuổi và sắc thái.
- Nhân vật nữ không có trong map: "nàng", "cô", "bà" tùy tuổi — tuyệt đối không dùng "hắn" cho nhân vật nữ.
- Nhân vật trẻ (thiếu niên, trẻ nhỏ): "cậu", "nó" theo giới tính — không tự xưng "ông"/"bà"/"lão" nếu nguyên bản không có.
- Giữ nhất quán đại từ cho từng nhân vật trong toàn đoạn.
- Lời thoại tự xưng giữ theo nguyên bản (ta, tôi, tại hạ, bổn tọa...); không thay đổi nếu không có cơ sở rõ ràng.

Xưng hô trong lời thoại — ngôi 1 và ngôi 2 (tiếng Việt đặc thù — ĐÂY LÀ LỖI CỰC PHỔ BIẾN KHI DỊCH MÁY):
Tiếng Việt xưng hô phụ thuộc QUAN HỆ + TUỔI + QUYỀN LỰC + CẢM XÚC. Không thể dịch cứng "I → tôi" hay "you → anh/bạn".

Ngôi 1 — tự xưng theo ngữ cảnh:
- Trung tính / lịch sự: "tôi"
- Thân mật cùng lứa ngang cấp: "mình", "tớ"; "tao" CHỈ khi thực sự thân hoặc đang đối đầu kẻ thù ngang tầm
- Kiêu ngạo / quyền lực / phản diện mạnh / đang đe dọa: "ta", "lão tử", "bổn tọa" — KHÔNG "tôi" lúc này
- Khiêm tốn / cấp dưới: "tại hạ", "đệ tử", "tiểu nhân"
- Người nhỏ hơn nói với người lớn hơn: "em", "con", "cháu" — KHÔNG "tao"

Ngôi 2 — gọi người đối diện theo ngữ cảnh:
- Người nhỏ → người lớn hơn: "anh", "chị", "chú", "bác", "thầy", "ngài" — KHÔNG "mày/ngươi/mi"
- Người lớn → người nhỏ hơn: "cậu", "em", "con", "cháu", "nhóc"
- Ngang hàng / thân thiết: "cậu", "bạn", "anh/chị" tùy tuổi
- Kẻ thù / thù địch / coi thường trong cảnh đối đầu: "ngươi", "mi" cho tiên hiệp/kiếm hiệp/cổ phong Trung Quốc; "mày", "tên kia" hoặc lược đại từ cho fantasy phương Tây/Korean LN/hiện đại — TUYỆT ĐỐI KHÔNG "anh/bạn/cậu" với kẻ thù
- Tôn kính / thần phục: "ngài", "tiền bối", "đại nhân", "sư phụ"
- Khinh thường: "thằng kia", "con kia", "tiểu tử", "lão già"

Quy tắc cứng không được vi phạm:
1. Kẻ thù đang đối đầu: tự xưng "ta/lão tử" trong cổ phong hoặc "tao" trong hiện đại/western; gọi đối phương "ngươi/mi" cho tiên hiệp/cổ phong, "mày/tên kia" cho fantasy phương Tây/Korean LN/hiện đại — KHÔNG bao giờ "anh/bạn/cậu"
2. Nhân vật nhỏ tuổi gặp người lớn hơn: "em/con" tự xưng, gọi người lớn "anh/chú/bác" — KHÔNG "mày/tao" với người lớn hơn
3. Cùng nhân vật xưng hô khác nhau tùy đối tượng: với đồng đội trẻ là "cậu"; với cấp trên là "ngài/tiền bối"; kẻ thù nói với họ là "ngươi" hoặc "mày" tùy bối cảnh."""

QWEN_USER_TEMPLATE = """Dịch đoạn sau sang tiếng Việt tự nhiên, mượt mà như văn xuôi sáng tác để đọc audio.
Nếu có character map phía trên: TUÂN THỦ TUYỆT ĐỐI xưng hô và giọng thoại từng nhân vật trong map.
Chỉ trả về bản dịch tiếng Việt; không ghi chú, không giải thích.

{text}"""

QWEN_USER_WITH_CONTEXT_TEMPLATE = """Dịch đoạn sau sang tiếng Việt tự nhiên, mượt mà như văn xuôi sáng tác để đọc audio.
Nếu có character map phía trên: TUÂN THỦ TUYỆT ĐỐI xưng hô và giọng thoại từng nhân vật trong map.

Ngữ cảnh — phần kết của đoạn liền trước (CHỈ để tham khảo giọng văn, xưng hô và mạch truyện, KHÔNG dịch lại):
---
{preceding_context}
---

Đoạn cần dịch:
{text}"""

VIETNAMESE_ADDRESS_POLICY_EN = """Vietnamese dialogue pronouns are mandatory and context-sensitive.
Do not translate English/Korean/Chinese "I/you" mechanically as "tôi/anh/bạn".
Choose first-person and second-person address from relationship + relative age + power + emotion + scene hostility.

First-person:
- Neutral or polite: "tôi".
- Close peers: "mình", "tớ".
- "tao" only for real intimacy or hostile same-level street/gang contexts; never toward elders/superiors.
- Arrogant, powerful, threatening, villainous speakers: "ta", "lão tử", "bổn tọa" in xianxia/wuxia; avoid "tôi" in open threats.
- Subordinates/disciples: "tại hạ", "đệ tử", "tiểu nhân" where appropriate.
- Younger speaker to older listener: "em", "con", "cháu"; never "tao".

Second-person:
- Younger -> older: "anh", "chị", "chú", "bác", "thầy", "ngài"; never "mày/ngươi/mi".
- Older -> younger: "cậu", "em", "con", "cháu", "nhóc".
- Equal/close: "cậu", "bạn", "anh/chị" depending on age and tone.
- Enemy, attacker, gang member, villain, hostile stranger, or contemptuous confrontation: use "ngươi", "mi", "tên kia" for xianxia/wuxia/archaic Chinese tone; use "mày", "tên kia", or omit the pronoun for Western/Korean/modern tone. NEVER call an enemy or hostile stranger "anh", "bạn", or friendly "cậu".
- Respect/submission: "ngài", "tiền bối", "đại nhân", "sư phụ", "lãnh chúa".
- Contempt: "thằng kia", "con kia", "tiểu tử", "lão già".

Hard rules:
1. In a hostile encounter, attackers/villains must not politely call the protagonist "anh/bạn/cậu".
2. A younger character addressing an older/respected person must not use "mày/tao" unless the source explicitly shows hostile defiance.
3. The same character may address different people differently: allies, elders, superiors, family, strangers, and enemies do not share one fixed pronoun pair.
4. If a character map includes per-target addressing rules, those override all general rules."""

TRANSLATEGEMMA_TEMPLATE = """You are a professional literary translator: Chinese/Korean/English → Vietnamese for audiobook narration. Your primary goal is natural, literary Vietnamese prose that reads as if written by a Vietnamese author — never a mechanical or word-for-word translation.

{priority_context}

Rules:
- Produce only the Vietnamese translation. No notes, no explanations, no commentary.
- Keep all content faithfully. Do not add or remove plot, dialogue, or details.
- MEANING OVER LITERALISM: When the source text is ambiguous, uses idioms, or has culture-specific expressions, translate the most natural and clear meaning in context. An ambiguous or awkward source sentence does NOT justify an ambiguous or unclear Vietnamese sentence — resolve it into the most sensible interpretation.
- Write flowing Vietnamese prose that sounds like a published Vietnamese novel. Avoid all machine-translation patterns: do not copy source sentence structure into Vietnamese when it produces unnatural output.
- If a character map is provided above: STRICTLY follow each character's third-person pronouns, self-address, per-target addressing rules, and speech style as specified — this overrides all general pronoun rules.
- Character voice consistency: a cold/terse character should not suddenly speak long emotional sentences; a reserved character should not explain things in detail.
- Third-person pronouns for characters not in the map must follow the story register. Chinese/xianxia/wuxia/archaic tone may use "hắn", "y", "gã", "lão" for male and "nàng", "cô", "bà" for female. Western/Korean/modern tone should prefer character names, "anh ta", "cô ta", "cậu", "cô", or "anh" as natural narration. Never use "hắn" for a female character.
- Young characters (teens/children) should not self-address as "ong/ba/lao" unless the original clearly has it.
- Sentence rhythm: alternate longer descriptive sentences with shorter punchy ones in action or emotional peaks.
- Active voice preferred; strong, specific verbs.
- Descriptive scenes: preserve sensory details (color, sound, physical sensation) from the original.
- Action scenes: short sentences, fast rhythm, strong verbs.
- Inner monologue: soft tone, preserve the character's emotional voice.
- Use Vietnamese onomatopoeia and vivid imagery where it fits the content naturally.
- Keep dialogue in double quotes. Keep character voice and tone.
- Keep proper nouns and place names consistent with any character map provided; do not invent romanizations.

Please translate the following text into Vietnamese:

{text}"""

TRANSLATEGEMMA_WITH_CONTEXT_TEMPLATE = """You are a professional literary translator: Chinese/Korean/English → Vietnamese for audiobook narration. Your primary goal is natural, literary Vietnamese prose that reads as if written by a Vietnamese author — never a mechanical or word-for-word translation.

{priority_context}

Rules:
- Produce only the Vietnamese translation. No notes, no explanations, no commentary.
- Keep all content faithfully. Do not add or remove plot, dialogue, or details.
- MEANING OVER LITERALISM: When the source text is ambiguous, uses idioms, or has culture-specific expressions, translate the most natural and clear meaning in context. An ambiguous source sentence does NOT justify an ambiguous Vietnamese output — resolve it into the most sensible interpretation.
- Continue the style, pronouns, character voice, and story flow from the preceding context.
- If a character map is provided above: STRICTLY follow each character's third-person pronouns, self-address, per-target addressing rules, and speech style as specified.
- Character voice consistency: keep each character's tone and word choice stable throughout.
- Write flowing Vietnamese prose; avoid machine-translation sentence structure patterns.

Preceding context for continuity only, do not translate it again:
---
{preceding_context}
---

Please translate the following text into Vietnamese:

{text}"""


def chapter_number(path: Path) -> int:
    match = CHAPTER_PATTERN.match(path.name)
    return int(match.group(1)) if match else 0


def list_chapter_files(input_dir: Path) -> list[Path]:
    return sorted(
        [path for path in input_dir.glob("chapter*.txt") if CHAPTER_PATTERN.match(path.name)],
        key=chapter_number,
    )


def split_text(text: str, max_chars: int) -> list[str]:
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""

        sentences = re.split(r"(?<=[。！？；;.!?])", paragraph)
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            candidate = f"{current}{sentence}" if current else sentence
            if len(candidate) <= max_chars:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = sentence

    if current:
        chunks.append(current)
    return chunks


def clean_model_output(text: str) -> str:
    text = text.strip()
    # Strip Qwen3/DeepSeek thinking blocks nếu Ollama chưa lọc.
    text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"^```(?:text|markdown)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    text = re.sub(r"^\s*(?:Bản dịch|Bản tiếng Việt|Văn bản đã dịch)\s*:\s*", "", text, flags=re.IGNORECASE)
    return text.strip()


HOSTILE_CONTEXT_RE = re.compile(
    r"(?:kẻ tấn công|tên cướp|tên trộm|bang hội|phản diện|kẻ thù|thù địch|"
    r"giết|xông lên|rút kiếm|dao găm|đe dọa|phục kích|attacker|thief|gang|"
    r"enemy|hostile|ambush|kill him|kill her|rushed|dagger)",
    re.IGNORECASE,
)
POLITE_HOSTILE_DIALOGUE_RE = re.compile(
    r'"(?:\s*(?:Này,?\s*)?(?:Anh|anh|Bạn|bạn|Cậu|cậu)\b[^"\n]{0,100}[?!.]?|'
    r'[^"\n]{1,24},\s*(?:anh|bạn|cậu)\b[^"\n]{0,100}[?!.]?)"'
)
RUDE_TO_ELDER_RE = re.compile(
    r"(?:đứa trẻ|cậu bé|cô bé|thiếu niên|nhân viên trẻ|child|boy|girl|young staff)"
    r"[\s\S]{0,180}"
    r'"[^"\n]{0,80}\b(?:mày|tao)\b[^"\n]{0,100}"',
    re.IGNORECASE,
)


def output_too_short(source_text: str, output_text: str, min_ratio: float = 0.55) -> bool:
    """True nếu output ngắn bất thường so với input — dấu hiệu dịch bị bỏ sót nội dung."""
    if min_ratio <= 0:
        return False
    source_len = len(re.sub(r"\s+", "", source_text or ""))
    output_len = len(re.sub(r"\s+", "", output_text or ""))
    if source_len < 300:
        return False
    return output_len < source_len * min_ratio


def find_addressing_quality_issues(text: str) -> list[str]:
    """Best-effort warnings for Vietnamese address choices that are often wrong."""
    issues: list[str] = []
    if not text:
        return issues
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    for idx, paragraph in enumerate(paragraphs, start=1):
        nearby = "\n".join(paragraphs[max(0, idx - 2): min(len(paragraphs), idx + 1)])
        if HOSTILE_CONTEXT_RE.search(nearby):
            for match in POLITE_HOSTILE_DIALOGUE_RE.finditer(paragraph):
                issues.append(
                    f"paragraph {idx}: hostile context may use polite/friendly address: {match.group(0)[:140]}"
                )
        if RUDE_TO_ELDER_RE.search(paragraph):
            issues.append(f"paragraph {idx}: young speaker may be using mày/tao toward an older/respected listener")
    return issues


def warn_addressing_quality(output_text: str, label: str) -> None:
    issues = find_addressing_quality_issues(output_text)
    for issue in issues[:8]:
        print(f"[QUALITY WARN] {label}: {issue}")
    if len(issues) > 8:
        print(f"[QUALITY WARN] {label}: {len(issues) - 8} more possible addressing issue(s)")


def build_messages(
    model: str,
    text: str,
    genre: str = "",
    char_map: str = "",
    preceding_context: str = "",
    story_memory_context: str = "",
    no_think: bool = True,
) -> list[dict[str, str]]:
    focused_char_map = filter_char_map_for_text(
        char_map,
        f"{preceding_context}\n\n{text}".strip(),
    ) if char_map else ""
    if model.startswith("translategemma"):
        addendum_en = get_translate_genre_addendum(genre, for_english_model=True)
        template = TRANSLATEGEMMA_WITH_CONTEXT_TEMPLATE if preceding_context else TRANSLATEGEMMA_TEMPLATE
        priority_parts = [f"Vietnamese address policy, highest priority:\n{VIETNAMESE_ADDRESS_POLICY_EN}"]
        if addendum_en:
            priority_parts.append(f"Genre rules:\n{addendum_en}")
        if focused_char_map:
            priority_parts.append(f"Character map and voice rules, absolute highest priority:\n{focused_char_map}")
        if story_memory_context:
            priority_parts.append(
                "Story memory, role bible, glossary, title/nickname policies, absolute highest priority:\n"
                f"{story_memory_context}"
            )
        content = template.format(
            priority_context="\n\n".join(priority_parts),
            preceding_context=preceding_context,
            text=text,
        )
        return [{"role": "user", "content": content}]
    system = inject_genre_into_system(
        QWEN_SYSTEM_PROMPT,
        get_translate_genre_addendum(genre),
    )
    system = inject_char_map_into_system(system, focused_char_map)
    if story_memory_context:
        system += (
            "\n\n"
            "══════ STORY MEMORY / ROLE BIBLE / GLOSSARY (ƯU TIÊN CAO) ══════\n"
            "Các quy tắc sau đặc thù cho truyện, nhân vật, role đại chúng, biệt danh và thuật ngữ. "
            "Chúng ghi đè quy tắc chung nếu mâu thuẫn:\n"
            f"{story_memory_context}"
        )
    user_content = (
        QWEN_USER_WITH_CONTEXT_TEMPLATE.format(preceding_context=preceding_context, text=text)
        if preceding_context
        else QWEN_USER_TEMPLATE.format(text=text)
    )
    if no_think and "qwen3" in model.lower():
        user_content = "/no_think\n\n" + user_content
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]


def call_ollama(
    base_url: str,
    model: str,
    text: str,
    temperature: float,
    num_ctx: int,
    timeout: int,
    retries: int,
    keep_alive: str,
    genre: str = "",
    char_map: str = "",
    preceding_context: str = "",
    story_memory_context: str = "",
    session: requests.Session | None = None,
    no_think: bool = True,
) -> str:
    url = base_url.rstrip("/") + "/api/chat"
    payload: dict[str, Any] = {
        "model": model,
        "stream": False,
        "messages": build_messages(model, text, genre, char_map, preceding_context, story_memory_context, no_think=no_think),
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
        },
        "keep_alive": keep_alive,
    }

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            client = session or requests
            response = client.post(url, json=payload, timeout=timeout)
            if response.status_code >= 400:
                raise RuntimeError(
                    f"HTTP {response.status_code} from Ollama {url}: {response.text[:1000]}"
                )
            data = response.json()
            content = data.get("message", {}).get("content", "")
            if not content.strip():
                raise ValueError(f"Ollama trả về rỗng: {json.dumps(data, ensure_ascii=False)[:500]}")
            return clean_model_output(content)
        except Exception as exc:
            last_error = exc
            print(f"[WARN] Ollama error attempt {attempt}/{retries}: {exc}")
            if attempt < retries:
                time.sleep(2)
    raise RuntimeError(f"Ollama failed after {retries} retries: {last_error}")


def _tail_context(text: str, max_chars: int = 1200) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= max_chars:
        return text
    return text[-max_chars:].lstrip()


def translate_file(input_path: Path, output_path: Path, args: argparse.Namespace) -> None:
    raw_text = clean_source_noise(input_path.read_text(encoding="utf-8")).strip()
    if not raw_text:
        print(f"[SKIP] File rỗng: {input_path}")
        return

    char_map_file = getattr(args, "char_map_file", "")
    char_map = load_char_map(char_map_file)
    genre = getattr(args, "genre", "") or infer_genre_from_char_map(char_map)
    story_id = str(getattr(args, "story_id", "") or "")
    story_slug = str(getattr(args, "story_slug", "") or input_path.parent.name)
    story_memory = load_story_memory(
        story_memory_dir=getattr(args, "story_memory_dir", ""),
        story_id=story_id,
        slug=story_slug,
        char_map_file=char_map_file,
    )
    aliases = parse_aliases(char_map) if char_map else {}
    if aliases:
        normalized = apply_aliases(raw_text, aliases)
        if normalized != raw_text:
            print(f"[ALIAS] Đã chuẩn hóa {sum(1 for k in aliases if k in raw_text.lower())} alias(es)")
            raw_text = normalized
    memory_normalized = apply_story_memory_replacements(raw_text, story_memory)
    if memory_normalized != raw_text:
        print("[STORY_MEMORY] Đã chuẩn hóa tên/thuật ngữ theo story memory")
        raw_text = memory_normalized

    chunks = split_text(raw_text, args.max_chars_per_chunk)
    char_map_note = f", char_map={'yes' if char_map else 'no'}"
    print(
        f"\n=== {input_path.name}: {len(raw_text)} chars -> {len(chunks)} chunks, "
        f"genre={genre or 'default'}{char_map_note}, {story_memory_status(story_memory)} ==="
    )

    translated_chunks: list[str] = []
    preceding_context = ""
    with requests.Session() as session:
        for idx, chunk in enumerate(chunks, start=1):
            print(f"[{idx}/{len(chunks)}] Translate {len(chunk)} chars with {args.model}" + (f" +ctx={len(preceding_context)}c" if preceding_context else ""))
            story_memory_context = build_story_memory_prompt(
                story_memory,
                f"{preceding_context}\n\n{chunk}".strip(),
                genre=genre,
            )
            translated = call_ollama(
                base_url=args.ollama_url,
                model=args.model,
                text=chunk,
                temperature=args.temperature,
                num_ctx=args.num_ctx,
                timeout=args.timeout,
                retries=args.retries,
                keep_alive=getattr(args, "keep_alive", "30m"),
                genre=genre,
                char_map=char_map,
                preceding_context=preceding_context,
                story_memory_context=story_memory_context,
                session=session,
            )
            translated = apply_story_memory_replacements(translated, story_memory)
            if output_too_short(chunk, translated):
                src_len = len(re.sub(r"\s+", "", chunk))
                out_len = len(re.sub(r"\s+", "", translated))
                print(
                    f"[QUALITY WARN] translate chunk {idx}/{len(chunks)}: "
                    f"output ({out_len} chars) < 55% of input ({src_len} chars) — "
                    f"có thể bị bỏ sót nội dung"
                )
            translated_chunks.append(translated)
            preceding_context = _tail_context(translated)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    final_text = apply_story_memory_replacements("\n\n".join(translated_chunks).strip(), story_memory)
    warn_addressing_quality(final_text, input_path.name)
    memory_issues = find_story_memory_quality_issues(final_text, story_memory, genre=genre)
    if memory_issues:
        print(f"[STORY_MEMORY WARN] {input_path.name}: {len(memory_issues)} issue(s)")
        for issue in memory_issues[:12]:
            print(f"  - {issue}")
        if len(memory_issues) > 12:
            print(f"  - ... {len(memory_issues) - 12} more")
        if getattr(args, "fail_on_story_memory_issues", False):
            raise RuntimeError(f"Story memory QA failed for {input_path.name}: {memory_issues[:5]}")
    output_path.write_text(final_text + "\n", encoding="utf-8")
    print(f"Đã lưu: {output_path}")


# ---------------------------------------------------------------------------
# Single-pass translate + polish (EN → polished VI in one Ollama call)
# ---------------------------------------------------------------------------

# VI output is typically 1.3–1.6× EN input in character count.
# Below 0.80 of source (non-whitespace) suggests omitted content.
_EN_TO_VI_MIN_RATIO = 0.80

SINGLE_PASS_SYSTEM_PROMPT = """You are a professional literary translator and audiobook editor for Vietnamese production.

Your task: translate the English passage to Vietnamese, applying literary polish in the same pass — output only the final polished Vietnamese text ready for text-to-speech.

Priority (highest to lowest): story memory / role bible > character map > genre rules > general rules below.

Translation rules (mandatory):
- Translate COMPLETELY. Never summarize, never omit sentences, paragraphs, dialogue, or system notifications.
- Output only the Vietnamese text — no notes, no explanations, no markdown, no headings, no editor remarks.
- If a character map is injected above: STRICTLY follow each character's pronouns, self-address, per-target address, personality, and speech style. These override all general pronoun rules.

Vietnamese address pronouns — context-sensitive, never translate I/you mechanically:
{address_policy}

Polish and style rules (apply while translating):
- Write natural Vietnamese literary prose — not machine-translated, not word-for-word.
- Meaning over literalism: resolve ambiguous or idiomatic English into natural Vietnamese phrasing.
- Preserve sensory details, action rhythm, and emotional tone from the source.
- Action scenes: short sentences, strong verbs, fast rhythm.
- Dialogue: keep double quotes, preserve each character's voice and register.
- TTS ready: no separators (---, ***), no markdown, no annotation lines. Convert numbers and percentages to Vietnamese reading form.
- The preceding Vietnamese context below is for style and flow continuity only — do not translate it again.

Genre vocabulary rules will be injected below if applicable."""


def build_single_pass_messages(
    text: str,
    genre: str = "",
    char_map: str = "",
    preceding_vi_context: str = "",
    story_memory_context: str = "",
    no_think: bool = True,
) -> list[dict[str, str]]:
    focused_char_map = filter_char_map_for_text(
        char_map,
        f"{preceding_vi_context}\n\n{text}".strip(),
    ) if char_map else ""
    system = SINGLE_PASS_SYSTEM_PROMPT.format(address_policy=VIETNAMESE_ADDRESS_POLICY_EN)
    system = inject_genre_into_system(system, get_translate_genre_addendum(genre))
    system = inject_char_map_into_system(system, focused_char_map)
    if story_memory_context:
        system += (
            "\n\n"
            "══════ STORY MEMORY / ROLE BIBLE / GLOSSARY (HIGHEST PRIORITY) ══════\n"
            "Story-specific rules — override all general rules if there is a conflict:\n"
            f"{story_memory_context}"
        )
    if preceding_vi_context:
        user_content = (
            "[PRECEDING VIETNAMESE CONTEXT — style reference only, do not translate]\n"
            "---\n"
            f"{preceding_vi_context}\n"
            "---\n\n"
            "[ENGLISH PASSAGE TO TRANSLATE AND POLISH]\n"
            f"{text}"
        )
    else:
        user_content = f"[ENGLISH PASSAGE TO TRANSLATE AND POLISH]\n{text}"
    if no_think:
        user_content = "/no_think\n\n" + user_content
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]


def _single_pass_call(
    session: requests.Session,
    url: str,
    payload: dict[str, Any],
    timeout: int,
    retries: int,
) -> tuple[str, int]:
    """Returns (output_text, prompt_eval_count). Raises RuntimeError after retries."""
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = session.post(url, json=payload, timeout=timeout)
            if response.status_code >= 400:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
            data = response.json()
            content = data.get("message", {}).get("content", "")
            if not content.strip():
                raise ValueError(f"Ollama trả về rỗng: {json.dumps(data)[:300]}")
            return clean_model_output(content), int(data.get("prompt_eval_count") or 0)
        except Exception as exc:
            last_error = exc
            print(f"[WARN] Single-pass Ollama error attempt {attempt}/{retries}: {exc}")
            if attempt < retries:
                time.sleep(2)
    raise RuntimeError(f"Single-pass failed after {retries} retries: {last_error}")


def _single_pass_call_with_fallback(
    session: requests.Session,
    url: str,
    payload: dict[str, Any],
    source_chunk: str,
    chunk_idx: int,
    total_chunks: int,
    args: argparse.Namespace,
    genre: str,
    char_map: str,
    preceding_vi_context: str,
    story_memory_context: str,
    num_ctx: int,
) -> str:
    output, prompt_tokens = _single_pass_call(session, url, payload, args.timeout, args.retries)

    src_nws = len(re.sub(r"\s+", "", source_chunk))
    out_nws = len(re.sub(r"\s+", "", output))
    if prompt_tokens:
        ctx_pct = int(prompt_tokens / num_ctx * 100)
        print(f"[TOKENS] chunk {chunk_idx}/{total_chunks}: prompt_eval={prompt_tokens}/{num_ctx} ({ctx_pct}%)")
        if ctx_pct >= 80:
            print(f"[WARN] prompt_tokens >= 80% of num_ctx={num_ctx} — consider --single-pass-num-ctx")

    if out_nws >= src_nws * _EN_TO_VI_MIN_RATIO:
        return output

    print(
        f"[QUALITY WARN] Single-pass chunk {chunk_idx}/{total_chunks}: "
        f"output ({out_nws} nws-chars) < {int(_EN_TO_VI_MIN_RATIO * 100)}% of source ({src_nws}) — "
        f"retrying with half-chunk split"
    )
    # Split at paragraph boundary near halfway; never return raw English.
    half = len(source_chunk) // 2
    split_pos = source_chunk.rfind("\n\n", 0, half + 200)
    if split_pos <= 0:
        split_pos = half
    sub_chunks = [c.strip() for c in [source_chunk[:split_pos], source_chunk[split_pos:]] if c.strip()]

    sub_outputs: list[str] = []
    sub_preceding = preceding_vi_context
    for sub_chunk in sub_chunks:
        sub_messages = build_single_pass_messages(
            text=sub_chunk,
            genre=genre,
            char_map=char_map,
            preceding_vi_context=sub_preceding,
            story_memory_context=story_memory_context,
            no_think=True,
        )
        sub_payload = {**payload, "messages": sub_messages}
        sub_out, _ = _single_pass_call(session, url, sub_payload, args.timeout, args.retries)
        sub_nws = len(re.sub(r"\s+", "", sub_out))
        sub_src_nws = len(re.sub(r"\s+", "", sub_chunk))
        if sub_nws < sub_src_nws * _EN_TO_VI_MIN_RATIO:
            raise RuntimeError(
                f"Single-pass sub-chunk output too short after retry "
                f"({sub_nws}/{sub_src_nws} nws-chars, threshold={_EN_TO_VI_MIN_RATIO}) — "
                f"job will be requeued with smaller chunk size"
            )
        sub_outputs.append(sub_out)
        sub_preceding = _tail_context(sub_out)

    return "\n\n".join(sub_outputs)


def single_pass_translate_polish_file(
    input_path: Path,
    output_path: Path,
    args: argparse.Namespace,
) -> None:
    from polish_chapter_texts_ollama import clean_for_audiobook_tts

    raw_text = clean_source_noise(input_path.read_text(encoding="utf-8")).strip()
    if not raw_text:
        print(f"[SKIP] File rỗng: {input_path}")
        return

    char_map_file = getattr(args, "char_map_file", "")
    char_map = load_char_map(char_map_file)
    genre = getattr(args, "genre", "") or infer_genre_from_char_map(char_map)
    story_id = str(getattr(args, "story_id", "") or "")
    story_slug = str(getattr(args, "story_slug", "") or input_path.parent.name)
    story_memory = load_story_memory(
        story_memory_dir=getattr(args, "story_memory_dir", ""),
        story_id=story_id,
        slug=story_slug,
        char_map_file=char_map_file,
    )
    aliases = parse_aliases(char_map) if char_map else {}
    if aliases:
        normalized = apply_aliases(raw_text, aliases)
        if normalized != raw_text:
            print(f"[ALIAS] Đã chuẩn hóa {sum(1 for k in aliases if k in raw_text.lower())} alias(es)")
            raw_text = normalized
    memory_normalized = apply_story_memory_replacements(raw_text, story_memory)
    if memory_normalized != raw_text:
        print("[STORY_MEMORY] Đã chuẩn hóa tên/thuật ngữ theo story memory")
        raw_text = memory_normalized

    max_chars = getattr(args, "max_chars_per_chunk", 2000)
    num_ctx = getattr(args, "num_ctx", 6144)
    chunks = split_text(raw_text, max_chars)

    char_map_note = f", char_map={'yes' if char_map else 'no'}"
    print(
        f"\n=== {input_path.name}: {len(raw_text)} chars -> {len(chunks)} chunks [single-pass], "
        f"genre={genre or 'default'}{char_map_note}, {story_memory_status(story_memory)} ==="
    )

    polished_chunks: list[str] = []
    preceding_vi_context = ""
    url = args.ollama_url.rstrip("/") + "/api/chat"

    with requests.Session() as session:
        for idx, chunk in enumerate(chunks, start=1):
            ctx_note = f" +ctx={len(preceding_vi_context)}c" if preceding_vi_context else ""
            print(f"[{idx}/{len(chunks)}] Single-pass {len(chunk)} chars{ctx_note}")
            story_memory_context = build_story_memory_prompt(
                story_memory,
                f"{preceding_vi_context}\n\n{chunk}".strip(),
                genre=genre,
            )
            messages = build_single_pass_messages(
                text=chunk,
                genre=genre,
                char_map=char_map,
                preceding_vi_context=preceding_vi_context,
                story_memory_context=story_memory_context,
                no_think=True,
            )
            payload_data: dict[str, Any] = {
                "model": args.model,
                "stream": False,
                "messages": messages,
                "options": {"temperature": args.temperature, "num_ctx": num_ctx},
                "keep_alive": getattr(args, "keep_alive", "30m"),
            }
            output = _single_pass_call_with_fallback(
                session=session,
                url=url,
                payload=payload_data,
                source_chunk=chunk,
                chunk_idx=idx,
                total_chunks=len(chunks),
                args=args,
                genre=genre,
                char_map=char_map,
                preceding_vi_context=preceding_vi_context,
                story_memory_context=story_memory_context,
                num_ctx=num_ctx,
            )
            output = apply_story_memory_replacements(output, story_memory)
            polished_chunks.append(output)
            preceding_vi_context = _tail_context(output)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    final_text = apply_story_memory_replacements(
        clean_for_audiobook_tts("\n\n".join(polished_chunks).strip()),
        story_memory,
    )
    warn_addressing_quality(final_text, input_path.name)
    memory_issues = find_story_memory_quality_issues(final_text, story_memory, genre=genre)
    if memory_issues:
        print(f"[STORY_MEMORY WARN] {input_path.name}: {len(memory_issues)} issue(s)")
        for issue in memory_issues[:12]:
            print(f"  - {issue}")
        if len(memory_issues) > 12:
            print(f"  - ... {len(memory_issues) - 12} more")
        if getattr(args, "fail_on_story_memory_issues", False):
            raise RuntimeError(f"Story memory QA failed for {input_path.name}: {memory_issues[:5]}")
    output_path.write_text(final_text + "\n", encoding="utf-8")
    print(f"Đã lưu (single-pass): {output_path}")


def main() -> None:
    # Tip tốc độ: OLLAMA_FLASH_ATTENTION=1 ollama serve  →  giảm 20-40% inference time.
    parser = argparse.ArgumentParser(description="Dịch chapter text sang tiếng Việt bằng Ollama.")
    parser.add_argument("--input-dir", required=True, help="Folder chứa chapterX.txt tiếng Trung/raw.")
    parser.add_argument("--output-root", default="story_data/translated")
    parser.add_argument("--output-dir", default="", help="Ghi trực tiếp vào thư mục này, bỏ qua --output-root/input-dir.name.")
    parser.add_argument("--chapter", type=int, default=0, help="0 nghĩa là dùng --all hoặc mặc định chapter1.")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="qwen3:14b")
    parser.add_argument("--temperature", type=float, default=0.2)
    # 4096 vừa đủ cho chunk 2500 ký tự Trung/Hàn + system prompt + output Vietnamese.
    # Tăng lên 6144-8192 nếu gặp output bị cắt ngắn với ngôn ngữ verbose (Hàn, Anh).
    parser.add_argument("--num-ctx", type=int, default=4096)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--keep-alive", default="30m")
    parser.add_argument("--max-chars-per-chunk", type=int, default=2500)
    parser.add_argument(
        "--genre",
        default="",
        help="Thể loại truyện: tien_hiep, huyen_huyen, he_thong, kiem_hiep, do_thi, xuyen_khong, mat_the, vong_du, lang_man, western_fantasy. Để trống để dùng prompt mặc định.",
    )
    parser.add_argument(
        "--char-map-file",
        default="",
        help="File nhân vật inject vào prompt dịch/polish. VD: story_data/char_maps/21180-vinh-thoai-hiep-si.txt",
    )
    parser.add_argument(
        "--story-memory-dir",
        default="",
        help=(
            "Root story memory hoặc thư mục memory cụ thể. Nếu bỏ trống, script tự tìm theo "
            "story_data/story_memory/{story_id}-{slug} từ story id/char-map/tên folder input."
        ),
    )
    parser.add_argument(
        "--fail-on-story-memory-issues",
        action="store_true",
        help="Nếu story memory QA phát hiện lỗi tên/thuật ngữ/register, dừng thay vì chỉ cảnh báo.",
    )
    args = parser.parse_args()

    input_dir = Path(args.input_dir).resolve()
    if not input_dir.is_dir():
        raise SystemExit(f"Không tìm thấy input-dir: {input_dir}")

    if args.all:
        input_files = list_chapter_files(input_dir)
    else:
        chapter_num = args.chapter or 1
        input_files = [input_dir / f"chapter{chapter_num}.txt"]

    input_files = [path for path in input_files if path.exists()]
    if not input_files:
        raise SystemExit("Không có chapter file để dịch.")

    output_dir = Path(args.output_dir) if args.output_dir else Path(args.output_root) / input_dir.name
    for input_path in input_files:
        output_path = output_dir / input_path.name
        if output_path.exists() and not args.overwrite:
            print(f"[SKIP] Đã tồn tại: {output_path}")
            continue
        try:
            translate_file(input_path, output_path, args)
        except Exception as exc:
            print(f"[ERROR] {input_path.name}: {exc}")

    print(f"\nHoàn tất. Text đã dịch nằm trong: {output_dir}")


if __name__ == "__main__":
    main()
