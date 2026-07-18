from __future__ import annotations

import re
from typing import Iterable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QStackedLayout,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from mock.data import PLAYLISTS
from ui.components import make_status_badge
from ui.tables import DataTable, ModelDataTable


def _normalize(text: str) -> str:
    return text.strip().casefold()


def _score(value: str, query: str) -> int:
    value = _normalize(value)
    if value == query:
        return 100
    if value.startswith(query):
        return 90
    if query in value:
        return 80
    pos = -1
    gaps = 0
    for char in query:
        found = value.find(char, pos + 1)
        if found < 0:
            return 0
        if pos >= 0:
            gaps += found - pos - 1
        pos = found
    return max(60, 79 - gaps)


def _size_number(value: str) -> float:
    match = re.search(r"[\d.]+", value)
    if not match:
        return 0.0
    amount = float(match.group())
    unit = value.upper()
    if "GB" in unit:
        return amount * 1024 * 1024 * 1024
    if "MB" in unit:
        return amount * 1024 * 1024
    if "KB" in unit:
        return amount * 1024
    return amount


def _duration_number(value: str) -> int:
    try:
        minute, second = value.split(":", 1)
        return int(minute) * 60 + int(second)
    except ValueError:
        return 0


class PlaylistAddMenu(QMenu):
    confirmed = Signal(list)
    create_requested = Signal()

    def __init__(self, playlist_names: Iterable[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("PlaylistAddMenu")
        self.setMinimumWidth(330)
        self.playlist_actions: dict[str, QAction] = {}

        search_host = QWidget()
        search_layout = QHBoxLayout(search_host)
        search_layout.setContentsMargins(8, 6, 8, 6)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索歌单")
        self.search.setClearButtonEnabled(True)
        search_layout.addWidget(self.search)
        search_action = QWidgetAction(self)
        search_action.setDefaultWidget(search_host)
        self.addAction(search_action)
        self.addSeparator()

        for playlist in playlist_names:
            label = playlist
            if playlist == "我喜欢的":
                label = f"{playlist}    已存在，将自动跳过"
            action = QAction(label, self)
            action.setCheckable(True)
            action.toggled.connect(self._update_confirm_state)
            self.playlist_actions[playlist] = action
            self.addAction(action)

        self.addSeparator()
        info = QAction("仅创建快捷方式，不会复制音乐文件", self)
        info.setEnabled(False)
        self.addAction(info)

        footer_host = QWidget()
        footer_layout = QHBoxLayout(footer_host)
        footer_layout.setContentsMargins(8, 6, 8, 6)
        create = QPushButton("新建歌单")
        create.clicked.connect(self.create_requested)
        footer_layout.addWidget(create)
        footer_layout.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.close)
        self.confirm_button = QPushButton("添加")
        self.confirm_button.setObjectName("PrimaryButton")
        self.confirm_button.setEnabled(False)
        self.confirm_button.clicked.connect(self._confirm)
        footer_layout.addWidget(cancel)
        footer_layout.addWidget(self.confirm_button)
        footer_action = QWidgetAction(self)
        footer_action.setDefaultWidget(footer_host)
        self.addAction(footer_action)

        self.search.textChanged.connect(self._filter_playlists)

    def selected_playlists(self) -> list[str]:
        return [name for name, action in self.playlist_actions.items() if action.isChecked()]

    def _filter_playlists(self, text: str) -> None:
        query = _normalize(text)
        for name, action in self.playlist_actions.items():
            action.setVisible(not query or query in _normalize(name))

    def _update_confirm_state(self, *_args) -> None:
        self.confirm_button.setEnabled(bool(self.selected_playlists()))

    def _confirm(self) -> None:
        selected = self.selected_playlists()
        if not selected:
            return
        self.confirmed.emit(selected)
        self.close()


class LibraryTableModel(QAbstractTableModel):
    """Immutable-record model for the production all-music view."""

    check_state_changed = Signal()

    def __init__(self, *, kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.kind = kind
        self._records: tuple[dict[str, object], ...] = ()
        self._checked: set[int] = set()

    def columns(self) -> tuple[str, ...]:
        if self.kind == "lyrics":
            return ("", "歌名", "歌手", "格式", "大小", "歌词状态")
        return ("", "歌名", "歌手", "时长", "格式", "大小", "歌词状态")

    def fields(self) -> tuple[str, ...]:
        if self.kind == "lyrics":
            return ("title", "artist", "format", "size", "status")
        return ("title", "artist", "duration", "format", "size", "status")

    def replace_records(self, records: Iterable[dict[str, object]]) -> None:
        self.beginResetModel()
        self._records = tuple(dict(record) for record in records)
        self._checked.clear()
        self.endResetModel()
        self.check_state_changed.emit()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._records)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.columns())

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            columns = self.columns()
            return columns[section] if 0 <= section < len(columns) else None
        return super().headerData(section, orientation, role)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._records):
            return None
        if index.column() == 0:
            if role == Qt.ItemDataRole.CheckStateRole:
                return Qt.CheckState.Checked if index.row() in self._checked else Qt.CheckState.Unchecked
            if role == Qt.ItemDataRole.TextAlignmentRole:
                return int(Qt.AlignmentFlag.AlignCenter)
            return None
        field = self.fields()[index.column() - 1]
        record = self._records[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return str(record.get(field, ""))
        if role == Qt.ItemDataRole.UserRole:
            return record.get("_index", index.row())
        if role == Qt.ItemDataRole.TextAlignmentRole and field in {"duration", "format", "size"}:
            return int(Qt.AlignmentFlag.AlignCenter)
        if role == Qt.ItemDataRole.ToolTipRole and field == "status":
            return str(record.get(field, ""))
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.isValid() and index.column() == 0:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(self, index: QModelIndex, value, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if not index.isValid() or index.column() != 0 or role != Qt.ItemDataRole.CheckStateRole:
            return False
        if Qt.CheckState(value) == Qt.CheckState.Checked:
            self._checked.add(index.row())
        else:
            self._checked.discard(index.row())
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        self.check_state_changed.emit()
        return True

    def checked_rows(self) -> tuple[int, ...]:
        return tuple(sorted(self._checked))

    def set_all_checked(self, checked: bool) -> None:
        self._checked = set(range(len(self._records))) if checked else set()
        if self._records:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._records) - 1, 0),
                [Qt.ItemDataRole.CheckStateRole],
            )
        self.check_state_changed.emit()


class LibraryPage(QWidget):
    delete_requested = Signal(list)
    new_playlist_requested = Signal()
    add_to_playlists_requested = Signal(object, object)
    open_location_requested = Signal(object)
    rename_context_requested = Signal(object)
    rematch_lyrics_requested = Signal(object)

    def __init__(
        self,
        title: str,
        data: Iterable[dict],
        *,
        kind: str = "music",
        display_count: int | None = None,
        playlist_name: str | None = None,
        use_model_view: bool = False,
        live_mode: bool = False,
        playlist_names: Iterable[str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("PageRoot")
        self.kind = kind
        self.playlist_name = playlist_name
        self.use_model_view = bool(use_model_view)
        self.live_mode = bool(live_mode)
        self._playlist_names = tuple(PLAYLISTS if playlist_names is None else playlist_names)
        self.all_data = [dict(item, _index=index) for index, item in enumerate(data)]
        self.visible_data = list(self.all_data)
        self.sort_key: str | None = None
        self.sort_descending = False
        self.playlist_note: QLabel | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 12)
        root.setSpacing(12)

        title_row = QHBoxLayout()
        title_label = QLabel(title)
        title_label.setObjectName("PageTitle")
        self.count_label = QLabel(f"共 {display_count if display_count is not None else len(self.all_data)} 首")
        self.count_label.setObjectName("PageCount")
        title_row.addWidget(title_label)
        title_row.addSpacing(8)
        title_row.addWidget(self.count_label, 0, Qt.AlignmentFlag.AlignBottom)
        title_row.addStretch(1)
        root.addLayout(title_row)

        if playlist_name:
            self.playlist_note = QLabel("从歌单移除只会删除快捷方式，不会删除音乐文件。")
            self.playlist_note.setObjectName("Hint")
            root.addWidget(self.playlist_note)

        actions = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索歌名或歌手")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(260)
        self.search.setMaximumWidth(380)
        actions.addWidget(self.search)
        actions.addStretch(1)
        self.sort_button = QPushButton("排序")
        self.sort_button.setAccessibleName("排序")
        self.sort_button.setFixedWidth(88)
        self.sort_button.clicked.connect(self._show_sort_menu)
        actions.addWidget(self.sort_button)
        self.add_button = QPushButton("添加到")
        self.add_button.setAccessibleName("添加到歌单")
        self.add_button.setFixedWidth(104)
        self.add_button.setEnabled(False)
        self.add_button.clicked.connect(self._show_add_menu)
        actions.addWidget(self.add_button)
        self.delete_button = QPushButton("从歌单移除" if playlist_name else "删除")
        self.delete_button.setObjectName("DangerButton")
        self.delete_button.setMinimumWidth(88)
        self.delete_button.setMaximumWidth(118 if playlist_name else 88)
        self.delete_button.setEnabled(False)
        self.delete_button.clicked.connect(self._request_delete)
        actions.addWidget(self.delete_button)
        if kind == "lyrics":
            self.add_button.hide()
        root.addLayout(actions)

        host = QWidget()
        self.content_stack = QStackedLayout(host)
        self.content_stack.setContentsMargins(0, 0, 0, 0)
        self._table_model: LibraryTableModel | None = None
        if self.use_model_view:
            self.table = ModelDataTable(checkable_header=True)
            self._table_model = LibraryTableModel(kind=self.kind, parent=self.table)
            self.table.setModel(self._table_model)
            self._table_model.check_state_changed.connect(self._update_selection_state)
        else:
            self.table = DataTable(checkable_header=True)
        self.checkable_header = self.table.require_checkable_header()
        self.table.selectionModel().selectionChanged.connect(self._update_selection_state)
        if not self.use_model_view:
            self.table.itemChanged.connect(self._update_selection_state)
        self.checkable_header.toggle_requested.connect(self._toggle_select_all)
        self.table.horizontalHeader().sectionClicked.connect(self._header_clicked)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.content_stack.addWidget(self.table)

        empty = QWidget()
        empty_layout = QVBoxLayout(empty)
        empty_layout.addStretch(1)
        icon = QLabel("⌕")
        icon.setObjectName("EmptyIcon")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_title = QLabel("没有找到匹配的音乐")
        self.empty_title.setObjectName("EmptyTitle")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_hint = QLabel("请尝试更换关键词，或清空搜索条件。")
        empty_hint.setObjectName("EmptyHint")
        empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(icon)
        empty_layout.addWidget(self.empty_title)
        empty_layout.addWidget(empty_hint)
        empty_layout.addStretch(1)
        self.content_stack.addWidget(empty)
        root.addWidget(host, 1)

        self.status = QLabel()
        self.status.setObjectName("StatusBar")
        self.status.setFixedHeight(30)
        root.addWidget(self.status)

        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(130)
        self.search_timer.timeout.connect(self._apply_search)
        self.search.textChanged.connect(lambda _text: self.search_timer.start())
        self._populate_table()

    def replace_data(self, records: Iterable[dict]) -> None:
        """Replace library rows without retaining stale selection or sort state."""

        self.search_timer.stop()
        self.table.clearSelection()
        self.all_data = [dict(item, _index=index) for index, item in enumerate(records)]
        self.sort_key = None
        self.sort_descending = False
        self.visible_data = self._matching_rows(self.search.text())
        self.count_label.setText(f"共 {len(self.all_data)} 首")
        self._populate_table()

    def set_playlist_names(self, names: Iterable[str]) -> None:
        self._playlist_names = tuple(names)

    def _columns(self) -> list[str]:
        if self.kind == "lyrics":
            return ["", "歌名", "歌手", "格式", "大小", "歌词状态"]
        return ["", "歌名", "歌手", "时长", "格式", "大小", "歌词状态"]

    def _field_order(self) -> list[str]:
        if self.kind == "lyrics":
            return ["title", "artist", "format", "size", "status"]
        return ["title", "artist", "duration", "format", "size", "status"]

    def _populate_table(self) -> None:
        columns = self._columns()
        fields = self._field_order()
        if self._table_model is not None:
            self.table.clearSelection()
            self._table_model.replace_records(self.visible_data)
            header = self.table.horizontalHeader()
            header.setMinimumSectionSize(40)
            for column, width in enumerate((44, 230, 190, 76, 72, 84, 136)):
                self.table.setColumnWidth(column, width)
            header.setStretchLastSection(False)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            self.content_stack.setCurrentIndex(0 if self.visible_data else 1)
            if not self.visible_data:
                self.empty_title.setText("没有找到匹配的歌词" if self.kind == "lyrics" else "没有找到匹配的音乐")
            if self.sort_key:
                field_column = self._field_order().index(self.sort_key) + 1
                order = Qt.SortOrder.DescendingOrder if self.sort_descending else Qt.SortOrder.AscendingOrder
                header.setSortIndicator(field_column, order)
                header.setSortIndicatorShown(True)
            else:
                header.setSortIndicatorShown(False)
            self._update_selection_state()
            return
        self.table.blockSignals(True)
        self.table.clear()
        self.table.setColumnCount(len(columns))
        self.table.setHorizontalHeaderLabels(columns)
        self.table.setRowCount(len(self.visible_data))
        for row, record in enumerate(self.visible_data):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, check)
            for offset, field in enumerate(fields, start=1):
                value = str(record.get(field, ""))
                # 状态列由自定义徽标负责显示，底层项目不再重复绘制文字。
                item = QTableWidgetItem("" if field == "status" else value)
                item.setData(Qt.ItemDataRole.UserRole, record.get("_index", row))
                if field in {"duration", "format", "size"}:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, offset, item)
                if field == "status":
                    item.setToolTip(value)
                    self.table.setCellWidget(row, offset, make_status_badge(value))
        header = self.table.horizontalHeader()
        header.setMinimumSectionSize(40)
        self.table.setColumnWidth(0, 44)
        self.table.setColumnWidth(1, 230)
        self.table.setColumnWidth(2, 190)
        if self.kind == "lyrics":
            self.table.setColumnWidth(3, 72)
            self.table.setColumnWidth(4, 84)
            self.table.setColumnWidth(5, 136)
        else:
            self.table.setColumnWidth(3, 76)
            self.table.setColumnWidth(4, 72)
            self.table.setColumnWidth(5, 84)
            self.table.setColumnWidth(6, 136)
        header.setStretchLastSection(False)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.blockSignals(False)
        self.content_stack.setCurrentIndex(0 if self.visible_data else 1)
        if not self.visible_data:
            self.empty_title.setText("没有找到匹配的歌词" if self.kind == "lyrics" else "没有找到匹配的音乐")
        if self.sort_key:
            field_column = self._field_order().index(self.sort_key) + 1
            order = Qt.SortOrder.DescendingOrder if self.sort_descending else Qt.SortOrder.AscendingOrder
            header.setSortIndicator(field_column, order)
            header.setSortIndicatorShown(True)
        else:
            header.setSortIndicatorShown(False)
        self._update_selection_state()

    def _matching_rows(self, query: str) -> list[dict]:
        query = _normalize(query)
        if not query:
            return list(self.all_data)
        title_matches = [(_score(item["title"], query), item) for item in self.all_data]
        title_matches = [(score, item) for score, item in title_matches if score]
        matches = title_matches
        if not matches:
            matches = [(_score(item["artist"], query), item) for item in self.all_data]
            matches = [(score, item) for score, item in matches if score]
        matches.sort(key=lambda pair: (-pair[0], pair[1]["_index"]))
        return [item for _score_value, item in matches]

    def _apply_search(self) -> None:
        # 新搜索必须优先按匹配度排序，因此清除之前的手动排序状态。
        self.sort_key = None
        self.sort_descending = False
        self.visible_data = self._matching_rows(self.search.text())
        self._populate_table()

    def apply_search_immediately(self, text: str) -> None:
        self.search.setText(text)
        self.search_timer.stop()
        self._apply_search()

    def _checked_rows(self) -> list[int]:
        if self._table_model is not None:
            return list(self._table_model.checked_rows())
        rows: list[int] = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                rows.append(row)
        return rows

    def _selected_rows(self) -> list[int]:
        rows = {index.row() for index in self.table.selectionModel().selectedRows()}
        rows.update(self._checked_rows())
        return sorted(rows)

    def selected_records(self) -> tuple[dict[str, object], ...]:
        """Return a frozen-by-copy snapshot of the visible selection union."""

        return tuple(
            dict(self.visible_data[row])
            for row in self._selected_rows()
            if row < len(self.visible_data)
        )

    def _selected_records(self) -> list[dict[str, object]]:
        return list(self.selected_records())

    def _update_selection_state(self, *_args) -> None:
        checked_count = len(self._checked_rows())
        count = len(self._selected_rows())
        row_count = self._table_model.rowCount() if self._table_model is not None else self.table.rowCount()
        if checked_count == 0 or row_count == 0:
            header_state = Qt.CheckState.Unchecked
        elif checked_count == row_count:
            header_state = Qt.CheckState.Checked
        else:
            header_state = Qt.CheckState.PartiallyChecked
        self.checkable_header.set_check_state(header_state)
        self.add_button.setEnabled(count > 0 and self.kind == "music")
        self.delete_button.setEnabled(count > 0)
        shown = len(self.visible_data)
        suffix = "索引来自用户选择目录" if self.live_mode else "仅演示界面，不操作真实文件"
        self.status.setText(f"已选择 {count} 项    ·    当前显示 {shown} 项    ·    {suffix}")

    def _header_clicked(self, column: int) -> None:
        if column == 0:
            self._toggle_select_all()
            return
        fields = self._field_order()
        if column < 1 or column > len(fields):
            return
        key = fields[column - 1]
        descending = self.sort_key == key and not self.sort_descending
        self._sort_records(key, descending)

    def _toggle_select_all(self) -> None:
        all_checked = self.checkable_header.check_state() == Qt.CheckState.Checked
        if self._table_model is not None:
            self._table_model.set_all_checked(not all_checked)
            return
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            self.table.item(row, 0).setCheckState(Qt.CheckState.Unchecked if all_checked else Qt.CheckState.Checked)
        self.table.blockSignals(False)
        self._update_selection_state()

    def _sort_records(self, key: str, descending: bool) -> None:
        self.sort_key = key
        self.sort_descending = descending
        if key == "duration":
            key_fn = lambda item: _duration_number(item.get(key, ""))
        elif key == "size":
            key_fn = lambda item: _size_number(item.get(key, ""))
        else:
            key_fn = lambda item: _normalize(item.get(key, ""))
        # 先恢复原始序号，再使用稳定排序，保证同值记录始终保持原列表顺序。
        self.visible_data.sort(key=lambda item: item["_index"])
        self.visible_data.sort(key=key_fn, reverse=descending)
        self._populate_table()

    def create_sort_menu(self) -> QMenu:
        menu = QMenu(self)
        labels = [
            ("按歌名升序", "title", False), ("按歌名降序", "title", True),
            ("按歌手升序", "artist", False), ("按歌手降序", "artist", True),
        ]
        if self.kind == "music":
            labels += [
                ("按时长升序", "duration", False), ("按时长降序", "duration", True),
                ("按大小升序", "size", False), ("按大小降序", "size", True),
            ]
        for label, key, desc in labels:
            action = QAction(label, menu)
            action.setCheckable(True)
            action.setChecked(key == self.sort_key and desc == self.sort_descending)
            action.triggered.connect(lambda _checked=False, k=key, d=desc: self._sort_records(k, d))
            menu.addAction(action)
        return menu

    def _show_sort_menu(self) -> None:
        menu = self.create_sort_menu()
        menu.exec(self.sort_button.mapToGlobal(QPoint(0, self.sort_button.height())))

    def create_playlist_menu(self) -> PlaylistAddMenu:
        menu = PlaylistAddMenu(self._playlist_names, self)
        menu.create_requested.connect(self.new_playlist_requested)
        menu.confirmed.connect(self._simulate_add_to_playlists)
        return menu

    def _show_add_menu(self) -> None:
        menu = self.create_playlist_menu()
        menu.exec(self.add_button.mapToGlobal(QPoint(0, self.add_button.height())))

    def _simulate_add_to_playlists(self, playlists: list[str]) -> None:
        records = self._selected_records()
        if self.live_mode:
            self.add_to_playlists_requested.emit(records, list(playlists))
            return
        item_count = len(records)
        self.status.setText(
            f"已模拟将 {item_count} 项添加到 {len(playlists)} 个歌单    ·    已存在的快捷方式会自动跳过"
        )

    def _request_delete(self) -> None:
        self.delete_requested.emit(self._selected_records())

    def _context_menu(self, pos: QPoint) -> None:
        row = self.table.rowAt(pos.y())
        if row < 0 or self.kind != "music":
            return
        if row not in self._checked_rows():
            self.table.selectRow(row)
        menu = self.create_context_menu(self.selected_records())
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def create_context_menu(self, records: object | None = None) -> QMenu:
        frozen = tuple(
            dict(record)
            for record in (self.selected_records() if records is None else records)
            if isinstance(record, dict)
        )

        def emit(signal: Signal) -> None:
            signal.emit(tuple(dict(record) for record in frozen))

        menu = QMenu(self)
        open_action = menu.addAction("打开所在文件夹")
        rename_action = menu.addAction("重命名")
        lyrics_action = menu.addAction("重新匹配歌词")
        open_action.triggered.connect(lambda _checked=False: emit(self.open_location_requested))
        rename_action.triggered.connect(lambda _checked=False: emit(self.rename_context_requested))
        lyrics_action.triggered.connect(lambda _checked=False: emit(self.rematch_lyrics_requested))
        return menu
