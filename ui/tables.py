from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QResizeEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHeaderView,
    QStyle,
    QStyledItemDelegate,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
)


class ItemTableModel(QAbstractTableModel):
    """Small QTableWidgetItem-compatible model for production record tables."""

    item_changed = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._headers: list[str] = []
        self._items: list[list[QTableWidgetItem | None]] = []

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._items)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._headers)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        item = self.item(index.row(), index.column())
        if item is None:
            return None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return item.text()
        return item.data(role)

    def setData(self, index: QModelIndex, value, role=Qt.ItemDataRole.EditRole) -> bool:
        if not index.isValid():
            return False
        item = self.item(index.row(), index.column())
        if item is None:
            item = QTableWidgetItem()
            self._items[index.row()][index.column()] = item
        if role == Qt.ItemDataRole.EditRole:
            item.setText(str(value))
        elif role == Qt.ItemDataRole.CheckStateRole:
            item.setCheckState(Qt.CheckState(value))
        else:
            item.setData(role, value)
        self.dataChanged.emit(index, index, [role])
        self.item_changed.emit(item)
        return True

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        item = self.item(index.row(), index.column()) if index.isValid() else None
        return Qt.ItemFlag.NoItemFlags if item is None else item.flags()

    def headerData(self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole):
        if (
            orientation == Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
            and 0 <= section < len(self._headers)
        ):
            return self._headers[section]
        return super().headerData(section, orientation, role)

    def set_column_count(self, count: int) -> None:
        count = max(0, int(count))
        self.beginResetModel()
        self._headers = (self._headers + [""] * count)[:count]
        self._items = [(row + [None] * count)[:count] for row in self._items]
        self.endResetModel()

    def set_headers(self, labels: list[str]) -> None:
        self.beginResetModel()
        self._headers = list(labels)
        count = len(self._headers)
        self._items = [(row + [None] * count)[:count] for row in self._items]
        self.endResetModel()

    def set_row_count(self, count: int) -> None:
        count = max(0, int(count))
        columns = len(self._headers)
        self.beginResetModel()
        self._items = (self._items + [[None] * columns for _ in range(count)])[:count]
        self.endResetModel()

    def insert_row(self, row: int) -> None:
        row = min(max(0, int(row)), len(self._items))
        self.beginInsertRows(QModelIndex(), row, row)
        self._items.insert(row, [None] * len(self._headers))
        self.endInsertRows()

    def set_item(self, row: int, column: int, item: QTableWidgetItem) -> None:
        if not (0 <= row < len(self._items) and 0 <= column < len(self._headers)):
            raise IndexError("表格单元格越界")
        self._items[row][column] = item
        index = self.index(row, column)
        self.dataChanged.emit(index, index)

    def item(self, row: int, column: int) -> QTableWidgetItem | None:
        if not (0 <= row < len(self._items) and 0 <= column < len(self._headers)):
            return None
        return self._items[row][column]

    def clear_items(self) -> None:
        self.beginResetModel()
        self._headers = []
        self._items = []
        self.endResetModel()


class ModelItemProxy(QTableWidgetItem):
    """Compatibility adapter for legacy callers reading a model-backed cell."""

    def __init__(self, model: QAbstractTableModel, index: QModelIndex) -> None:
        super().__init__()
        self._model = model
        self._index = index

    def text(self) -> str:
        return str(self._model.data(self._index, Qt.ItemDataRole.DisplayRole) or "")

    def setText(self, text: str) -> None:
        self._model.setData(self._index, text, Qt.ItemDataRole.EditRole)

    def checkState(self) -> Qt.CheckState:
        value = self._model.data(self._index, Qt.ItemDataRole.CheckStateRole)
        return Qt.CheckState.Unchecked if value is None else Qt.CheckState(value)

    def setCheckState(self, state: Qt.CheckState) -> None:
        self._model.setData(self._index, state, Qt.ItemDataRole.CheckStateRole)

    def flags(self) -> Qt.ItemFlag:
        return self._model.flags(self._index)

    def data(self, role: int):
        return self._model.data(self._index, role)

class CheckableHeaderView(QHeaderView):
    """在第一列表头绘制并管理与行复选框一致的原生三态控件。"""

    toggle_requested = Signal()
    _ROW_INDICATOR_OFFSET = -4

    def __init__(self, orientation: Qt.Orientation, parent=None) -> None:
        super().__init__(orientation, parent)
        self._check_state = Qt.CheckState.Unchecked
        self.checkbox = QCheckBox(self.viewport())
        self.checkbox.setObjectName("HeaderCheckBox")
        self.checkbox.setAccessibleName("全选当前列表")
        self.checkbox.setTristate(True)
        self.checkbox.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.checkbox.setFixedSize(16, 16)
        self.checkbox.clicked.connect(lambda _checked=False: self.toggle_requested.emit())
        self.sectionResized.connect(lambda *_args: self._update_checkbox_geometry())
        self.sectionMoved.connect(lambda *_args: self._update_checkbox_geometry())
        self.geometriesChanged.connect(self._update_checkbox_geometry)
        self._update_checkbox_geometry()

    def check_state(self) -> Qt.CheckState:
        return self._check_state

    def set_check_state(self, state: Qt.CheckState) -> None:
        state = Qt.CheckState(state)
        self._check_state = state
        if self.checkbox.checkState() != state:
            blocker = QSignalBlocker(self.checkbox)
            self.checkbox.setCheckState(state)
            del blocker

    def _update_checkbox_geometry(self) -> None:
        if self.count() == 0 or self.isSectionHidden(0):
            self.checkbox.hide()
            return
        x = (
            self.sectionViewportPosition(0)
            + (self.sectionSize(0) - self.checkbox.width()) // 2
            + self._ROW_INDICATOR_OFFSET
        )
        y = (self.height() - self.checkbox.height()) // 2
        self.checkbox.move(x, y)
        self.checkbox.show()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_checkbox_geometry()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.logicalIndexAt(event.position().toPoint()) == 0
        ):
            self.toggle_requested.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class StatusBadgeDelegate(QStyledItemDelegate):
    """Paint compact status text without installing one widget per table cell."""

    def paint(self, painter: QPainter, option, index) -> None:
        status = str(index.data() or index.data(Qt.ItemDataRole.UserRole) or "")
        if not status:
            super().paint(painter, option, index)
            return
        colors = {
            "失败": ("#fde8e8", "#9b1c1c"),
            "文件缺失": ("#fde8e8", "#9b1c1c"),
            "快捷方式损坏": ("#fde8e8", "#9b1c1c"),
            "冲突": ("#fff1d6", "#8a4b08"),
            "外部变化": ("#fff1d6", "#8a4b08"),
            "目标未索引": ("#fff1d6", "#8a4b08"),
            "已忽略": ("#eeeeee", "#555555"),
            "未匹配": ("#fff1d6", "#8a4b08"),
        }
        background, foreground = colors.get(status, ("#e7f5ec", "#176b3a"))
        painter.save()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
        badge = option.rect.adjusted(8, 8, -8, -8)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(background))
        painter.drawRoundedRect(badge, 8, 8)
        painter.setPen(QColor(foreground))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, status)
        painter.restore()


class DataTable(QTableWidget):
    def __init__(self, parent=None, *, checkable_header: bool = False) -> None:
        super().__init__(parent)
        self._checkable_header: CheckableHeaderView | None = None
        if checkable_header:
            self._checkable_header = CheckableHeaderView(Qt.Orientation.Horizontal, self)
            self.setHorizontalHeader(self._checkable_header)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(40)
        self.horizontalHeader().setHighlightSections(False)
        self.horizontalHeader().setSectionsClickable(True)
        self.horizontalHeader().setSortIndicatorShown(False)
        # 页面层负责排序模拟数据。禁止 QTableWidget 独立重排行，
        # 否则界面行号会与页面持有的数据顺序失去对应关系。
        self.setSortingEnabled(False)

    def checkable_header(self) -> CheckableHeaderView | None:
        return self._checkable_header

    def require_checkable_header(self) -> CheckableHeaderView:
        if self._checkable_header is None:
            raise RuntimeError("此表格未启用可勾选表头")
        return self._checkable_header


class ModelDataTable(QTableView):
    """Model/View table used by production pages and record dialogs."""

    itemChanged = Signal(object)
    itemSelectionChanged = Signal()

    def __init__(self, parent=None, *, checkable_header: bool = False) -> None:
        super().__init__(parent)
        self._checkable_header: CheckableHeaderView | None = None
        if checkable_header:
            self._checkable_header = CheckableHeaderView(Qt.Orientation.Horizontal, self)
            self.setHorizontalHeader(self._checkable_header)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(40)
        self.horizontalHeader().setHighlightSections(False)
        self.horizontalHeader().setSectionsClickable(True)
        self.horizontalHeader().setSortIndicatorShown(False)

    def setModel(self, model) -> None:
        super().setModel(model)
        selection_model = self.selectionModel()
        if selection_model is not None:
            selection_model.selectionChanged.connect(
                lambda _selected, _deselected: self.itemSelectionChanged.emit()
            )
        if isinstance(model, ItemTableModel):
            model.item_changed.connect(self.itemChanged.emit)

    def _item_model(self) -> ItemTableModel:
        model = self.model()
        if not isinstance(model, ItemTableModel):
            model = ItemTableModel(self)
            self.setModel(model)
        return model

    def setColumnCount(self, count: int) -> None:
        self._item_model().set_column_count(count)

    def setHorizontalHeaderLabels(self, labels) -> None:
        self._item_model().set_headers([str(label) for label in labels])

    def setRowCount(self, count: int) -> None:
        self._item_model().set_row_count(count)

    def insertRow(self, row: int) -> None:
        self._item_model().insert_row(row)

    def setItem(self, row: int, column: int, item: QTableWidgetItem) -> None:
        self._item_model().set_item(row, column, item)

    def item(self, row: int, column: int) -> QTableWidgetItem | None:
        model = self.model()
        if isinstance(model, ItemTableModel):
            return model.item(row, column)
        if model is None or not (0 <= row < model.rowCount() and 0 <= column < model.columnCount()):
            return None
        return ModelItemProxy(model, model.index(row, column))

    def rowCount(self) -> int:
        model = self.model()
        return 0 if model is None else model.rowCount()

    def columnCount(self) -> int:
        model = self.model()
        return 0 if model is None else model.columnCount()

    def horizontalHeaderItem(self, column: int) -> QTableWidgetItem | None:
        model = self.model()
        if model is None or not 0 <= column < model.columnCount():
            return None
        value = model.headerData(
            column, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole
        )
        return QTableWidgetItem("" if value is None else str(value))

    def clear(self) -> None:
        self._item_model().clear_items()

    def require_checkable_header(self) -> CheckableHeaderView:
        if self._checkable_header is None:
            raise RuntimeError("此表格未启用可勾选表头")
        return self._checkable_header
