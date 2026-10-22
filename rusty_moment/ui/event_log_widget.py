"""事件日志表格：只记录事件元数据（时间/方式/原因/评分/播放/恢复）。

默认不记录任何语音文本，符合隐私要求；日志条数由配置上限裁剪。
"""

from __future__ import annotations

import time

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.event_manager import EventRecord

TRIGGER_LABELS = {
    "manual": "手动",
    "keyword": "关键词",
    "rule": "规则",
    "ai": "AI",
    "keyword+rule": "关键词+规则",
    "auto": "自动",
}

COLUMNS = ["时间", "触发方式", "触发原因", "评分", "播放", "恢复"]


class EventLogWidget(QWidget):
    def __init__(self, max_entries: int = 200, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._max_entries = max_entries
        self.table = QTableWidget(0, len(COLUMNS), self)
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for col in range(3, len(COLUMNS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.table)

    # -- 数据 --------------------------------------------------------------- #
    def add_record(self, record: EventRecord) -> None:
        row = 0
        self.table.insertRow(row)
        stamp = time.strftime("%H:%M:%S", time.localtime(record.started_at))
        trigger = TRIGGER_LABELS.get(record.trigger_source, record.trigger_source)
        score = "-" if record.score is None else f"{record.score:.2f}"
        played = "未开始" if record.played_ok is None else ("成功" if record.played_ok else "失败")
        if record.restore_ok is None:
            restored = "无需"
        elif record.restore_ok:
            restored = "成功"
        else:
            restored = "失败"

        reason = record.reason
        if record.note:
            reason = f"{reason}（{record.note}）"

        values = [stamp, trigger, reason, score, played, restored]
        for col, text in enumerate(values):
            item = QTableWidgetItem(text)
            item.setToolTip(text)
            if col == 4 and record.played_ok is False:
                item.setForeground(QColor("#ff6b6b"))
            if col == 5 and record.restore_ok is False:
                item.setForeground(QColor("#ff6b6b"))
            self.table.setItem(row, col, item)

        while self.table.rowCount() > self._max_entries:
            self.table.removeRow(self.table.rowCount() - 1)

    def clear(self) -> None:
        self.table.setRowCount(0)

    def set_max_entries(self, max_entries: int) -> None:
        self._max_entries = max(1, int(max_entries))
        while self.table.rowCount() > self._max_entries:
            self.table.removeRow(self.table.rowCount() - 1)

    def records_count(self) -> int:
        return self.table.rowCount()
