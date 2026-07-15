from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from dialogs.common import PrototypeDialog, dialog_header
from mock.data import HISTORY
from ui.components import make_status_badge
from ui.tables import DataTable


class HistoryDialog(PrototypeDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("操作历史", (1000, 650), parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)
        root.addWidget(dialog_header("操作历史", "查看模拟操作记录和文件级明细。"))

        filters = QHBoxLayout()
        group = QButtonGroup(self)
        group.setExclusive(True)
        for index, label in enumerate(["全部", "导入", "重命名", "删除", "歌单", "歌词匹配"]):
            button = QPushButton(label)
            button.setObjectName("FilterButton")
            button.setCheckable(True)
            button.setChecked(index == 0)
            group.addButton(button)
            filters.addWidget(button)
        filters.addStretch(1)
        root.addLayout(filters)

        self.table = DataTable()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["时间", "操作类型", "成功数量", "失败数量", "状态"])
        self.table.setRowCount(len(HISTORY))
        self.table.setColumnWidth(0, 170)
        self.table.horizontalHeader().setSectionResizeMode(1, self.table.horizontalHeader().ResizeMode.Stretch)
        self.table.setColumnWidth(2, 100)
        self.table.setColumnWidth(3, 100)
        self.table.setColumnWidth(4, 130)
        for row, record in enumerate(HISTORY):
            for col, value in enumerate(record):
                item = QTableWidgetItem("" if col == 4 else str(value))
                if col in {2, 3}:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if col == 4:
                    item.setData(Qt.ItemDataRole.UserRole, value)
                    item.setToolTip(str(value))
                self.table.setItem(row, col, item)
            self.table.setCellWidget(row, 4, make_status_badge(record[4]))
        self.table.selectRow(0)
        root.addWidget(self.table, 1)

        detail = QWidget()
        detail.setStyleSheet("background:#fbfbfb;border:1px solid #dedede;border-radius:5px")
        detail_layout = QVBoxLayout(detail)
        detail_title = QLabel("操作明细")
        detail_title.setStyleSheet("font-weight:600;border:0")
        detail_text = QLabel(
            "涉及文件：晴天-周杰伦.mp3 等 10 项\n"
            "原路径：C:\\MusicCtrlDemo\\Downloads\n"
            "目标路径：C:\\MusicCtrlDemo\\Music\\所有音乐\n"
            "结果：9 项成功，1 项失败（目标文件内容冲突）\n"
            "操作时间：2026-07-15 10:42:18"
        )
        detail_text.setStyleSheet("border:0;color:#555")
        detail_layout.addWidget(detail_title)
        detail_layout.addWidget(detail_text)
        root.addWidget(detail)

        footer = QHBoxLayout()
        undo = QPushButton("撤销导入")
        undo.setToolTip("仅最近一次完整导入记录可撤销")
        footer.addWidget(undo)
        footer.addStretch(1)
        close = QPushButton("关闭")
        close.clicked.connect(self.close)
        footer.addWidget(close)
        root.addLayout(footer)
