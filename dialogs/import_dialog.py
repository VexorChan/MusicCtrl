from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from dialogs.common import PrototypeDialog, dialog_header
from mock.data import IMPORT_AUDIO, IMPORT_LYRICS
from ui.components import make_status_badge
from ui.tables import DataTable


class ImportDialog(PrototypeDialog):
    start_requested = Signal(object, object, str)
    cancel_requested = Signal()

    def __init__(self, parent: QWidget | None = None, *, live_mode: bool = False) -> None:
        super().__init__("导入", (900, 620), parent)
        self.mode = "audio"
        self.live_mode = bool(live_mode)
        self._running = False
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)
        subtitle = (
            "源文件会先复制到临时目标，完成大小和 SHA-256 校验后才安全移动；同名文件绝不覆盖。"
            if live_mode
            else "扫描 Downloads 中的文件并预览整理结果；本原型不会移动任何真实文件。"
        )
        root.addWidget(dialog_header("导入本地文件", subtitle))

        actions = QHBoxLayout()
        self.audio_button = QPushButton("扫描音频")
        self.lyrics_button = QPushButton("扫描歌词")
        for button in (self.audio_button, self.lyrics_button):
            button.setCheckable(True)
        group = QButtonGroup(self)
        group.setExclusive(True)
        group.addButton(self.audio_button)
        group.addButton(self.lyrics_button)
        self.audio_button.setChecked(True)
        self.audio_button.clicked.connect(lambda: self.set_mode("audio"))
        self.lyrics_button.clicked.connect(lambda: self.set_mode("lyrics"))
        self.move_button = QPushButton("移动")
        self.move_button.setObjectName("PrimaryButton")
        self.move_button.setEnabled(True)
        if live_mode:
            self.move_button.clicked.connect(self._request_start)
        actions.addWidget(self.audio_button)
        actions.addWidget(self.lyrics_button)
        actions.addStretch(1)
        actions.addWidget(self.move_button)
        root.addLayout(actions)

        self.mode_hint = QLabel()
        self.mode_hint.setObjectName("Hint")
        root.addWidget(self.mode_hint)

        self.table = DataTable()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["No.", "名称", "状态"])
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.setColumnWidth(0, 68)
        self.table.horizontalHeader().setSectionResizeMode(1, self.table.horizontalHeader().ResizeMode.Stretch)
        self.table.setColumnWidth(2, 180)
        root.addWidget(self.table, 1)

        paths = QGridLayout()
        paths.setHorizontalSpacing(8)
        paths.setVerticalSpacing(8)
        scan_label = QLabel("扫描文件夹")
        target_label = QLabel("目标文件夹")
        self.scan_path = QLineEdit(r"C:\MusicCtrlDemo\Downloads")
        self.target_path = QLineEdit(r"C:\MusicCtrlDemo\Music\所有音乐")
        scan_choose = QPushButton("选择")
        target_choose = QPushButton("选择")
        scan_choose.clicked.connect(lambda: self._choose(self.scan_path))
        target_choose.clicked.connect(lambda: self._choose(self.target_path))
        paths.addWidget(scan_label, 0, 0)
        paths.addWidget(self.scan_path, 0, 1)
        paths.addWidget(scan_choose, 0, 2)
        paths.addWidget(target_label, 1, 0)
        paths.addWidget(self.target_path, 1, 1)
        paths.addWidget(target_choose, 1, 2)
        paths.setColumnStretch(1, 1)
        root.addLayout(paths)

        footer = QHBoxLayout()
        self.summary = QLabel()
        self.summary.setObjectName("StatusBar")
        self.summary.setFixedHeight(32)
        footer.addWidget(self.summary, 1)
        close = QPushButton("关闭")
        close.clicked.connect(self.close)
        footer.addWidget(close)
        root.addLayout(footer)
        self.set_mode("audio")
        if live_mode:
            self.scan_path.clear()
            self.target_path.clear()
            self.table.setRowCount(0)
            self.summary.setText("请选择源目录和目标目录，然后开始安全导入。")

    def _choose(self, line: QLineEdit) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择文件夹", line.text())
        if path:
            line.setText(path.replace("/", "\\"))

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.audio_button.setChecked(mode == "audio")
        self.lyrics_button.setChecked(mode == "lyrics")
        rows = IMPORT_AUDIO if mode == "audio" else IMPORT_LYRICS
        if not self.live_mode:
            self.target_path.setText(
                r"C:\MusicCtrlDemo\Music\所有音乐" if mode == "audio" else r"C:\MusicCtrlDemo\Music\歌词"
            )
        self.mode_hint.setText("音频模式 · 仅显示 MP3 / FLAC / WAV / M4A / OGG / AAC" if mode == "audio" else "歌词模式 · 仅显示 LRC")
        if self.live_mode:
            self.table.setRowCount(0)
            self.summary.setText("请选择源目录和目标目录，然后开始安全导入。")
            return
        self.table.setRowCount(len(rows))
        for row, (name, status) in enumerate(rows):
            number = QTableWidgetItem(str(row + 1))
            number.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 0, number)
            self.table.setItem(row, 1, QTableWidgetItem(name))
            status_item = QTableWidgetItem()
            status_item.setData(Qt.ItemDataRole.UserRole, status)
            status_item.setToolTip(status)
            self.table.setItem(row, 2, status_item)
            self.table.setCellWidget(row, 2, make_status_badge(status))
        if mode == "audio":
            self.summary.setText("已选择 12 项    ·    可移动 9 项    ·    冲突 2 项    ·    重复 1 项")
        else:
            self.summary.setText("已选择 6 项    ·    可移动 3 项    ·    冲突 2 项    ·    重复 1 项")

    def _request_start(self) -> None:
        source = Path(self.scan_path.text().strip())
        target = Path(self.target_path.text().strip())
        if not source.is_absolute() or not target.is_absolute():
            self.summary.setText("源目录和目标目录都必须选择绝对路径。")
            return
        self.start_requested.emit(source, target, self.mode)

    def set_running(self, running: bool) -> None:
        self._running = bool(running)
        self.move_button.setText("导入中" if running else "移动")
        self.move_button.setEnabled(not running)
        self.audio_button.setEnabled(not running)
        self.lyrics_button.setEnabled(not running)
        self.scan_path.setEnabled(not running)
        self.target_path.setEnabled(not running)
        if running:
            self.summary.setText("正在校验并安全导入；关闭窗口会先协作取消。")

    def show_result(self, result: object, *, cancelled: bool = False) -> None:
        items = tuple(getattr(result, "items", ()))
        self.table.setRowCount(len(items))
        labels = {"success": "已导入", "duplicate": "重复", "conflict": "冲突", "failed": "失败"}
        for row, item in enumerate(items):
            number = QTableWidgetItem(str(row + 1))
            number.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 0, number)
            self.table.setItem(row, 1, QTableWidgetItem(item.source_path.name))
            status = labels.get(item.status, item.status)
            status_item = QTableWidgetItem(status)
            status_item.setToolTip(item.message)
            self.table.setItem(row, 2, status_item)
        prefix = "已取消" if cancelled else "已完成"
        self.summary.setText(
            f"{prefix} · 成功 {getattr(result, 'success_count', 0)} · "
            f"重复 {getattr(result, 'duplicate_count', 0)} · "
            f"冲突 {getattr(result, 'conflict_count', 0)} · "
            f"失败 {getattr(result, 'failure_count', 0)}"
        )

    def show_failed(self, message: str) -> None:
        self.summary.setText(f"导入失败：{message}")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._running:
            self.cancel_requested.emit()
            event.ignore()
            return
        super().closeEvent(event)
