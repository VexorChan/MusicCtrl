from __future__ import annotations

from PySide6.QtWidgets import QFormLayout, QLineEdit, QVBoxLayout, QWidget

from dialogs.common import PrototypeDialog, dialog_header, footer_buttons


class CreatePlaylistDialog(PrototypeDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("新建歌单", (430, 230), parent)
        self.setMinimumSize(400, 220)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.addWidget(dialog_header("新建歌单"))
        form = QFormLayout()
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("例如：周末散步")
        form.addRow("歌单名称", self.name_input)
        root.addLayout(form)
        root.addStretch(1)
        footer, self.create_button = footer_buttons(self, "创建")
        self.create_button.setEnabled(False)
        self.name_input.textChanged.connect(lambda text: self.create_button.setEnabled(bool(text.strip())))
        root.addWidget(footer)


class RenamePlaylistDialog(PrototypeDialog):
    def __init__(self, current_name: str, parent: QWidget | None = None) -> None:
        super().__init__("重命名歌单", (430, 230), parent)
        self.setMinimumSize(400, 220)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.addWidget(dialog_header("重命名歌单", "只修改歌单目录名称，不会修改音乐文件。"))
        form = QFormLayout()
        self.name_input = QLineEdit(current_name)
        self.name_input.selectAll()
        form.addRow("新名称", self.name_input)
        root.addLayout(form)
        root.addStretch(1)
        footer, self.rename_button = footer_buttons(self, "重命名")
        self.name_input.textChanged.connect(
            lambda text: self.rename_button.setEnabled(
                bool(text.strip()) and text.strip() != current_name
            )
        )
        self.rename_button.setEnabled(False)
        root.addWidget(footer)
