from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtGui import QMouseEvent, QResizeEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHeaderView,
    QTableView,
    QTableWidget,
)


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
    """Model/View table used by the production all-music page."""

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

    def require_checkable_header(self) -> CheckableHeaderView:
        if self._checkable_header is None:
            raise RuntimeError("此表格未启用可勾选表头")
        return self._checkable_header
