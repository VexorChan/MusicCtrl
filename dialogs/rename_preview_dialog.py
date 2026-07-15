from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QLabel, QTableWidgetItem, QVBoxLayout, QWidget

from dialogs.common import PrototypeDialog, dialog_header, footer_buttons
from mock.data import RENAME_ROWS
from ui.components import make_status_badge
from ui.tables import DataTable


class RenamePreviewDialog(PrototypeDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("重命名预览", (980, 620), parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)
        root.addWidget(dialog_header("重命名预览", "只检查尚未完成规范化标记的音乐文件；执行前始终显示完整预览。"))

        self.table = DataTable()
        self.table.setEditTriggers(self.table.EditTrigger.DoubleClicked | self.table.EditTrigger.EditKeyPressed)
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["", "原文件名", "建议文件名", "识别来源", "状态"])
        self.table.setRowCount(len(RENAME_ROWS))
        self.table.setColumnWidth(0, 42)
        self.table.horizontalHeader().setSectionResizeMode(1, self.table.horizontalHeader().ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, self.table.horizontalHeader().ResizeMode.Stretch)
        self.table.setColumnWidth(3, 160)
        self.table.setColumnWidth(4, 140)
        for row, (checked, original, suggested, source, status) in enumerate(RENAME_ROWS):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, check)
            self.table.setItem(row, 1, QTableWidgetItem(original))
            suggestion = QTableWidgetItem(suggested)
            if suggested == "—":
                suggestion.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, 2, suggestion)
            self.table.setItem(row, 3, QTableWidgetItem(source))
            status_item = QTableWidgetItem()
            status_item.setData(Qt.ItemDataRole.UserRole, status)
            status_item.setToolTip(status)
            self.table.setItem(row, 4, status_item)
            self.table.setCellWidget(row, 4, make_status_badge(status))
        root.addWidget(self.table, 1)

        id3 = QCheckBox("同步修改音频 ID3 中的 Title 和 Artist")
        id3.setChecked(True)
        root.addWidget(id3)
        hint = QLabel("提示：此处仅演示可编辑的建议文件名和选择状态，不会直接批量修改。")
        hint.setObjectName("Hint")
        root.addWidget(hint)
        footer, _primary = footer_buttons(self, "应用重命名")
        root.addWidget(footer)
