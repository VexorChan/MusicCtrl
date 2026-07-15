from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget


class PrototypeDialog(QDialog):
    def __init__(self, title: str, size: tuple[int, int], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(*size)
        self.setMinimumSize(max(640, size[0] - 260), max(460, size[1] - 160))
        self.setModal(False)


def dialog_header(title: str, subtitle: str = "") -> QWidget:
    host = QWidget()
    layout = QVBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    title_label = QLabel(title)
    title_label.setObjectName("PageTitle")
    layout.addWidget(title_label)
    if subtitle:
        subtitle_label = QLabel(subtitle)
        subtitle_label.setObjectName("Hint")
        subtitle_label.setWordWrap(True)
        layout.addWidget(subtitle_label)
    return host


def footer_buttons(parent: QDialog, primary_text: str, danger: bool = False) -> tuple[QWidget, QPushButton]:
    host = QWidget()
    row = QHBoxLayout(host)
    row.setContentsMargins(0, 0, 0, 0)
    row.addStretch(1)
    cancel = QPushButton("取消")
    cancel.clicked.connect(parent.reject)
    primary = QPushButton(primary_text)
    primary.setObjectName("DangerButton" if danger else "PrimaryButton")
    primary.clicked.connect(parent.accept)
    row.addWidget(cancel)
    row.addWidget(primary)
    return host, primary

