import math
import re
import unicodedata

from general.general_tool_audio import(
    clearText, 
    normalize_text
)

SPEECH_TOKENS_PER_SECOND = 25
MAX_SPEECH_TOKENS = 1000


def get_list_word(word: str) -> list:
    # Chuẩn hóa về dạng 'dựng sẵn' để các chữ có dấu không bị tách rời
    word = unicodedata.normalize('NFC', word)
    return list(word)


def _count_spoken_chars(text: str) -> int:
    text = unicodedata.normalize("NFC", text)
    return len(re.sub(r"\s+", "", text))


def _pause_seconds_from_punctuation(text: str) -> float:
    comma_like = len(re.findall(r"[,，;；:：]", text))
    sentence_like = len(re.findall(r"[.!?。！？…]", text))
    return min(3.0, comma_like * 0.12 + sentence_like * 0.30)


def _reading_profile(word_count: int) -> tuple[float, float, float]:
    """Return token_ratio, seconds_per_word, chars_per_second.

    Viterbox's speech tokens are roughly 25 tokens/second. The old fixed
    token budget was too tight for long Vietnamese sentences, which could
    cut the generated audio before the final words. This profile treats the
    token limit as an upper bound and lets T3 stop naturally at EOS.
    """
    if word_count <= 4:
        return 1.20, 0.34, 11.0
    if word_count <= 17:
        return 1.28, 0.29, 13.0
    if word_count <= 35:
        return 2.10, 0.46, 11.5
    return 2.35, 0.48, 12.0


def _budget_guard(word_count: int) -> tuple[float, int]:
    # The guard band lets T3 finish naturally and emit EOS without being cut short.
    # Previously 1.05x + 10 tokens gave ~15 token slack for short/medium units —
    # too much room for garbage tokens → buzz.  With AlignmentStreamAnalyzer active
    # as the primary EOS enforcer, a tighter guard is now safe:
    #   - 1.03x + 5 for ≤17 words → ~9 token slack (was ~15)
    #   - 1.08x + 12 for longer  → ~18 slack (was ~24)
    if word_count <= 17:
        return 1.03, 5
    return 1.08, 12


def getNumberTokenText(content: str, input_token_count: int) -> int:
    # Count sentence boundaries BEFORE clearText strips them.
    # clearText converts '.' → ', ' so _pause_seconds_from_punctuation(clearText(content))
    # always returns sentence_like=0 — causing the budget to miss the 0.30s sentence pause.
    pause_seconds_raw = _pause_seconds_from_punctuation(content)

    # Xử lý text - BUỘC PHẢI CÓ ĐỂ CHUẨN HÓA
    getContent = clearText(content)
    getContent = normalize_text(getContent)

    bunchOfText = getContent.split()
    n = len(bunchOfText)

    if n == 1:
        # sẽ sử dụng cái này nhiều nhất cho advance mode TTS
        # vì cách hoạt động của hàm inference là tách từng chữ ra inference rồi ráp kết quả TTS lại
        getNumber = number_token_for_single_word(n, getContent, input_token_count)
        print(f"\n💎 input_tokens={input_token_count}, words={n}, max_speech_tokens={getNumber}, text={bunchOfText}")
        return getNumber

    spoken_chars = _count_spoken_chars(getContent)
    pause_seconds = pause_seconds_raw  # use pre-clearText value to include sentence pauses
    token_ratio, seconds_per_word, chars_per_second = _reading_profile(n)

    token_based = input_token_count * token_ratio
    word_based = (n * seconds_per_word + pause_seconds) * SPEECH_TOKENS_PER_SECOND
    char_based = ((spoken_chars / chars_per_second) + pause_seconds) * SPEECH_TOKENS_PER_SECOND

    # Keep a small guard band so the model can finish the sentence and emit EOS.
    guard_ratio, guard_tokens = _budget_guard(n)
    adaptive_tokens = math.ceil(max(token_based, word_based, char_based) * guard_ratio + guard_tokens)
    adaptive_tokens = min(adaptive_tokens, MAX_SPEECH_TOKENS)

    print(
        "\n💎 "
        f"input_tokens={input_token_count}, words={n}, chars={spoken_chars}, "
        f"pause_s={pause_seconds:.2f}, max_speech_tokens={adaptive_tokens}, "
        f"text={bunchOfText}"
    )
    return adaptive_tokens
    
    
def number_token_for_single_word(number_of_words: int, 
                                text: str, 
                                input_token_count: int) -> int:
    # Xử lý text - BUỘC PHẢI CÓ ĐỂ CHUẨN HÓA
    text = clearText(text)
    text = normalize_text(text)
    text = text.casefold()  # đảm bảo chữ thường hết

    getNormal = min(int(input_token_count), MAX_SPEECH_TOKENS) # lấy full
    getSpecial = min(int(input_token_count * 0.85), MAX_SPEECH_TOKENS) # chỉ lấy 85% token

    if number_of_words > 1: 
        return getNormal
    
    # ta mạc định text là chỉ có 1 chữ 
    listWord = get_list_word(text) # EX: 'chào' -> ['c', 'h', 'à', 'o']
    listWord_lower = {w.casefold() for w in listWord}  # Ép toàn bộ danh sách về lower

    # Tập hợp các ký tự đặc biệt cần kiểm tra
    special_words = {'ạ', 'ậ', 'ặ', 'ẹ', 'ệ', 'ị', 'ọ', 'ộ', 'ợ', 'ụ', 'ự', 'ỵ'}
    special_words_lower = {w.casefold() for w in special_words}  # Ép toàn bộ danh sách về lower

    is_contant_special_word = any(char in special_words_lower for char in listWord_lower)

    

    if is_contant_special_word:
        print(f"1️⃣ MỘT chữ SPECIAL: {text}, 📝 và các từ của chữ đó: {listWord_lower} \n")
        return getSpecial
    else:
        print(f"1️⃣ MỘT chữ NORMAL: {text}, 📝 và các từ của chữ đó: {listWord_lower} \n")
        return getNormal
