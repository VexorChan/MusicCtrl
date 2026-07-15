from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QLabel, QVBoxLayout, QWidget

from dialogs.common import PrototypeDialog, footer_buttons


class DeleteConfirmDialog(PrototypeDialog):
    def __init__(self, records: list[dict] | None = None, parent: QWidget | None = None) -> None:
        records = records or [
            {"title": "晴天", "artist": "周杰伦", "format": "FLAC"},
            {"title": "珊瑚海", "artist": "周杰伦、梁心颐", "format": "MP3"},
            {"title": "富士山下", "artist": "陈奕迅", "format": "M4A"},
        ]
        super().__init__("删除音乐", (520, 360), parent)
        self.setMinimumSize(480, 330)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(12)
        title = QLabel("删除音乐")
        title.setObjectName("PageTitle")
        root.addWidget(title)
        body = QLabel(f"将删除选中的 {len(records)} 个音频文件。文件会被移动到备份目录，不会立即永久删除。")
        body.setWordWrap(True)
        root.addWidget(body)
        list_text = []
        for item in records[:3]:
            extension = str(item.get("format", "MP3")).lower()
            list_text.append(f"• {item.get('title', '未知')}-{item.get('artist', '未知')}.{extension}")
        if len(records) > 3:
            list_text.append(f"• 以及其他 {len(records) - 3} 个文件")
        files = QLabel("\n".join(list_text))
        files.setStyleSheet("background:#f5f5f5;border:1px solid #dedede;border-radius:4px;padding:12px;color:#555")
        root.addWidget(files)
        self.delete_lyrics = QCheckBox("同时删除已匹配的歌词")
        self.delete_lyrics.setChecked(False)
        root.addWidget(self.delete_lyrics)
        warning = QLabel("此操作仅为界面演示，本原型不会删除或移动任何真实文件。")
        warning.setObjectName("Hint")
        root.addWidget(warning)
        root.addStretch(1)
        footer, _primary = footer_buttons(self, "删除", danger=True)
        root.addWidget(footer)


class DeleteLyricsConfirmDialog(PrototypeDialog):
    def __init__(self, records: list[dict] | None = None, parent: QWidget | None = None) -> None:
        records = records or [{"title": "晴天", "artist": "周杰伦", "format": "LRC"}]
        super().__init__("删除歌词", (520, 330), parent)
        self.setMinimumSize(480, 300)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(12)

        title = QLabel("删除歌词")
        title.setObjectName("PageTitle")
        root.addWidget(title)

        body = QLabel(
            f"正式版本删除前会检查选中的 {len(records)} 个歌词文件的引用关系。"
            "确认后文件会被移动到备份目录，不会立即永久删除。"
        )
        body.setWordWrap(True)
        root.addWidget(body)

        list_text = []
        for item in records[:3]:
            list_text.append(f"• {item.get('title', '未知')}-{item.get('artist', '未知')}.lrc")
        if len(records) > 3:
            list_text.append(f"• 以及其他 {len(records) - 3} 个文件")
        files = QLabel("\n".join(list_text))
        files.setStyleSheet("background:#f5f5f5;border:1px solid #dedede;border-radius:4px;padding:12px;color:#555")
        root.addWidget(files)

        warning = QLabel("此操作仅为界面演示，本 M1 原型不会读取、移动或删除任何真实文件。")
        warning.setObjectName("Hint")
        warning.setWordWrap(True)
        root.addWidget(warning)
        root.addStretch(1)

        footer, _primary = footer_buttons(self, "删除", danger=True)
        root.addWidget(footer)


class RemovePlaylistItemsDialog(PrototypeDialog):
    def __init__(self, count: int, parent: QWidget | None = None) -> None:
        super().__init__("从歌单移除", (480, 250), parent)
        self.setMinimumSize(440, 230)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.addWidget(QLabel(f"将从当前歌单移除选中的 {count} 个快捷方式。"))
        note = QLabel("这不会删除音乐文件。")
        note.setStyleSheet("font-weight:600")
        root.addWidget(note)
        root.addStretch(1)
        footer, _primary = footer_buttons(self, "移除", danger=True)
        root.addWidget(footer)
