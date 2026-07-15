from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from dialogs.common import PrototypeDialog, dialog_header
from ui.components import make_status_badge


class LyricsMatchDialog(PrototypeDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("歌词匹配", (1020, 660), parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)
        root.addWidget(dialog_header("歌词匹配", "优先处理高置信度候选；所有匹配操作均为模拟。"))

        toolbar = QHBoxLayout()
        auto = QPushButton("自动匹配高置信度项目")
        auto.setObjectName("PrimaryButton")
        toolbar.addWidget(auto)
        toolbar.addWidget(QPushButton("批量处理"))
        toolbar.addWidget(QPushButton("标记忽略"))
        toolbar.addStretch(1)
        root.addLayout(toolbar)

        tabs = QTabWidget()
        tabs.addTab(self._match_panel(False), "未匹配  6")
        tabs.addTab(self._match_panel(True), "重复与冲突  2")
        root.addWidget(tabs, 1)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close = QPushButton("关闭")
        close.clicked.connect(self.close)
        close_row.addWidget(close)
        root.addLayout(close_row)

    def _match_panel(self, conflict: bool) -> QWidget:
        panel = QWidget()
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(14, 12, 14, 12)
        heading = QLabel("音频文件")
        heading.setObjectName("SectionTitle")
        left_layout.addWidget(heading)
        audio_list = QListWidget()
        names = ["晴天-周杰伦.mp3", "富士山下-陈奕迅.m4a", "必杀技-古巨基.mp3", "光年之外-G.E.M.邓紫棋.flac"]
        if conflict:
            names = ["七里香-周杰伦.flac", "珊瑚海-周杰伦、梁心颐.mp3"]
        audio_list.addItems(names)
        audio_list.setCurrentRow(0)
        left_layout.addWidget(audio_list, 1)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(18, 12, 18, 12)
        title = QLabel("晴天-周杰伦.mp3" if not conflict else "七里香-周杰伦.flac")
        title.setStyleSheet("font-size:17px;font-weight:600")
        right_layout.addWidget(title)
        status_row = QHBoxLayout()
        status_row.addWidget(QLabel("当前状态"))
        status_row.addWidget(make_status_badge("未匹配" if not conflict else "冲突"))
        status_row.addStretch(1)
        right_layout.addLayout(status_row)
        right_layout.addSpacing(6)
        candidates_title = QLabel("歌词候选")
        candidates_title.setObjectName("SectionTitle")
        right_layout.addWidget(candidates_title)
        candidates = QListWidget()
        for text in (["晴天-周杰伦.lrc                         98%", "周杰伦-晴天.lrc                         92%", "晴天 Live.lrc                              71%"] if not conflict else ["七里香-周杰伦.lrc                      98%", "七里香 (修订版).lrc                    86%"]):
            candidates.addItem(QListWidgetItem(text))
        candidates.setCurrentRow(0)
        right_layout.addWidget(candidates)
        preview = QLabel("[00:15.20] 故事的小黄花\n[00:18.56] 从出生那年就飘着\n[00:22.04] 童年的荡秋千……")
        preview.setStyleSheet("background:#f7f7f7;border:1px solid #e1e1e1;border-radius:4px;padding:12px;color:#555")
        right_layout.addWidget(preview)
        buttons = QHBoxLayout()
        buttons.addWidget(QPushButton("查看歌词文本"))
        buttons.addWidget(QPushButton("取消已有匹配"))
        buttons.addStretch(1)
        use = QPushButton("使用所选歌词")
        use.setObjectName("PrimaryButton")
        buttons.addWidget(use)
        right_layout.addLayout(buttons)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([320, 620])
        layout.addWidget(splitter)
        return panel

