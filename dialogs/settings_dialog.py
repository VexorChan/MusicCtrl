from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from dialogs.common import PrototypeDialog, dialog_header


class SettingsDialog(PrototypeDialog):
    save_requested = Signal(object)
    cleanup_requested = Signal()
    open_backup_requested = Signal()
    rescan_requested = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        live_mode: bool = False,
        retention_days: int | None = 7,
        remembered_paths: dict[str, Path | None] | None = None,
        backup_root: Path | None = None,
    ) -> None:
        super().__init__("设置", (900, 650), parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)
        self.live_mode = bool(live_mode)
        self._initial_retention = retention_days
        self._remembered_paths = dict(remembered_paths or {})
        self._backup_root = backup_root
        root.addWidget(dialog_header("设置", "设置保存到本机应用数据库。" if live_mode else "本阶段只展示设置结构，不会写入持久化配置。"))

        body = QHBoxLayout()
        nav = QListWidget()
        nav.setFixedWidth(150)
        nav.addItems(["路径", "重命名", "备份", "数据维护"])
        nav.setCurrentRow(0)
        self.stack = QStackedWidget()
        self.stack.addWidget(self._path_page())
        self.stack.addWidget(self._rename_page())
        self.stack.addWidget(self._backup_page())
        self.stack.addWidget(self._maintenance_page())
        nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        body.addWidget(nav)
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)

        footer = QHBoxLayout()
        self.status = QLabel(
            "目录只会在对应功能中由你明确选择。" if live_mode else "带 * 的目录会由应用自动生成"
        )
        self.status.setWordWrap(True)
        footer.addWidget(self.status)
        footer.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        save = QPushButton("保存")
        save.setObjectName("PrimaryButton")
        save.clicked.connect(self._save)
        footer.addWidget(cancel)
        footer.addWidget(save)
        root.addLayout(footer)

    def _path_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 0, 0, 0)
        group = QGroupBox("路径")
        form = QFormLayout(group)
        if self.live_mode:
            self.path_fields: dict[str, QLineEdit] = {}
            for key, label in (
                ("audio", "音乐扫描目录"),
                ("lyrics", "歌词目录"),
                ("playlist", "歌单目录"),
            ):
                value = self._remembered_paths.get(key)
                line = QLineEdit("未选择" if value is None else str(value))
                line.setReadOnly(True)
                self.path_fields[key] = line
                form.addRow(label, line)
            hint = QLabel("这些目录来自你在扫描、歌词匹配和歌单功能中的最近一次明确选择；设置页不会自行扫描。")
            hint.setWordWrap(True)
            hint.setObjectName("Hint")
            form.addRow(hint)
            layout.addWidget(group)
            layout.addStretch(1)
            return page
        scan = QLineEdit(r"C:\MusicCtrlDemo\Downloads")
        music = QLineEdit(r"C:\MusicCtrlDemo\Music")
        form.addRow("扫描目录", scan)
        remember_scan = QCheckBox("记住上一次选择")
        remember_scan.setChecked(True)
        form.addRow("", remember_scan)
        form.addRow("音乐根目录", music)
        remember_music = QCheckBox("记住上一次选择")
        remember_music.setChecked(True)
        form.addRow("", remember_music)
        for label, path in [("所有音乐 *", r"Music\所有音乐"), ("歌词 *", r"Music\歌词"), ("歌单 *", r"Music\歌单")]:
            line = QLineEdit(path)
            line.setReadOnly(True)
            form.addRow(label, line)
        layout.addWidget(group)
        layout.addStretch(1)
        return page

    def _rename_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 0, 0, 0)
        group = QGroupBox("重命名规则")
        form = QFormLayout(group)
        if self.live_mode:
            name = QLineEdit("歌名-歌手.ext")
            name.setReadOnly(True)
            form.addRow("命名格式", name)
            form.addRow(QLabel("Title 和 Artist 同时有效时采用标签；否则整体按文件名最后一个半角 '-' 解析。"))
            form.addRow(QLabel("是否同步 Title/Artist 在每次重命名预览确认时选择。"))
            forced = QCheckBox("重命名前显示预览（强制开启）")
            forced.setChecked(True)
            forced.setEnabled(False)
            form.addRow(forced)
            layout.addWidget(group)
            layout.addStretch(1)
            return page
        sync = QCheckBox("同步修改 ID3 中的 Title 和 Artist")
        sync.setChecked(True)
        form.addRow(sync)
        name = QLineEdit("歌名-歌手.ext")
        name.setReadOnly(True)
        form.addRow("命名格式", name)
        separator = QLineEdit("、")
        separator.setMaximumWidth(80)
        form.addRow("多歌手分隔符", separator)
        for text in ["清理末尾随机字符串", "清理重复下载序号", "清理音质标记", "清理网站名称"]:
            box = QCheckBox(text)
            box.setChecked(True)
            form.addRow(box)
        forced = QCheckBox("重命名前显示预览（强制开启）")
        forced.setChecked(True)
        forced.setEnabled(False)
        form.addRow(forced)
        layout.addWidget(group)
        layout.addStretch(1)
        return page

    def _backup_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 0, 0, 0)
        group = QGroupBox("备份")
        form = QFormLayout(group)
        self.backup_path = QLineEdit(
            str(self._backup_root)
            if self.live_mode and self._backup_root is not None
            else r"应用本地数据目录\backup"
        )
        self.backup_path.setReadOnly(True)
        form.addRow("备份目录", self.backup_path)
        self.retention = QComboBox()
        self.retention.addItems(["7 天", "15 天", "30 天", "永久保留"])
        self.retention.setCurrentText("永久保留" if self._initial_retention is None else f"{self._initial_retention} 天")
        form.addRow("备份保留时间", self.retention)
        form.addRow(QLabel("删除音乐时先移动到备份目录，不会立即永久删除。"))
        layout.addWidget(group)
        layout.addStretch(1)
        return page

    def _maintenance_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 0, 0, 0)
        group = QGroupBox("数据维护")
        group_layout = QVBoxLayout(group)
        actions = [
            ("重新检查已标记文件", "重新扫描已记住的音乐和歌词目录", self.rescan_requested),
            ("打开备份目录", "打开当前应用的真实备份目录", self.open_backup_requested),
            ("清理过期备份", "预览数量和路径后再永久清理", self.cleanup_requested),
        ] if self.live_mode else [
            ("重新检查已标记文件", "重新生成模拟检查状态", None),
            ("打开备份目录", "查看本地备份文件", None),
            ("清理过期备份", "按保留时间清理过期项目", self.cleanup_requested),
        ]
        self.maintenance_buttons: dict[str, QPushButton] = {}
        for text, hint, signal in actions:
            row = QHBoxLayout()
            labels = QVBoxLayout()
            labels.addWidget(QLabel(text))
            small = QLabel(hint)
            small.setObjectName("Hint")
            labels.addWidget(small)
            row.addLayout(labels)
            row.addStretch(1)
            execute = QPushButton("执行")
            if signal is not None:
                execute.clicked.connect(signal)
            self.maintenance_buttons[text] = execute
            row.addWidget(execute)
            group_layout.addLayout(row)
        layout.addWidget(group)
        layout.addStretch(1)
        return page

    def _save(self) -> None:
        if self.live_mode:
            text = self.retention.currentText()
            value = None if text == "永久保留" else int(text.split()[0])
            self.save_requested.emit({"backup_retention_days": value})
            return
        self.accept()

    def show_message(self, message: str) -> None:
        self.status.setText(message)

    def complete_save(self) -> None:
        self.accept()

    def set_maintenance_running(self, running: bool) -> None:
        for text, button in getattr(self, "maintenance_buttons", {}).items():
            button.setEnabled(not running or text == "打开备份目录")
        if self.live_mode and running:
            self.status.setText("已有后台任务运行；重新检查和永久清理暂不可用。")
