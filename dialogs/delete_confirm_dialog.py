from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QLabel, QVBoxLayout, QWidget

from dialogs.common import PrototypeDialog, footer_buttons


class DeleteConfirmDialog(PrototypeDialog):
    def __init__(
        self,
        records: list[dict] | None = None,
        parent: QWidget | None = None,
        *,
        live_mode: bool = False,
    ) -> None:
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
        body = QLabel(
            f"将把选中的 {len(records)} 个音频文件移入应用备份目录，"
            "不会立即永久删除，可在操作历史中恢复。"
            if live_mode
            else f"将删除选中的 {len(records)} 个音频文件。文件会被移动到备份目录，不会立即永久删除。"
        )
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
        warning = QLabel(
            "确认后会实际移动以上音频文件；已匹配歌词不会随音乐自动删除。"
            if live_mode
            else "此操作仅为界面演示，本原型不会删除或移动任何真实文件。"
        )
        warning.setObjectName("Hint")
        root.addWidget(warning)
        self.backup_linked_lyrics = QCheckBox("同时备份当前匹配的外部 LRC")
        self.backup_linked_lyrics.setChecked(False)
        self.backup_linked_lyrics.setVisible(live_mode)
        self.backup_linked_lyrics.setToolTip("仅处理当前外部 LRC；内嵌歌词不会作为文件处理")
        root.addWidget(self.backup_linked_lyrics)
        root.addStretch(1)
        footer, _primary = footer_buttons(
            self, "移入备份" if live_mode else "删除", danger=True
        )
        root.addWidget(footer)


class DeleteLyricsConfirmDialog(PrototypeDialog):
    def __init__(
        self,
        records: list[dict] | None = None,
        parent: QWidget | None = None,
        *,
        live_mode: bool = False,
    ) -> None:
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
            (
                f"将检查选中的 {len(records)} 个歌词文件；仍被音乐引用的歌词会拒绝处理，"
                "其余文件将移入应用备份目录并可恢复。"
            )
            if live_mode
            else (
                f"正式版本删除前会检查选中的 {len(records)} 个歌词文件的引用关系。"
                "确认后文件会被移动到备份目录，不会立即永久删除。"
            )
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

        warning = QLabel(
            "确认后会实际移动未被引用的歌词文件，不会立即永久删除。"
            if live_mode
            else "此操作仅为界面演示，本 M1 原型不会读取、移动或删除任何真实文件。"
        )
        warning.setObjectName("Hint")
        warning.setWordWrap(True)
        root.addWidget(warning)
        root.addStretch(1)

        footer, _primary = footer_buttons(
            self, "移入备份" if live_mode else "删除", danger=True
        )
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
