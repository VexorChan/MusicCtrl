from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from dialogs.common import PrototypeDialog, dialog_header
from ui.components import make_status_badge


class LyricsMatchDialog(PrototypeDialog):
    scan_requested = Signal(object)
    candidate_requested = Signal(str)
    candidates_requested = Signal(object)
    ignore_requested = Signal(object)
    unignore_requested = Signal(object)
    cancel_match_requested = Signal(str)
    cancel_scan_requested = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        live_mode: bool = False,
        initial_root: Path | None = None,
    ) -> None:
        super().__init__("歌词匹配", (1020, 660), parent)
        self.live_mode = bool(live_mode)
        self._running = False
        self._close_pending = False
        if self.live_mode:
            self._build_live(initial_root)
            return
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)
        root.addWidget(dialog_header("歌词匹配", "优先处理高置信度候选；所有匹配操作均为模拟。"))

        toolbar = QHBoxLayout()
        auto = QPushButton("自动匹配高置信度项目")
        auto.setObjectName("PrimaryButton")
        toolbar.addWidget(auto)
        toolbar.addWidget(QPushButton("批量处理"))
        toolbar.addWidget(QPushButton("标记忽略"))
        toolbar.addStretch(1)
        root.addLayout(toolbar)

        tabs = QTabWidget()
        tabs.addTab(self._match_panel(False), "未匹配  6")
        tabs.addTab(self._match_panel(True), "重复与冲突  2")
        root.addWidget(tabs, 1)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close = QPushButton("关闭")
        close.clicked.connect(self.close)
        close_row.addWidget(close)
        root.addLayout(close_row)

    def _build_live(self, initial_root: Path | None) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)
        root.addWidget(dialog_header("歌词匹配", "只扫描你明确选择的 LRC 目录；高置信度自动匹配，其余由你确认。"))

        path_row = QHBoxLayout()
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("选择包含 .lrc 的文件夹")
        if initial_root is not None:
            self.path_input.setText(str(initial_root))
        browse = QPushButton("选择文件夹")
        browse.clicked.connect(self._browse_root)
        self.start_button = QPushButton("开始扫描并匹配")
        self.start_button.setObjectName("PrimaryButton")
        self.start_button.clicked.connect(self._start_live_scan)
        path_row.addWidget(self.path_input, 1)
        path_row.addWidget(browse)
        path_row.addWidget(self.start_button)
        root.addLayout(path_row)

        self.summary = QLabel("请选择目录后开始；不会修改歌词或音频文件。")
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)

        self.result_tabs = QTabWidget()
        self.unmatched_results = self._live_results_table()
        self.conflict_results = self._live_results_table()
        self._tables = (self.unmatched_results, self.conflict_results)
        self.results = self.unmatched_results  # 兼容既有只读集成探针
        self.result_tabs.addTab(self.unmatched_results, "未匹配  0")
        self.result_tabs.addTab(self.conflict_results, "重复与冲突  0")
        root.addWidget(self.result_tabs, 1)

        actions = QHBoxLayout()
        self.use_button = QPushButton("批量确认所选歌词")
        self.use_button.setObjectName("PrimaryButton")
        self.use_button.clicked.connect(self._use_selected)
        self.cancel_match_button = QPushButton("取消当前匹配")
        self.cancel_match_button.clicked.connect(self._cancel_selected_match)
        self.ignore_button = QPushButton("标记忽略")
        self.ignore_button.clicked.connect(self._ignore_selected)
        self.unignore_button = QPushButton("取消忽略")
        self.unignore_button.clicked.connect(self._unignore_selected)
        actions.addWidget(self.use_button)
        actions.addWidget(self.cancel_match_button)
        actions.addWidget(self.ignore_button)
        actions.addWidget(self.unignore_button)
        actions.addStretch(1)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.close)
        actions.addWidget(self.close_button)
        root.addLayout(actions)
        self._update_live_actions()

    def _live_results_table(self) -> QTableWidget:
        table = QTableWidget(0, 6)
        table.setHorizontalHeaderLabels(["选择", "音频", "歌词候选", "置信度", "状态", "说明"])
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(True)
        table.setColumnWidth(0, 54)
        table.setColumnWidth(1, 200)
        table.setColumnWidth(2, 220)
        table.setColumnWidth(3, 72)
        table.setColumnWidth(4, 110)
        table.itemSelectionChanged.connect(self._update_live_actions)
        table.itemChanged.connect(lambda _item: self._update_live_actions())
        return table

    def _browse_root(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择 LRC 歌词目录", self.path_input.text())
        if selected:
            self.path_input.setText(selected)

    def _start_live_scan(self) -> None:
        text = self.path_input.text().strip()
        if not text:
            self.show_warning("请先选择 LRC 歌词目录")
            return
        path = Path(text)
        if not path.is_absolute():
            self.show_warning("歌词目录必须是绝对路径")
            return
        self.scan_requested.emit(path)

    def set_running(self, running: bool) -> None:
        if not self.live_mode:
            return
        self._running = bool(running)
        self.path_input.setEnabled(not running)
        self.start_button.setEnabled(not running)
        self.start_button.setText("正在扫描…" if running else "开始扫描并匹配")
        if running:
            for table in self._tables:
                table.setRowCount(0)
            self.summary.setText("正在后台扫描、索引并匹配；关闭窗口会安全取消。")
        self._update_live_actions()
        if not running and self._close_pending:
            self._close_pending = False
            self.close()

    def show_results(self, result: object) -> None:
        if not self.live_mode:
            return
        items = tuple(getattr(result, "items", ()))
        unmatched = tuple(item for item in items if getattr(item, "status", "") not in {"冲突", "已忽略"})
        conflicts = tuple(item for item in items if getattr(item, "status", "") in {"冲突", "已忽略"})
        self._populate_live_table(self.unmatched_results, unmatched)
        self._populate_live_table(self.conflict_results, conflicts)
        self.result_tabs.setTabText(0, f"未匹配  {len(unmatched)}")
        self.result_tabs.setTabText(1, f"重复与冲突  {len(conflicts)}")
        self.summary.setText(
            f"已索引 {getattr(result, 'indexed_count', 0)} 个 LRC，"
            f"自动匹配 {getattr(result, 'automatic_count', 0)} 项；其余请人工确认。"
        )
        self._update_live_actions()

    def _populate_live_table(self, table: QTableWidget, items: tuple[object, ...]) -> None:
        table.blockSignals(True)
        table.setRowCount(len(items))
        for row, item in enumerate(items):
            candidate = "内嵌歌词" if getattr(item, "source_kind", "") == "embedded" else (
                "—" if getattr(item, "lyric_path", None) is None else Path(item.lyric_path).name
            )
            values = (
                "",
                getattr(item, "audio_label", ""),
                candidate,
                f"{getattr(item, 'confidence', 0)}%",
                getattr(item, "status", ""),
                getattr(item, "message", ""),
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setData(Qt.ItemDataRole.UserRole, getattr(item, "token", ""))
                cell.setData(Qt.ItemDataRole.UserRole + 1, getattr(item, "audio_asset_id", ""))
                cell.setData(Qt.ItemDataRole.UserRole + 2, bool(getattr(item, "lyric_asset_id", None)))
                cell.setData(
                    Qt.ItemDataRole.UserRole + 3,
                    bool(getattr(item, "requires_confirmation", False)),
                )
                cell.setData(
                    Qt.ItemDataRole.UserRole + 4,
                    bool(getattr(item, "has_current_match", False)),
                )
                cell.setData(Qt.ItemDataRole.UserRole + 5, bool(getattr(item, "ignored", False)))
                if column == 0:
                    cell.setFlags(cell.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    cell.setCheckState(Qt.CheckState.Unchecked)
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                table.setItem(row, column, cell)
        table.blockSignals(False)

    def show_warning(self, message: str) -> None:
        if self.live_mode:
            self.summary.setText(message)

    def _selected_cells(self) -> tuple[QTableWidgetItem, ...]:
        if not self.live_mode:
            return ()
        cells: list[QTableWidgetItem] = []
        seen: set[tuple[int, int]] = set()
        for table_index, table in enumerate(self._tables):
            rows = {index.row() for index in table.selectionModel().selectedRows()}
            rows.update(
                row for row in range(table.rowCount())
                if table.item(row, 0).checkState() == Qt.CheckState.Checked
            )
            for row in sorted(rows):
                key = (table_index, row)
                if key not in seen:
                    seen.add(key)
                    cells.append(table.item(row, 0))
        return tuple(cells)

    def _selected_cell(self) -> QTableWidgetItem | None:
        cells = self._selected_cells()
        return cells[0] if len(cells) == 1 else None

    def _update_live_actions(self) -> None:
        if not self.live_mode:
            return
        cells = self._selected_cells()
        manual = tuple(
            cell for cell in cells
            if cell.data(Qt.ItemDataRole.UserRole + 2)
            and cell.data(Qt.ItemDataRole.UserRole + 3)
            and not cell.data(Qt.ItemDataRole.UserRole + 5)
        )
        self.use_button.setEnabled(not self._running and bool(manual) and len(manual) == len(cells))
        self.cancel_match_button.setEnabled(
            not self._running
            and len(cells) == 1
            and bool(cells[0].data(Qt.ItemDataRole.UserRole + 4))
        )
        self.ignore_button.setEnabled(
            not self._running and bool(cells)
            and any(not cell.data(Qt.ItemDataRole.UserRole + 5) for cell in cells)
        )
        self.unignore_button.setEnabled(
            not self._running and bool(cells)
            and all(cell.data(Qt.ItemDataRole.UserRole + 5) for cell in cells)
        )

    def _use_selected(self) -> None:
        cells = self._selected_cells()
        tokens = tuple(str(cell.data(Qt.ItemDataRole.UserRole)) for cell in cells)
        if tokens:
            self.candidates_requested.emit(tokens)
            if len(tokens) == 1:
                self.candidate_requested.emit(tokens[0])

    def _cancel_selected_match(self) -> None:
        cell = self._selected_cell()
        if cell is not None:
            self.cancel_match_requested.emit(str(cell.data(Qt.ItemDataRole.UserRole + 1)))

    def _selected_audio_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            str(cell.data(Qt.ItemDataRole.UserRole + 1))
            for cell in self._selected_cells()
        ))

    def _ignore_selected(self) -> None:
        audio_ids = self._selected_audio_ids()
        if audio_ids:
            self.ignore_requested.emit(audio_ids)

    def _unignore_selected(self) -> None:
        audio_ids = self._selected_audio_ids()
        if audio_ids:
            self.unignore_requested.emit(audio_ids)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.live_mode and self._running:
            self._close_pending = True
            self.cancel_scan_requested.emit()
            event.ignore()
            return
        super().closeEvent(event)

    def _match_panel(self, conflict: bool) -> QWidget:
        panel = QWidget()
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(14, 12, 14, 12)
        heading = QLabel("音频文件")
        heading.setObjectName("SectionTitle")
        left_layout.addWidget(heading)
        audio_list = QListWidget()
        names = ["晴天-周杰伦.mp3", "富士山下-陈奕迅.m4a", "必杀技-古巨基.mp3", "光年之外-G.E.M.邓紫棋.flac"]
        if conflict:
            names = ["七里香-周杰伦.flac", "珊瑚海-周杰伦、梁心颐.mp3"]
        audio_list.addItems(names)
        audio_list.setCurrentRow(0)
        left_layout.addWidget(audio_list, 1)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(18, 12, 18, 12)
        title = QLabel("晴天-周杰伦.mp3" if not conflict else "七里香-周杰伦.flac")
        title.setStyleSheet("font-size:17px;font-weight:600")
        right_layout.addWidget(title)
        status_row = QHBoxLayout()
        status_row.addWidget(QLabel("当前状态"))
        status_row.addWidget(make_status_badge("未匹配" if not conflict else "冲突"))
        status_row.addStretch(1)
        right_layout.addLayout(status_row)
        right_layout.addSpacing(6)
        candidates_title = QLabel("歌词候选")
        candidates_title.setObjectName("SectionTitle")
        right_layout.addWidget(candidates_title)
        candidates = QListWidget()
        for text in (["晴天-周杰伦.lrc                         98%", "周杰伦-晴天.lrc                         92%", "晴天 Live.lrc                              71%"] if not conflict else ["七里香-周杰伦.lrc                      98%", "七里香 (修订版).lrc                    86%"]):
            candidates.addItem(QListWidgetItem(text))
        candidates.setCurrentRow(0)
        right_layout.addWidget(candidates)
        preview = QLabel("[00:15.20] 故事的小黄花\n[00:18.56] 从出生那年就飘着\n[00:22.04] 童年的荡秋千……")
        preview.setStyleSheet("background:#f7f7f7;border:1px solid #e1e1e1;border-radius:4px;padding:12px;color:#555")
        right_layout.addWidget(preview)
        buttons = QHBoxLayout()
        buttons.addWidget(QPushButton("查看歌词文本"))
        buttons.addWidget(QPushButton("取消已有匹配"))
        buttons.addStretch(1)
        use = QPushButton("使用所选歌词")
        use.setObjectName("PrimaryButton")
        buttons.addWidget(use)
        right_layout.addLayout(buttons)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([320, 620])
        layout.addWidget(splitter)
        return panel
