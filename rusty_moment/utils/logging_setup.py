"""logging 初始化与敏感信息脱敏。

规则：日志里不得出现 OBS 密码、API Key。所有 handler 之前挂一个
:class:`RedactionFilter`，把形如 ``password=xxx`` / ``api_key: yyy`` / ``Bearer zzz``
的内容替换掉，即使调用方手滑写了明文也不会落盘。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
from pathlib import Path

REDACTED = "***REDACTED***"

# 覆盖常见写法：password=xxx / "api_key": "xxx" / Bearer xxx / sk-xxxx
_PATTERNS = [
    re.compile(
        r"(?i)(password|passwd|pwd|token|api[_-]?key|secret)(\"?\s*[:=]\s*\"?)([^\s,;\"'}]+)"
    ),
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)(\S+)"),
    re.compile(r"(sk-[A-Za-z0-9]{8,})"),
]


class RedactionFilter(logging.Filter):
    """替换日志中的凭据类文本。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - 格式化异常不应吞日志
            return True
        redacted = redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def redact(text: str) -> str:
    if not text:
        return text
    out = text
    out = _PATTERNS[0].sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)
    out = _PATTERNS[1].sub(lambda m: f"{m.group(1)}{REDACTED}", out)
    out = _PATTERNS[2].sub(REDACTED, out)
    return out


def default_log_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "RustyMoment" / "logs"


def log_file_path(log_dir: Path | None = None) -> Path:
    return (log_dir or default_log_dir()) / "rusty_moment.log"


def setup_logging(
    level: str = "INFO",
    log_dir: Path | None = None,
    console: bool = True,
) -> Path:
    """配置根 logger，返回日志文件路径。重复调用是安全的（会先清空 handler）。"""
    target_dir = Path(log_dir) if log_dir else default_log_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / "rusty_moment.log"

    root = logging.getLogger()
    for handler in list(root.handlers):
        handler.close()
        root.removeHandler(handler)

    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    redaction = RedactionFilter()

    file_handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(redaction)
    root.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        stream.addFilter(redaction)
        root.addHandler(stream)

    # 第三方库降噪
    logging.getLogger("websocket").setLevel(logging.WARNING)
    return path
