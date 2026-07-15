from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QHBoxLayout, QWidget


def make_status_badge(text: str) -> QWidget:
    host = QWidget()
    layout = QHBoxLayout(host)
    layout.setContentsMargins(8, 0, 8, 0)
    label = QLabel(text)
    label.setObjectName("StatusBadge")
    label.setProperty("status", text)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(label, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    layout.addStretch(1)
    return host


def make_hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("Hint")
    label.setWordWrap(True)
    return label

