from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QCheckBox, QLabel, QTableWidgetItem, QVBoxLayout, QWidget

from dialogs.common import PrototypeDialog, dialog_header, footer_buttons
from mock.data import RENAME_ROWS
from services.metadata_preview import MetadataPreviewResult
from ui.components import make_status_badge
from ui.tables import DataTable


_READ_ONLY_FLAGS = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable


class RenamePreviewDialog(PrototypeDialog):
    cancel_requested = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        live_mode: bool = False,
    ) -> None:
        super().__init__("重命名预览", (980, 620), parent)
        self.live_mode = live_mode
        self._running = False
        self._close_pending = False
        self._results: tuple[MetadataPreviewResult, ...] = ()

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)
        subtitle = (
            "只读分析已选择的索引音乐；建议名称可以编辑，但本阶段不会修改文件名或音频标签。"
            if live_mode
            else "只检查尚未完成规范化标记的音乐文件；执行前始终显示完整预览。"
        )
        root.addWidget(dialog_header("重命名预览", subtitle))

        self.table = DataTable()
        self.table.setEditTriggers(self.table.EditTrigger.DoubleClicked | self.table.EditTrigger.EditKeyPressed)
        root.addWidget(self.table, 1)

        self.id3_checkbox = QCheckBox("同步修改音频 ID3 中的 Title 和 Artist")
        self.id3_checkbox.setChecked(True)
        self.id3_checkbox.setVisible(not live_mode)
        root.addWidget(self.id3_checkbox)

        self.summary = QLabel()
        self.summary.setObjectName("Hint")
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)

        primary_text = "完成预览" if live_mode else "应用重命名"
        footer, self.primary_button = footer_buttons(self, primary_text)
        root.addWidget(footer)

        if live_mode:
            self._configure_live_table()
            self.summary.setText("请选择音乐开始只读分析；未执行任何文件写入。")
        else:
            self._populate_mock_rows()
            self.summary.setText("提示：此处仅演示可编辑的建议文件名和选择状态，不会直接批量修改。")

    @staticmethod
    def _read_only_item(text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setFlags(_READ_ONLY_FLAGS)
        return item

    def _populate_mock_rows(self) -> None:
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
            self.table.setItem(row, 1, self._read_only_item(original))
            suggestion = QTableWidgetItem(suggested)
            if suggested == "—":
                suggestion.setFlags(_READ_ONLY_FLAGS)
            self.table.setItem(row, 2, suggestion)
            self.table.setItem(row, 3, self._read_only_item(source))
            status_item = self._read_only_item("")
            status_item.setData(Qt.ItemDataRole.UserRole, status)
            status_item.setToolTip(status)
            self.table.setItem(row, 4, status_item)
            self.table.setCellWidget(row, 4, make_status_badge(status))

    def _configure_live_table(self) -> None:
        self.table.clear()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["", "完整源路径", "建议名称", "扩展", "识别来源", "状态"])
        self.table.setRowCount(0)
        self.table.setColumnWidth(0, 42)
        self.table.horizontalHeader().setSectionResizeMode(1, self.table.horizontalHeader().ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, self.table.horizontalHeader().ResizeMode.Stretch)
        self.table.setColumnWidth(3, 72)
        self.table.setColumnWidth(4, 100)
        self.table.setColumnWidth(5, 130)

    def replace_results(self, results: Sequence[MetadataPreviewResult]) -> None:
        if not self.live_mode:
            raise RuntimeError("模拟预览窗口不能注入真实分析结果")
        self._results = tuple(results)
        self.table.setRowCount(len(self._results))
        for row, result in enumerate(self._results):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(
                Qt.CheckState.Checked
                if result.status == "可预览" and not result.requires_confirmation
                else Qt.CheckState.Unchecked
            )
            self.table.setItem(row, 0, check)
            source_item = self._read_only_item(str(result.canonical_path))
            source_item.setToolTip(str(result.canonical_path))
            self.table.setItem(row, 1, source_item)
            suggestion = QTableWidgetItem(result.suggested_stem or "—")
            if result.suggested_stem is None:
                suggestion.setFlags(_READ_ONLY_FLAGS)
            suggestion.setToolTip("只能编辑建议名称；父目录和扩展名保持不变。")
            self.table.setItem(row, 2, suggestion)
            self.table.setItem(row, 3, self._read_only_item(result.extension))
            self.table.setItem(row, 4, self._read_only_item(result.source))
            status_item = self._read_only_item("")
            status_item.setData(Qt.ItemDataRole.UserRole, result.status)
            status_item.setToolTip(result.message)
            self.table.setItem(row, 5, status_item)
            self.table.setCellWidget(row, 5, make_status_badge(result.status))
        self.summary.setText(f"只读分析完成：{len(self._results)} 项。仅预览，未修改任何文件或标签。")

    def show_results(self, results: object) -> None:
        if not isinstance(results, (tuple, list)):
            self.show_failed("分析结果格式无效")
            return
        self.replace_results(results)

    def show_warning(self, message: str) -> None:
        self.summary.setText(message)

    def show_failed(self, message: str) -> None:
        self.summary.setText(f"分析失败：{message}")

    def show_cancelled(self, _count: int = 0) -> None:
        self.summary.setText("已取消只读分析；没有发布残缺预览，也未修改文件。")

    def set_running(self, running: bool) -> None:
        self._running = running
        self.primary_button.setEnabled(not running)
        if running:
            self.summary.setText("正在后台只读分析所选音乐…")
        elif self._close_pending:
            QTimer.singleShot(0, self.close)

    def reject(self) -> None:
        if self.live_mode and self._running:
            self._close_pending = True
            self.cancel_requested.emit()
            return
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.live_mode and self._running:
            self._close_pending = True
            self.cancel_requested.emit()
            event.ignore()
            return
        super().closeEvent(event)
