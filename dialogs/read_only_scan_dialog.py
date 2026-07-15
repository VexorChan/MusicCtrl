"""Explicit read-only scan dialog for building the local SQLite index."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)


class ReadOnlyScanDialog(QDialog):
    start_requested = Signal(object)
    cancel_requested = Signal()

    def __init__(self, remembered_root: Path | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("只读扫描并建立索引")
        self.resize(760, 520)
        self.setMinimumSize(680, 460)
        self._running = False
        self._close_requested = False
        self._row_count = 0

        root = QVBoxLayout(self)
        title = QLabel("只读扫描并建立索引")
        title.setObjectName("DialogTitle")
        root.addWidget(title)
        safety = QLabel(
            "仅枚举所选目录中的音频文件并建立本地索引；不会读取音频内容，"
            "不会移动、重命名、删除文件，也不会计算哈希。"
        )
        safety.setObjectName("Hint")
        safety.setWordWrap(True)
        root.addWidget(safety)

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("扫描文件夹"))
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("请选择要只读扫描的音乐目录")
        if remembered_root is not None:
            self.path_input.setText(str(remembered_root))
        path_row.addWidget(self.path_input, 1)
        self.choose_button = QPushButton("选择…")
        self.choose_button.clicked.connect(self._choose_directory)
        path_row.addWidget(self.choose_button)
        root.addLayout(path_row)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["No.", "名称", "状态"])
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        root.addWidget(self.table, 1)

        self.summary = QLabel("尚未开始扫描。请选择目录后点击“开始扫描”。")
        self.summary.setObjectName("StatusBar")
        root.addWidget(self.summary)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._request_cancel)
        self.start_button = QPushButton("开始扫描")
        self.start_button.setObjectName("PrimaryButton")
        self.start_button.clicked.connect(self._request_start)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.start_button)
        root.addLayout(buttons)

    def _choose_directory(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择只读扫描目录", self.path_input.text())
        if selected:
            self.path_input.setText(selected)

    def _request_start(self) -> None:
        text = self.path_input.text().strip()
        if not text:
            self.summary.setText("请先选择扫描文件夹。")
            return
        path = Path(text)
        if not path.is_absolute():
            self.summary.setText("扫描文件夹必须是绝对路径。")
            return
        self.table.setRowCount(0)
        self._row_count = 0
        self.summary.setText("正在只读扫描并建立索引…")
        self.start_requested.emit(path)

    def _request_cancel(self) -> None:
        if not self._running:
            return
        self.cancel_button.setEnabled(False)
        self.summary.setText("正在协作取消；当前文件系统调用或数据库事务完成后停止…")
        self.cancel_requested.emit()

    def set_running(self, running: bool) -> None:
        self._running = running
        self.path_input.setEnabled(not running)
        self.choose_button.setEnabled(not running)
        self.start_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)

    def add_batch(self, entries: tuple) -> None:
        for entry in entries:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._row_count += 1
            values = (str(self._row_count), entry.relative_path.as_posix(), "已建立索引")
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column != 1:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, column, item)
        self.summary.setText(f"已提交 {self._row_count} 个音频索引。")

    def show_completed(self, count: int) -> None:
        self._show_terminal(f"扫描完成，共提交 {count} 个音频索引。")

    def show_cancelled(self, count: int) -> None:
        self._show_terminal(f"扫描已取消，已安全保留 {count} 个已提交索引。")

    def show_failed(self, message: str) -> None:
        self._show_terminal(f"扫描失败：{message}")

    def show_warning(self, message: str) -> None:
        self.summary.setText(f"提示：{message}")

    def _show_terminal(self, message: str) -> None:
        self.set_running(False)
        self.summary.setText(message)
        if self._close_requested:
            self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._running:
            self._close_requested = True
            self._request_cancel()
            event.ignore()
            return
        super().closeEvent(event)
