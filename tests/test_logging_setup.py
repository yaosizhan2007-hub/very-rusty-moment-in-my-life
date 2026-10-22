"""日志脱敏测试：密码与 API Key 不得出现在日志文件里。"""

from __future__ import annotations

import logging
from pathlib import Path

from rusty_moment.utils.logging_setup import REDACTED, RedactionFilter, redact, setup_logging


def test_redact_common_patterns():
    assert "hunter2" not in redact("password=hunter2")
    assert REDACTED in redact("password=hunter2")
    assert REDACTED in redact('"api_key": "abc123"')
    assert REDACTED in redact("Authorization: Bearer tokenvalue")
    assert REDACTED in redact("key sk-abcdefgh12345678")


def test_redaction_filter_applies_to_log_file(tmp_path: Path):
    log_path = setup_logging("DEBUG", tmp_path, console=False)
    logger = logging.getLogger("rusty_moment.test")
    logger.info("obs password=SuperSecret123")
    logger.info("api_key: sk-shouldnotappear12345")
    logging.shutdown()

    content = Path(log_path).read_text(encoding="utf-8")
    assert "SuperSecret123" not in content
    assert "sk-shouldnotappear12345" not in content
    assert REDACTED in content


def test_filter_returns_true_and_rewrites_message():
    record = logging.LogRecord(
        name="x", level=logging.INFO, pathname=__file__, lineno=1,
        msg="connect with password=hunter2", args=(), exc_info=None,
    )
    assert RedactionFilter().filter(record) is True
    assert "hunter2" not in record.getMessage()
