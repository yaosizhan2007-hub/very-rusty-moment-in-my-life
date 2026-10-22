"""文本归一化：中文繁转简、大小写、空白。

为什么需要：faster-whisper 等 Whisper 系模型对中文经常输出**繁体**（训练语料里
繁体占比不小），而用户维护的关键词库是简体。「別管我」匹配不到「别管我」会
让关键词层直接失效——这是真机测试里发现的问题，必须在这里兜住。

实现优先用 Windows 自带的 ``LCMapStringW``（LCMAP_SIMPLIFIED_CHINESE），
零依赖且准确；不可用时退回内置的高频繁简映射表。
"""

from __future__ import annotations

import ctypes
import logging
import sys

log = logging.getLogger(__name__)

LCMAP_SIMPLIFIED_CHINESE = 0x02000000

# 兜底映射：覆盖常见悲壮台词与日常高频字（不追求完整，只保关键词命中）
_FALLBACK_MAP = str.maketrans(
    {
        "別": "别", "們": "们", "來": "来", "這": "这", "說": "说", "對": "对",
        "會": "会", "後": "后", "過": "过", "還": "还", "讓": "让", "個": "个",
        "為": "为", "時": "时", "點": "点", "現": "现", "開": "开", "關": "关",
        "覺": "觉", "應": "应", "該": "该", "誰": "谁", "裡": "里", "裏": "里",
        "髮": "发", "衝": "冲", "幾": "几", "從": "从", "帶": "带", "願": "愿",
        "撐": "撑", "儘": "尽", "託": "托", "報": "报", "犧": "牺", "牲": "牲",
        "沒": "没", "戰": "战", "鬥": "斗", "勝": "胜", "敗": "败", "輸": "输",
        "贏": "赢", "軍": "军", "隊": "队", "殺": "杀", "死": "死", "傷": "伤",
        "護": "护", "擋": "挡", "斷": "断", "續": "续", "終": "终", "結": "结",
        "離": "离", "棄": "弃", "逃": "逃", "救": "救", "幫": "帮", "謝": "谢",
        "記": "记", "得": "得", "復": "复", "仇": "仇", "淚": "泪", "哭": "哭",
        "聲": "声", "話": "话", "語": "语", "聽": "听", "見": "见", "嗎": "吗",
        "麼": "么", "為": "为", "無": "无", "軍": "军", "鐵": "铁", "鋼": "钢",
    }
)

_kernel32 = None
_api_ready: bool | None = None


def _win_api():
    global _kernel32, _api_ready
    if _api_ready is not None:
        return _kernel32 if _api_ready else None
    if sys.platform != "win32":  # pragma: no cover - 本项目只跑 Windows
        _api_ready = False
        return None
    try:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LCMapStringW.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPCWSTR,
            ctypes.c_int,
            wintypes.LPWSTR,
            ctypes.c_int,
        ]
        kernel32.LCMapStringW.restype = ctypes.c_int
        _kernel32 = kernel32
        _api_ready = True
    except Exception as exc:  # pragma: no cover - 极端环境
        log.warning("无法使用 LCMapStringW 做繁简转换：%s", exc)
        _kernel32 = None
        _api_ready = False
    return _kernel32


def to_simplified(text: str) -> str:
    """繁体转简体（尽力而为，失败时原样返回）。"""
    if not text:
        return text
    api = _win_api()
    if api is not None:
        try:
            needed = api.LCMapStringW(0x0804, LCMAP_SIMPLIFIED_CHINESE, text, -1, None, 0)
            if needed > 0:
                buffer = ctypes.create_unicode_buffer(needed + 1)
                written = api.LCMapStringW(
                    0x0804, LCMAP_SIMPLIFIED_CHINESE, text, -1, buffer, needed + 1
                )
                if written > 0:
                    return buffer.value
        except Exception:  # pragma: no cover - 走兜底
            log.debug("LCMapStringW 调用失败，使用内置映射", exc_info=True)
    return text.translate(_FALLBACK_MAP)


def normalize_for_matching(text: str, *, simplify: bool = True, lower: bool = False) -> str:
    """匹配前的归一化：去首尾空白、可选繁转简与大小写折叠。"""
    result = (text or "").strip()
    if simplify:
        result = to_simplified(result)
    # 全角标点统一成半角，避免「别管我，」类边界差异
    result = (
        result.replace("，", ",")
        .replace("。", ".")
        .replace("！", "!")
        .replace("？", "?")
        .replace("、", ",")
        .replace("：", ":")
        .replace("；", ";")
        .replace("　", " ")
    )
    if lower:
        result = result.lower()
    return result
