"""把词库热词拼成 whisper 的 initial_prompt。

语音识别对专有表达（「别管我」「替我报仇」）经常输出同音字，把它们放进
whisper 的提示词能显著提高命中率，且完全不增加运行时延迟与费用。
"""

from __future__ import annotations

from .models import PhraseLibrary

BASE_PROMPT = "以下是普通话的游戏语音。"
"""与 STTConfig.initial_prompt 的默认值保持一致。"""

MAX_HINT_CHARS = 180
"""whisper 的 initial_prompt 建议控制在 224 token 内，中文按 180 字保守取。"""


def build_stt_hint(library: PhraseLibrary, *, limit: int = 40, max_chars: int = MAX_HINT_CHARS) -> str:
    """从词库挑高频短表达，拼成一句提示词。

    挑选规则：优先短词条（keyword）与高权重词条，跳过排除类与仅整活类，
    避免把「我外卖到了」这种词混进提示里干扰正常识别。
    """
    candidates: list[tuple[float, int, str]] = []
    for entry in library.entries:
        if not entry.enabled or entry.category == "exclusion":
            continue
        if entry.modes == ["chaos"]:
            continue
        phrase = entry.canonical_phrase.strip()
        if not phrase or len(phrase) > 10:
            continue
        # 短关键词优先，其次权重高的
        priority = (1.0 if entry.match_type == "keyword" else 0.0) + entry.weight
        candidates.append((priority, -len(phrase), phrase))

    candidates.sort(reverse=True)
    picked: list[str] = []
    total = 0
    for _priority, _neg_len, phrase in candidates[:limit]:
        if phrase in picked:
            continue
        if total + len(phrase) + 1 > max_chars:
            break
        picked.append(phrase)
        total += len(phrase) + 1

    if not picked:
        return BASE_PROMPT
    return f"{BASE_PROMPT}可能出现的词：{'、'.join(picked)}。"


def merge_prompt(base: str, hint: str) -> str:
    """把用户自定义的 initial_prompt 与热词提示合并（去重、限长）。

    默认的「以下是普通话的句子。」这类通用引导会被热词提示整句替换，
    避免拼接出两句重复的开场白。
    """
    base = (base or "").strip()
    hint = (hint or "").strip()
    if not hint:
        return base or BASE_PROMPT
    if not base or base.startswith("以下是普通话"):
        return hint
    if "可能出现的词" in base:
        return base
    return f"{base}{hint}"
