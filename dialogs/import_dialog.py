from __future__ import annotations

from PySide6.QtCore import Qt
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
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("导入", (900, 620), parent)
        self.mode = "audio"
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)
        root.addWidget(dialog_header("导入本地文件", "扫描 Downloads 中的文件并预览整理结果；本原型不会移动任何真实文件。"))

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

    def _choose(self, line: QLineEdit) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择文件夹", line.text())
        if path:
            line.setText(path.replace("/", "\\"))

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.audio_button.setChecked(mode == "audio")
        self.lyrics_button.setChecked(mode == "lyrics")
        rows = IMPORT_AUDIO if mode == "audio" else IMPORT_LYRICS
        self.target_path.setText(
            r"C:\MusicCtrlDemo\Music\所有音乐" if mode == "audio" else r"C:\MusicCtrlDemo\Music\歌词"
        )
        self.mode_hint.setText("音频模式 · 仅显示 MP3 / FLAC / WAV / M4A / OGG / AAC" if mode == "audio" else "歌词模式 · 仅显示 LRC")
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
