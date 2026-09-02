"""PySide6 GUI for Notion PDF Exporter."""

import inspect
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QSettings, QThread, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedLayout,
    QStackedWidget,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .utils import extract_page_id

try:
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWebEngineCore import QWebEngineSettings
except Exception as exc:  # pragma: no cover
    raise RuntimeError(
        "PySide6 WebEngine is required. Install with: pip install PySide6"
    ) from exc

from .api import NotionClient
from .cache import PageDataCache
from .cli import resolve_token
from .renderer import PageRenderer

LOGGER = logging.getLogger(__name__)
SIDEBAR_MIN_WIDTH = 360


def _setup_logging() -> None:
    log_dir = Path.home() / ".notion_pdf" / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"gui_{time.strftime('%Y%m%d_%H%M%S')}.log"
        handler = logging.FileHandler(log_file, encoding="utf-8")
    except OSError:
        log_dir = Path(tempfile.gettempdir()) / "notion_pdf" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"gui_{time.strftime('%Y%m%d_%H%M%S')}.log"
        handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    # Also log to stderr so console output remains visible.
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root.addHandler(console)
    LOGGER.info("Logging to %s", log_file)


_setup_logging()


class Worker(QThread):
    """Runs a blocking job and emits the result, progress, or an error."""

    finished = Signal(object)
    error = Signal(str)
    progress = Signal(str, object)

    def __init__(self, job, *args, parent=None, **kwargs):
        super().__init__(parent)
        self.job = job
        self.args = args
        self.kwargs = kwargs

    def run(self):
        try:
            kwargs = dict(self.kwargs)
            sig = inspect.signature(self.job)
            if "progress_callback" in sig.parameters:
                kwargs["progress_callback"] = lambda stage, data: self.progress.emit(stage, data)
            result = self.job(*self.args, **kwargs)
            self.finished.emit(result)
        except Exception as exc:
            LOGGER.exception("Worker job failed")
            self.error.emit(str(exc))


class Card(QFrame):
    """A rounded card widget for the Image-2-style layout."""

    def __init__(
        self,
        title: str | None = None,
        right_widget: QWidget | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)
        if title or right_widget:
            header = QHBoxLayout()
            header.setSpacing(8)
            header.setContentsMargins(0, 0, 0, 0)
            if title:
                label = QLabel(title)
                label.setObjectName("cardTitle")
                header.addWidget(label)
            header.addStretch()
            if right_widget:
                header.addWidget(right_widget)
            layout.addLayout(header)
        self.layout = layout


class NotionPdfWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Notion PDF 导出工具")
        self.setMinimumSize(1280, 800)
        self.resize(1440, 900)

        self.settings = QSettings("Bertie", "NotionPDF")
        self.renderer: PageRenderer | None = None
        self.page_data: dict | None = None
        self._cached_renderer: PageRenderer | None = None
        self._cached_renderer_key: str | None = None
        self._loaded_url: str | None = None
        self._load_busy = False
        self._preview_busy = False
        self._preview_pending = False
        self._export_busy = False
        self.db_controls: dict[str, dict[str, QWidget]] = {}
        self.page_cache = PageDataCache()
        self._has_cached_page = False
        self._background_refresh = False
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._do_preview)
        self._toolbar_hide_timer = QTimer(self)
        self._toolbar_hide_timer.setSingleShot(True)
        self._toolbar_hide_timer.timeout.connect(self._hide_preview_toolbar)

        self._build_ui()
        self._apply_styles()
        self._connect_signals()
        self._load_settings()

    def closeEvent(self, event):
        self._save_settings()
        event.accept()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        main_layout.addWidget(splitter)

        # --- Left panel: controls ---
        left_content = QWidget()
        left_layout = QVBoxLayout(left_content)
        left_layout.setSpacing(18)
        left_layout.setContentsMargins(16, 20, 16, 20)
        left_layout.setAlignment(Qt.AlignTop)

        header = QLabel("Notion → PDF")
        header.setObjectName("appHeader")
        header.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        left_layout.addWidget(header)

        # Notion URL
        url_card = Card("Notion 页面链接")
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("https://www.notion.so/...")
        url_card.layout.addWidget(self.url_input)
        left_layout.addWidget(url_card)

        # Token source
        self.token_source_check = QCheckBox("使用令牌文件")
        self.token_source_check.setChecked(True)
        self.token_source_check.stateChanged.connect(self._on_token_source_changed)
        token_card = Card("访问令牌", right_widget=self.token_source_check)
        token_card.setFixedHeight(132)
        token_vlayout = QVBoxLayout()
        token_vlayout.setSpacing(10)
        token_vlayout.setContentsMargins(0, 0, 0, 0)

        self.token_stack = QStackedWidget()
        self.token_stack.setFixedHeight(42)

        # File page
        file_page = QWidget()
        file_layout = QHBoxLayout(file_page)
        file_layout.setSpacing(8)
        file_layout.setContentsMargins(0, 0, 0, 0)
        self.token_file_input = QLineEdit()
        self.token_file_input.setPlaceholderText("选择包含 Notion token 的 .txt 文件")
        token_btn = QPushButton("浏览")
        token_btn.setObjectName("secondaryBtn")
        token_btn.setFixedWidth(80)
        token_btn.clicked.connect(self._browse_token)
        file_layout.addWidget(self.token_file_input)
        file_layout.addWidget(token_btn)
        self.token_stack.addWidget(file_page)

        # Paste page
        paste_page = QWidget()
        paste_layout = QVBoxLayout(paste_page)
        paste_layout.setSpacing(4)
        paste_layout.setContentsMargins(0, 0, 0, 0)
        self.token_paste_input = QLineEdit()
        self.token_paste_input.setEchoMode(QLineEdit.Password)
        self.token_paste_input.setPlaceholderText("secret_...")
        paste_layout.addWidget(self.token_paste_input)
        self.token_stack.addWidget(paste_page)

        token_vlayout.addWidget(self.token_stack)
        token_card.layout.addLayout(token_vlayout)
        left_layout.addWidget(token_card)

        # Page settings
        opts_card = Card("页面设置")
        opts_layout = QVBoxLayout()
        opts_layout.setSpacing(12)
        opts_layout.setContentsMargins(0, 0, 0, 0)

        row1 = QHBoxLayout()
        self.full_width_cb = QCheckBox("全宽页面")
        row1.addWidget(self.full_width_cb)
        row1.addStretch()
        opts_layout.addLayout(row1)

        page_form = QFormLayout()
        page_form.setSpacing(10)
        page_form.setContentsMargins(0, 0, 0, 0)
        page_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.orientation_combo = QComboBox()
        self.orientation_combo.addItem("自动", "auto")
        self.orientation_combo.addItem("纵向", "portrait")
        self.orientation_combo.addItem("横向", "landscape")
        self.margin_spin = QDoubleSpinBox()
        self.margin_spin.setRange(5, 50)
        self.margin_spin.setValue(15)
        self.margin_spin.setDecimals(1)
        self.margin_spin.setSuffix(" mm")
        page_form.addRow("方向：", self.orientation_combo)
        page_form.addRow("页边距：", self.margin_spin)
        opts_layout.addLayout(page_form)

        opts_card.layout.addLayout(opts_layout)
        left_layout.addWidget(opts_card)

        database_card = Card("数据库设置")
        self.db_tabs = QTabWidget()
        self.db_tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.db_tabs.setFixedHeight(190)
        empty_db = QLabel("加载页面后，可分别设置每个数据库。")
        empty_db.setAlignment(Qt.AlignCenter)
        self.db_tabs.addTab(empty_db, "数据库")
        database_card.layout.addWidget(self.db_tabs)
        left_layout.addWidget(database_card)

        # Status log
        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setWordWrap(True)
        self.status_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        left_layout.addWidget(self.status_label)

        left_layout.addStretch()
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setMinimumWidth(SIDEBAR_MIN_WIDTH)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        left_scroll.setFrameShape(QFrame.NoFrame)
        left_scroll.setWidget(left_content)
        splitter.addWidget(left_scroll)
        splitter.setCollapsible(0, False)

        # --- Right panel: preview ---
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        preview_header = QLabel("实时预览")
        preview_header.setObjectName("previewHeader")
        right_layout.addWidget(preview_header)

        # Preview container with floating export button overlay
        preview_container = QWidget()
        self.preview_container = preview_container
        preview_stack = QStackedLayout(preview_container)
        preview_stack.setStackingMode(QStackedLayout.StackAll)
        preview_stack.setContentsMargins(0, 0, 0, 0)

        preview_widget = QWidget()
        preview_widget_layout = QVBoxLayout(preview_widget)
        preview_widget_layout.setContentsMargins(0, 0, 0, 0)
        preview_widget_layout.setSpacing(0)
        self.preview = QWebEngineView()
        self.preview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        settings = self.preview.settings()
        if settings:
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True
            )
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True
            )
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.AutoLoadImages, True
            )
        preview_widget_layout.addWidget(self.preview)

        preview_stack.addWidget(preview_widget)
        self.preview_toolbar = QFrame(preview_container)
        self.preview_toolbar.setObjectName("previewToolbar")
        toolbar_layout = QHBoxLayout(self.preview_toolbar)
        toolbar_layout.setContentsMargins(6, 6, 6, 6)
        toolbar_layout.setSpacing(4)
        self.refresh_btn = QToolButton()
        self.refresh_btn.setIcon(self._fluent_outline_icon("\ue72c"))
        self.refresh_btn.setToolTip("刷新 Notion 页面")
        self.zoom_out_btn = QToolButton()
        self.zoom_out_btn.setText("−")
        self.zoom_out_btn.setToolTip("缩小预览")
        self.zoom_in_btn = QToolButton()
        self.zoom_in_btn.setText("+")
        self.zoom_in_btn.setToolTip("放大预览")
        self.export_fab = QToolButton()
        self.export_fab.setIcon(self._fluent_outline_icon("\ue74e"))
        self.export_fab.setToolTip("保存 PDF / HTML")
        self.export_fab.setEnabled(False)
        for button in (self.refresh_btn, self.zoom_out_btn, self.zoom_in_btn, self.export_fab):
            button.setCursor(Qt.PointingHandCursor)
            button.setFixedSize(38, 38)
            toolbar_layout.addWidget(button)
        self.preview_toolbar.adjustSize()
        self.preview_toolbar.hide()
        preview_container.installEventFilter(self)
        self.preview.installEventFilter(self)
        self.preview_toolbar.installEventFilter(self)
        right_layout.addWidget(preview_container)
        splitter.addWidget(right)

        splitter.setSizes([SIDEBAR_MIN_WIDTH, 1040])

    def _apply_styles(self):
        self.setStyleSheet("""
            QMainWindow {
                background-color: #f5f5f7;
            }
            #appHeader {
                font-size: 26px;
                font-weight: 700;
                color: #1d1d1f;
                padding-bottom: 8px;
            }
            #card {
                background-color: #ffffff;
                border-radius: 16px;
                border: 1px solid #e5e5ea;
            }
            #cardTitle {
                font-size: 13px;
                font-weight: 600;
                color: #6e6e73;
                text-transform: uppercase;
                letter-spacing: 0.5px;
                margin-bottom: 4px;
            }
            QLineEdit, QComboBox, QDoubleSpinBox {
                padding: 10px 12px;
                border: 1px solid #d2d2d7;
                border-radius: 10px;
                background-color: #fafafc;
                font-size: 13px;
                color: #1d1d1f;
            }
            QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus {
                border: 1.5px solid #0071e3;
            }
            QComboBox::drop-down {
                border: none;
                width: 24px;
            }
            QCheckBox {
                font-size: 13px;
                color: #1d1d1f;
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border-radius: 5px;
                border: 1px solid #d2d2d7;
                background-color: #fafafc;
            }
            QCheckBox::indicator:checked {
                background-color: #0071e3;
                border-color: #0071e3;
            }
            #primaryBtn {
                background-color: #0071e3;
                color: white;
                border: none;
                border-radius: 10px;
                padding: 10px 18px;
                font-size: 13px;
                font-weight: 600;
            }
            #primaryBtn:hover {
                background-color: #0077ed;
            }
            #primaryBtn:disabled {
                background-color: #d2d2d7;
                color: #8e8e93;
            }
            #secondaryBtn {
                background-color: #e8e8ed;
                color: #1d1d1f;
                border: none;
                border-radius: 10px;
                padding: 8px 14px;
                font-size: 13px;
                font-weight: 600;
            }
            #secondaryBtn:hover {
                background-color: #dedee3;
            }
            #fabBtn {
                background-color: #0071e3;
                color: white;
                border: none;
                border-radius: 24px;
                padding: 14px 28px;
                font-size: 14px;
                font-weight: 600;
                box-shadow: 0 4px 14px rgba(0, 113, 227, 0.35);
            }
            #fabBtn:hover {
                background-color: #0077ed;
            }
            #fabBtn:disabled {
                background-color: #d2d2d7;
                color: #8e8e93;
                box-shadow: none;
            }
            #previewToolbar {
                background-color: rgba(32, 32, 35, 225);
                border: 1px solid #4a4a4f;
                border-radius: 8px;
            }
            #previewToolbar QToolButton {
                color: white;
                background: transparent;
                border: none;
                border-radius: 5px;
                font-size: 22px;
            }
            #previewToolbar QToolButton:hover { background-color: #505056; }
            #previewToolbar QToolButton:disabled { color: #77777c; }
            #statusLabel {
                font-size: 12px;
                color: #6e6e73;
                padding-top: 4px;
            }
            #previewHeader {
                background-color: #ffffff;
                color: #1d1d1f;
                font-size: 13px;
                font-weight: 600;
                padding: 14px 20px;
                border-bottom: 1px solid #e5e5ea;
            }
            QLabel {
                font-size: 13px;
                color: #1d1d1f;
            }
        """)

    def _connect_signals(self):
        self.export_fab.clicked.connect(self._export_pdf)
        self.refresh_btn.clicked.connect(self._refresh_page)
        self.zoom_in_btn.clicked.connect(lambda: self._change_preview_zoom(0.1))
        self.zoom_out_btn.clicked.connect(lambda: self._change_preview_zoom(-0.1))
        self.url_input.editingFinished.connect(self._maybe_auto_load)
        self.token_file_input.editingFinished.connect(self._maybe_auto_load)
        self.token_paste_input.editingFinished.connect(self._maybe_auto_load)

        # Auto-preview on parameter changes (debounced)
        for widget in (
            self.full_width_cb,
            self.orientation_combo,
            self.margin_spin,
        ):
            if isinstance(widget, QLineEdit):
                widget.textChanged.connect(self._schedule_preview)
            elif isinstance(widget, QComboBox):
                if widget.isEditable():
                    widget.currentTextChanged.connect(self._schedule_preview)
                else:
                    widget.currentIndexChanged.connect(self._schedule_preview)
            elif isinstance(widget, QDoubleSpinBox):
                widget.valueChanged.connect(self._schedule_preview)
            elif isinstance(widget, QCheckBox):
                widget.stateChanged.connect(self._schedule_preview)

    @staticmethod
    def _fluent_outline_icon(glyph: str, size: int = 22) -> QIcon:
        result = QPixmap(size, size)
        result.fill(Qt.transparent)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.TextAntialiasing)
        font = QFont("Segoe Fluent Icons", 15)
        if not QFont("Segoe Fluent Icons").exactMatch():
            font = QFont("Segoe MDL2 Assets", 15)
        painter.setFont(font)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(result.rect(), Qt.AlignCenter, glyph)
        painter.end()
        return QIcon(result)

    def eventFilter(self, watched, event):
        if watched in (self.preview_container, self.preview, self.preview_toolbar):
            if event.type() == QEvent.Resize and watched is self.preview_container:
                self._position_preview_toolbar()
            elif event.type() == QEvent.Enter:
                self._toolbar_hide_timer.stop()
                self.preview_toolbar.show()
                self.preview_toolbar.raise_()
                self._position_preview_toolbar()
            elif event.type() == QEvent.Leave:
                self._toolbar_hide_timer.start(250)
        return super().eventFilter(watched, event)

    def _position_preview_toolbar(self):
        self.preview_toolbar.adjustSize()
        x = max(12, self.preview_container.width() - self.preview_toolbar.width() - 20)
        y = max(12, self.preview_container.height() - self.preview_toolbar.height() - 20)
        self.preview_toolbar.move(x, y)

    def _hide_preview_toolbar(self):
        if not any(widget.underMouse() for widget in (
            self.preview_container, self.preview, self.preview_toolbar
        )):
            self.preview_toolbar.hide()

    def _change_preview_zoom(self, delta: float):
        zoom = min(2.0, max(0.5, self.preview.zoomFactor() + delta))
        self.preview.setZoomFactor(zoom)

    def _browse_token(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择令牌文件", "", "文本文件 (*.txt);;所有文件 (*.*)"
        )
        if path:
            self.token_file_input.setText(path)
            self._maybe_auto_load()

    def _on_token_source_changed(self, state: int):
        self.token_stack.setCurrentIndex(0 if self.token_source_check.isChecked() else 1)

    def _load_settings(self):
        self.token_source_check.setChecked(
            self.settings.value("token_source_is_file", True) in (True, "true", "1")
        )
        self.token_file_input.setText(self.settings.value("token_file_path", ""))
        self.token_paste_input.setText(self.settings.value("token_paste", ""))
        self.full_width_cb.setChecked(self.settings.value("full_width", False) in (True, "true", "1"))
        orientation = self.settings.value("orientation", "auto")
        self.orientation_combo.setCurrentIndex(max(0, self.orientation_combo.findData(orientation)))
        self.margin_spin.setValue(float(self.settings.value("margin", 15)))
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)

    def _save_settings(self):
        self.settings.setValue("token_source_is_file", self.token_source_check.isChecked())
        self.settings.setValue("token_file_path", self.token_file_input.text())
        self.settings.setValue("token_paste", self.token_paste_input.text())
        self.settings.setValue("full_width", self.full_width_cb.isChecked())
        self.settings.setValue("orientation", self.orientation_combo.currentData())
        self.settings.setValue("margin", self.margin_spin.value())
        self.settings.setValue("geometry", self.saveGeometry())

    def _safe_filename(self, name: str) -> str:
        import re
        cleaned = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
        return cleaned or "notion_export"

    def _page_title_from_data(self, data: dict) -> str:
        page = data.get("page", {})
        props = page.get("properties", {})
        for prop in props.values():
            if prop.get("type") == "title":
                title_parts = prop.get("title", [])
                if title_parts:
                    return "".join(part.get("plain_text", "") for part in title_parts) or "Notion Export"
        return "Notion Export"

    def _maybe_auto_load(self):
        url = self.url_input.text().strip()
        if not url:
            return
        if self._loaded_url == url:
            return
        token = self._current_token()
        if not token:
            return
        try:
            extract_page_id(url)
        except Exception:
            return
        self._load_page()

    def _set_status(self, text: str):
        self.status_label.setText(text)
        LOGGER.info(text)

    def _params(self) -> dict[str, Any]:
        return {
            "orientation": self.orientation_combo.currentData(),
            "full_width": self.full_width_cb.isChecked(),
            "database_options": self._database_options(),
        }

    def _current_token(self) -> str:
        if self.token_source_check.isChecked():
            return self.token_file_input.text().strip()
        return self.token_paste_input.text().strip()

    def _make_renderer(self) -> PageRenderer:
        token = resolve_token(self._current_token())
        key = f"{token}::{self.margin_spin.value()}"
        if self._cached_renderer is None or self._cached_renderer_key != key:
            self._cached_renderer_key = key
            self._cached_renderer = PageRenderer(
                client=NotionClient(token),
                margin_mm=self.margin_spin.value(),
            )
        return self._cached_renderer

    def _refresh_page(self):
        self._load_page(force_refresh=True)

    def _load_page(self, force_refresh: bool = False):
        url = self.url_input.text().strip()
        LOGGER.info("Starting load for URL: %s", url)
        if not url:
            QMessageBox.warning(self, "缺少输入", "请输入 Notion 页面链接。")
            return
        token = self._current_token()
        if not token:
            QMessageBox.warning(self, "缺少输入", "请提供 Notion token。")
            return
        try:
            resolved_token = resolve_token(token)
        except Exception as exc:
            QMessageBox.critical(self, "令牌无效", str(exc))
            return

        if self._load_busy:
            LOGGER.info("Load already in progress, skipping")
            return

        self._loaded_url = url
        self._load_busy = True
        self.refresh_btn.setEnabled(False)
        cached = None if force_refresh else self.page_cache.load(url, resolved_token)
        self._has_cached_page = cached is not None
        self._background_refresh = force_refresh and self.page_data is not None
        if cached:
            self.page_data, saved_at = cached
            self.export_fab.setEnabled(True)
            self._update_database_tabs(self.page_data)
            self._set_status(
                f"已加载本地缓存（{time.strftime('%Y-%m-%d %H:%M', time.localtime(saved_at))}），正在同步…"
            )
            self._do_preview()
        elif self._background_refresh:
            self.export_fab.setEnabled(True)
            self._set_status("正在刷新 Notion 页面…")
        else:
            self.export_fab.setEnabled(False)
            self._set_status("正在从 Notion 加载页面…")

        renderer = self._make_renderer()
        worker = Worker(renderer.fetch_page_data, url, parent=self)
        worker.progress.connect(self._on_load_progress)
        worker.finished.connect(self._on_page_loaded)
        worker.error.connect(self._on_worker_error)
        worker.start()

    def _database_blocks(self, data: dict) -> list[dict]:
        found = []
        def walk(blocks: list[dict]):
            for block in blocks:
                if block.get("type") == "child_database" and block.get("_db"):
                    found.append(block)
                walk(block.get("children", []))
        walk(data.get("blocks", []))
        return found

    def _update_database_tabs(self, data: dict):
        previous = self._database_options()
        self.db_tabs.clear()
        self.db_controls.clear()
        blocks = self._database_blocks(data)
        if not blocks:
            label = QLabel("当前页面没有可配置的数据库。")
            label.setAlignment(Qt.AlignCenter)
            self.db_tabs.addTab(label, "数据库")
            return
        for index, block in enumerate(blocks, 1):
            db_id = block["id"]
            db = block["_db"]
            title = block.get("child_database", {}).get("title") or db.get("name") or f"数据库 {index}"
            page = QWidget()
            form = QFormLayout(page)
            view_combo = QComboBox()
            views = db.get("_views", [])
            for view in views:
                view_combo.addItem(f'{view.get("name", "未命名视图")} ({view.get("type", "table")})', view.get("id"))
            if not views:
                view_combo.addItem("自动识别", None)
            group_combo = QComboBox()
            group_combo.addItem("跟随视图", None)
            group_combo.addItem("不分组", "")
            for name, prop in db.get("properties", {}).items():
                if prop.get("type") in ("select", "multi_select", "status", "people", "checkbox"):
                    group_combo.addItem(name, name)
            fit_combo = QComboBox()
            fit_combo.addItem("跟随视图", None)
            fit_combo.addItem("完整显示（Contain）", True)
            fit_combo.addItem("填充裁剪（Cover）", False)
            size_combo = QComboBox()
            size_combo.addItem("跟随视图", None)
            size_combo.addItem("小", "small")
            size_combo.addItem("中", "medium")
            size_combo.addItem("大", "large")
            form.addRow("视图：", view_combo)
            form.addRow("分组字段：", group_combo)
            form.addRow("媒体适配：", fit_combo)
            form.addRow("卡片尺寸：", size_combo)
            controls = {"view": view_combo, "group": group_combo, "fit": fit_combo, "size": size_combo}
            self.db_controls[db_id] = controls
            saved = previous.get(db_id, {})
            if saved.get("view_id"):
                view_combo.setCurrentIndex(max(0, view_combo.findData(saved["view_id"])))
            if saved.get("group_by"):
                group_combo.setCurrentIndex(max(0, group_combo.findData(saved["group_by"])))
            if "gallery_fit" in saved:
                fit_combo.setCurrentIndex(max(0, fit_combo.findData(saved.get("gallery_fit"))))
            size_combo.setCurrentIndex(max(0, size_combo.findData(saved.get("gallery_size"))))
            for widget in controls.values():
                widget.currentIndexChanged.connect(self._schedule_preview)
            self.db_tabs.addTab(page, str(title)[:24])
        tab_height = 46 + max(
            (self.db_tabs.widget(i).sizeHint().height() for i in range(self.db_tabs.count())),
            default=120,
        )
        self.db_tabs.setFixedHeight(min(300, max(170, tab_height)))

    def _database_options(self) -> dict[str, dict]:
        result = {}
        for db_id, controls in self.db_controls.items():
            result[db_id] = {
                "view_id": controls["view"].currentData(),
                "group_by": controls["group"].currentData(),
                "gallery_fit": controls["fit"].currentData(),
                "gallery_size": controls["size"].currentData(),
            }
        return result

    def _on_load_progress(self, stage: str, data: dict):
        LOGGER.info("Load progress: %s", stage)
        if (self._has_cached_page or self._background_refresh) and stage != "databases":
            stage_names = {"page": "页面", "children": "子内容", "databases": "数据库"}
            self._set_status(f"正在后台同步{stage_names.get(stage, stage)}…")
            return
        self.page_data = data
        title = self._page_title_from_data(data)
        if stage == "databases":
            self._update_database_tabs(data)
        stage_names = {"page": "页面", "children": "子内容", "databases": "数据库"}
        self._set_status(f"正在加载{stage_names.get(stage, stage)}…")
        self._do_preview()

    def _on_page_loaded(self, data: dict):
        LOGGER.info("Load finished successfully")
        self._load_busy = False
        self.refresh_btn.setEnabled(True)
        self.page_data = data
        self.export_fab.setEnabled(True)
        title = self._page_title_from_data(data)

        self._update_database_tabs(data)
        try:
            token = resolve_token(self._current_token())
            cache_worker = Worker(
                self.page_cache.save, self._loaded_url, token, data, parent=self
            )
            cache_worker.error.connect(lambda message: LOGGER.warning("Cache save failed: %s", message))
            cache_worker.start()
        except Exception as exc:
            LOGGER.warning("Could not schedule page cache save: %s", exc)
        self._has_cached_page = False
        self._background_refresh = False

        self._set_status(f"已加载：{title}")
        self._do_preview()

    def _on_worker_error(self, message: str):
        LOGGER.error("Load worker error: %s", message)
        self._load_busy = False
        self.refresh_btn.setEnabled(True)
        if (self._has_cached_page or self._background_refresh) and self.page_data is not None:
            self.export_fab.setEnabled(True)
            self._set_status(f"在线同步失败，继续使用当前页面：{message}")
            QMessageBox.warning(self, "同步失败", "无法更新 Notion 数据，当前继续使用现有页面。")
        else:
            self._loaded_url = None
            self.export_fab.setEnabled(True)
            QMessageBox.critical(self, "错误", message)
            self._set_status(f"错误：{message}")
        self._background_refresh = False
        self._has_cached_page = False

    def _schedule_preview(self):
        if self.page_data is None:
            return
        self._preview_timer.stop()
        self._preview_timer.start(350)

    def _do_preview(self):
        if self.page_data is None:
            return
        if self._preview_busy:
            LOGGER.debug("Preview already in progress, skipping")
            self._preview_pending = True
            return
        self._preview_busy = True
        self._preview_pending = False
        self._set_status("正在更新预览…")
        renderer = self._make_renderer()
        params = self._params()
        worker = Worker(
            renderer.render_html,
            self.page_data,
            parent=self,
            orientation=params["orientation"],
            full_width=params["full_width"],
            database_options=params["database_options"],
            preview_mode=True,
        )
        worker.finished.connect(self._on_preview_ready)
        worker.error.connect(self._on_preview_error)
        worker.start()

    def _on_preview_ready(self, result: dict):
        self._preview_busy = False
        LOGGER.info("Preview render ready, html length=%d", len(result.get("html", "")))
        html_path = Path(tempfile.gettempdir()) / f"notion_preview_{os.getpid()}_{int(time.time() * 1000)}.html"
        try:
            html_path.write_text(result["html"], encoding="utf-8")
            LOGGER.info("Wrote preview html to %s", html_path)
            self.preview.load(QUrl.fromLocalFile(str(html_path)))
            self._set_status("预览已更新")
        except Exception as exc:
            LOGGER.exception("Failed to write/load preview html")
            self._set_status(f"预览错误：{exc}")
        if self._preview_pending:
            QTimer.singleShot(0, self._do_preview)

    def _on_preview_error(self, message: str):
        self._preview_busy = False
        LOGGER.error("Preview worker error: %s", message)
        self._set_status(f"预览错误：{message}")
        if self._preview_pending:
            QTimer.singleShot(0, self._do_preview)

    def _export_pdf(self):
        title = self._page_title_from_data(self.page_data or {})
        default_path = str(Path.cwd() / f"{self._safe_filename(title)}.pdf")
        output_path, selected_filter = QFileDialog.getSaveFileName(
            self, "保存文件", default_path, "PDF 文件 (*.pdf);;HTML 文件 (*.html)"
        )
        if not output_path:
            return
        suffix = Path(output_path).suffix.lower()
        if not suffix:
            suffix = ".html" if "HTML" in selected_filter else ".pdf"
            output_path += suffix
        if suffix not in (".pdf", ".html", ".htm"):
            QMessageBox.warning(self, "格式不支持", "请使用 .pdf、.html 或 .htm 扩展名。")
            return

        if self._export_busy:
            return

        self._export_busy = True
        self.export_fab.setEnabled(False)
        self._set_status("正在保存 HTML…" if suffix in (".html", ".htm") else "正在导出 PDF…")

        renderer = self._make_renderer()
        params = self._params()
        export_job = (
            renderer.export_html_from_data
            if suffix in (".html", ".htm") else renderer.export_pdf_from_data
        )
        worker = Worker(
            export_job,
            self.page_data,
            output_path,
            parent=self,
            orientation=params["orientation"],
            full_width=params["full_width"],
            database_options=params["database_options"],
        )
        worker.finished.connect(self._on_pdf_exported)
        worker.error.connect(self._on_export_error)
        worker.start()

    def _on_pdf_exported(self, path: str):
        self._export_busy = False
        self.export_fab.setEnabled(True)
        kind = "HTML" if Path(path).suffix.lower() in (".html", ".htm") else "PDF"
        self._set_status(f"{kind} 已保存：{path}")
        reply = QMessageBox.question(
            self,
            "导出完成",
            f"{kind} 已保存到：\n{path}\n\n是否打开所在文件夹？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            os.startfile(str(Path(path).parent))

    def _on_export_error(self, message: str):
        self._export_busy = False
        self.export_fab.setEnabled(True)
        QMessageBox.critical(self, "导出失败", message)
        self._set_status(f"导出错误：{message}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    window = NotionPdfWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
