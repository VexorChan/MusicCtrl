from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from mock.data import PLAYLISTS


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_ICON_PATH = PROJECT_ROOT / "assets" / "app_icon.png"


class Sidebar(QWidget):
    navigation_requested = Signal(str)
    create_playlist_requested = Signal()

    def __init__(self, parent: QWidget | None = None, *, live_mode: bool = False) -> None:
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setFixedWidth(216)
        self._buttons: dict[str, QPushButton] = {}
        self._playlist_buttons: dict[str, QPushButton] = {}
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 16, 12, 12)
        root.setSpacing(8)

        brand = QHBoxLayout()
        self.brand_icon = QLabel()
        self.brand_icon.setObjectName("AppMark")
        self.brand_icon.setAccessibleName("乐库整理助手图标")
        self.brand_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.brand_icon.setFixedSize(32, 32)
        if APP_ICON_PATH.is_file():
            pixmap = QPixmap(str(APP_ICON_PATH))
            if not pixmap.isNull():
                self.brand_icon.setPixmap(
                    pixmap.scaled(
                        32,
                        32,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
        name = QLabel("乐库整理助手")
        name.setObjectName("AppName")
        brand.addWidget(self.brand_icon)
        brand.addSpacing(2)
        brand.addWidget(name)
        brand.addStretch(1)
        root.addLayout(brand)
        root.addSpacing(8)

        library = QLabel("音乐库")
        library.setObjectName("SectionTitle")
        root.addWidget(library)
        root.addWidget(self._nav_button("所有音乐", "所有音乐"))
        root.addWidget(self._nav_button("所有歌词", "所有歌词"))

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color:#dddddd")
        root.addWidget(line)

        section_row = QHBoxLayout()
        section = QLabel("歌单")
        section.setObjectName("SectionTitle")
        self.add_playlist_button = QPushButton("+")
        self.add_playlist_button.setObjectName("PlaylistAdd")
        self.add_playlist_button.setAccessibleName("新建歌单")
        self.add_playlist_button.setToolTip("新建歌单")
        self.add_playlist_button.clicked.connect(self.create_playlist_requested)
        section_row.addWidget(section)
        section_row.addStretch(1)
        section_row.addWidget(self.add_playlist_button)
        root.addLayout(section_row)

        self.search = QLineEdit()
        self.search.setObjectName("SidebarSearch")
        self.search.setPlaceholderText("搜索歌单")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter_playlists)
        root.addWidget(self.search)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_host = QWidget()
        self.playlist_layout = QVBoxLayout(scroll_host)
        self.playlist_layout.setContentsMargins(0, 0, 0, 0)
        self.playlist_layout.setSpacing(4)
        for playlist in (() if live_mode else PLAYLISTS):
            button = self._nav_button(f"playlist:{playlist}", playlist)
            self._playlist_buttons[playlist] = button
            self.playlist_layout.addWidget(button)
        self.playlist_layout.addStretch(1)
        scroll.setWidget(scroll_host)
        root.addWidget(scroll, 1)

        version = QLabel("本地文件管理" if live_mode else "仅 UI 演示 · 不会操作真实文件")
        version.setObjectName("Hint")
        version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(version)

        self.select_key("所有音乐")

    def _nav_button(self, key: str, text: str) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("NavButton")
        button.setProperty("navAlignment", "center")
        button.setCheckable(True)
        button.clicked.connect(lambda _checked=False, k=key: self.navigation_requested.emit(k))
        self._group.addButton(button)
        self._buttons[key] = button
        return button

    def _filter_playlists(self, text: str) -> None:
        query = text.strip().casefold()
        for name, button in self._playlist_buttons.items():
            button.setVisible(not query or query in name.casefold())

    def select_key(self, key: str) -> None:
        if key in self._buttons:
            self._buttons[key].setChecked(True)

    def add_playlist(self, name: str) -> None:
        if not name or name in self._playlist_buttons:
            return
        button = self._nav_button(f"playlist:{name}", name)
        self._playlist_buttons[name] = button
        self.playlist_layout.insertWidget(self.playlist_layout.count() - 1, button)

    def set_playlists(self, names: tuple[str, ...] | list[str]) -> None:
        for name, button in tuple(self._playlist_buttons.items()):
            self._group.removeButton(button)
            self._buttons.pop(f"playlist:{name}", None)
            self.playlist_layout.removeWidget(button)
            button.deleteLater()
        self._playlist_buttons.clear()
        for name in names:
            self.add_playlist(name)
        self._filter_playlists(self.search.text())
