from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from dialogs.common import PrototypeDialog, dialog_header
from mock.data import HISTORY
from services.history_service import HistoryDetail, HistoryRecord, HistoryService, HistorySnapshot
from ui.components import make_status_badge
from ui.tables import DataTable


_FILTERS = (
    ("全部", None),
    ("导入", "import"),
    ("重命名", "rename"),
    ("删除", "delete"),
    ("歌单", "playlist"),
    ("歌词匹配", "lyrics"),
)


def _mock_snapshot() -> HistorySnapshot:
    categories = {
        "导入音频": "import",
        "导入歌词": "import",
        "撤销导入": "import",
        "批量重命名": "rename",
        "歌词匹配": "lyrics",
        "创建歌单": "playlist",
        "添加到歌单": "playlist",
        "删除音乐": "delete",
    }
    records: list[HistoryRecord] = []
    for index, (created, action, success, failure, status) in enumerate(HISTORY):
        created_at = datetime.strptime(created, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).isoformat()
        detail = HistoryDetail(
            f"示例文件-{index + 1}.mp3",
            None,
            None,
            str(status),
            "原型演示记录",
            created_at,
        )
        records.append(
            HistoryRecord(
                f"mock:{index}",
                categories.get(str(action), "import"),
                str(action),
                created_at,
                str(status),
                int(success),
                int(failure),
                (detail,),
                undoable=index == 0,
            )
        )
    return HistorySnapshot(tuple(records))


class HistoryDialog(PrototypeDialog):
    restore_requested = Signal(object)
    cleanup_requested = Signal()
    undo_import_requested = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        snapshot: HistorySnapshot | None = None,
        backup_entries: tuple[object, ...] | None = None,
        import_batches: tuple[dict[str, object], ...] = (),
    ) -> None:
        super().__init__("操作历史", (1000, 650), parent)
        self._live_mode = snapshot is not None or backup_entries is not None
        if snapshot is None and backup_entries is not None:
            backup_source = type("LegacyBackupSource", (), {"list_entries": lambda _self: backup_entries})()
            import_source = type("LegacyImportSource", (), {"list_history": lambda _self: import_batches})()
            snapshot = HistoryService(
                import_controller=import_source,
                backup_controller=backup_source,
            ).load()
        self._snapshot = snapshot or _mock_snapshot()
        self._active_category: str | None = None
        self._visible_records: tuple[HistoryRecord, ...] = ()

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)
        root.addWidget(
            dialog_header(
                "操作历史",
                "查看真实操作记录、文件级结果，并按当前资格恢复或撤销。"
                if self._live_mode
                else "查看模拟操作记录和文件级明细。",
            )
        )

        filters = QHBoxLayout()
        self.filter_group = QButtonGroup(self)
        self.filter_group.setExclusive(True)
        self.filter_buttons: dict[str, QPushButton] = {}
        for index, (label, category) in enumerate(_FILTERS):
            button = QPushButton(label)
            button.setObjectName("FilterButton")
            button.setCheckable(True)
            button.setChecked(index == 0)
            button.clicked.connect(lambda _checked=False, value=category: self.set_filter(value))
            self.filter_group.addButton(button)
            self.filter_buttons[label] = button
            filters.addWidget(button)
        filters.addStretch(1)
        root.addLayout(filters)

        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color:#a55b00")
        root.addWidget(self.warning_label)

        self.table = DataTable()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["时间", "操作类型", "成功数量", "失败数量", "状态"])
        self.table.setColumnWidth(0, 190)
        self.table.horizontalHeader().setSectionResizeMode(1, self.table.horizontalHeader().ResizeMode.Stretch)
        self.table.setColumnWidth(2, 100)
        self.table.setColumnWidth(3, 100)
        self.table.setColumnWidth(4, 130)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        root.addWidget(self.table, 2)

        detail_title = QLabel("操作明细")
        detail_title.setStyleSheet("font-weight:600")
        root.addWidget(detail_title)
        self.detail_table = DataTable()
        self.detail_table.setColumnCount(6)
        self.detail_table.setHorizontalHeaderLabels(["文件", "原路径", "目标路径", "结果", "原因", "时间"])
        self.detail_table.horizontalHeader().setSectionResizeMode(0, self.detail_table.horizontalHeader().ResizeMode.ResizeToContents)
        self.detail_table.horizontalHeader().setSectionResizeMode(1, self.detail_table.horizontalHeader().ResizeMode.Stretch)
        self.detail_table.horizontalHeader().setSectionResizeMode(2, self.detail_table.horizontalHeader().ResizeMode.Stretch)
        self.detail_table.setColumnWidth(3, 90)
        self.detail_table.setColumnWidth(4, 180)
        self.detail_table.setColumnWidth(5, 180)
        root.addWidget(self.detail_table, 1)

        footer = QHBoxLayout()
        self.restore_button = QPushButton("恢复所选备份")
        self.restore_button.setToolTip("仅当前仍存在且尚未恢复的备份可以恢复")
        self.restore_button.clicked.connect(self._request_restore)
        footer.addWidget(self.restore_button)
        self.undo_import_button = QPushButton("撤销最近完整导入")
        self.undo_import_button.setToolTip("仅最近一次完整成功且尚未撤销的导入可以撤销")
        self.undo_import_button.clicked.connect(self._request_undo_import)
        footer.addWidget(self.undo_import_button)
        self.undo = self.restore_button if self._live_mode else self.undo_import_button
        self.cleanup_button = QPushButton("清理到期备份")
        self.cleanup_button.setObjectName("DangerButton")
        self.cleanup_button.clicked.connect(self.cleanup_requested)
        footer.addWidget(self.cleanup_button)
        if not self._live_mode:
            self.restore_button.hide()
            self.cleanup_button.hide()
        footer.addStretch(1)
        close = QPushButton("关闭")
        close.clicked.connect(self.close)
        footer.addWidget(close)
        root.addLayout(footer)

        self.set_snapshot(self._snapshot)

    @property
    def visible_records(self) -> tuple[HistoryRecord, ...]:
        return self._visible_records

    def set_snapshot(self, snapshot: HistorySnapshot) -> None:
        self._snapshot = snapshot
        self.warning_label.setText("\n".join(snapshot.warnings))
        self.warning_label.setVisible(bool(snapshot.warnings))
        self._render_records()

    def set_filter(self, category: str | None) -> None:
        self._active_category = category
        self._render_records()

    def _render_records(self) -> None:
        self._visible_records = tuple(
            record
            for record in self._snapshot.records
            if self._active_category is None or record.category == self._active_category
        )
        self.table.setRowCount(len(self._visible_records))
        for row, record in enumerate(self._visible_records):
            values = (
                record.created_at,
                record.action,
                record.success_count,
                record.failure_count,
                record.status,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem("" if column == 4 else str(value))
                if column in {2, 3}:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, record)
                if column == 4:
                    item.setData(Qt.ItemDataRole.UserRole, record.status)
                    item.setToolTip(str(value))
                self.table.setItem(row, column, item)
            self.table.setCellWidget(row, 4, make_status_badge(record.status))
        if self._visible_records:
            self.table.selectRow(0)
            self._selection_changed()
        else:
            self.table.clearSelection()
            self._show_details(None)

    def _selected_record(self) -> HistoryRecord | None:
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        if len(rows) != 1:
            return None
        item = self.table.item(rows[0], 0)
        record = None if item is None else item.data(Qt.ItemDataRole.UserRole)
        return record if isinstance(record, HistoryRecord) else None

    def _selection_changed(self) -> None:
        self._show_details(self._selected_record())

    def _show_details(self, record: HistoryRecord | None) -> None:
        details = () if record is None else record.items
        self.detail_table.setRowCount(len(details))
        for row, detail in enumerate(details):
            values = (
                detail.file_name,
                "" if detail.source_path is None else str(detail.source_path),
                "" if detail.target_path is None else str(detail.target_path),
                detail.result,
                detail.reason,
                detail.completed_at,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                self.detail_table.setItem(row, column, item)
        self.restore_button.setEnabled(bool(record is not None and record.restore_ids))
        self.undo_import_button.setEnabled(bool(record is not None and record.undoable))

    def _request_restore(self) -> None:
        record = self._selected_record()
        if record is not None and record.restore_ids:
            self.restore_requested.emit(record.restore_ids)

    def _request_undo_import(self) -> None:
        record = self._selected_record()
        if record is not None and record.undoable:
            self.undo_import_requested.emit()
