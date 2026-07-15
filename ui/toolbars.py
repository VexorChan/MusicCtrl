from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget


class GlobalToolbar(QWidget):
    import_requested = Signal()
    rename_requested = Signal()
    lyrics_requested = Signal()
    history_requested = Signal()
    settings_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("GlobalToolbar")
        self.buttons_by_text: dict[str, QPushButton] = {}
        self.setFixedHeight(58)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 10, 20, 10)
        layout.setSpacing(8)

        buttons = [
            ("导入", self.import_requested),
            ("重命名", self.rename_requested),
            ("匹配歌词", self.lyrics_requested),
            ("操作历史", self.history_requested),
            ("设置", self.settings_requested),
        ]
        for text, signal in buttons:
            button = QPushButton(text)
            button.setObjectName("ToolbarButton")
            button.setAccessibleName(text)
            button.clicked.connect(signal)
            layout.addWidget(button)
            self.buttons_by_text[text] = button
        layout.addStretch(1)

    @property
    def button_order(self) -> list[str]:
        return list(self.buttons_by_text)
