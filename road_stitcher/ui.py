from __future__ import annotations

import ntpath
import os
import queue
import shutil
import sys
import threading
from pathlib import Path

from . import __version__
from .config import JobConfig, WORK_DIR_NAME
from .media import inspect_video
from .pipeline import PipelineRunner
from .state import PipelineControl


def _qt():
    try:
        from PySide6 import QtCore, QtGui, QtWidgets

        return QtCore, QtGui, QtWidgets
    except ImportError as error:
        raise RuntimeError(
            "未安装图形界面组件。请安装 requirements.txt 中的依赖。"
        ) from error


def _resource_path(relative_path: str) -> Path:
    """Resolve project assets both from source and a PyInstaller bundle."""
    bundle_root = Path(
        getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])
    )
    return bundle_root / relative_path


def normalize_windows_path_text(value: str | Path) -> str:
    """Return a stable Windows-style path for display in the desktop UI."""
    text = str(value).strip()
    if not text:
        return ""
    return ntpath.normpath(text.replace("/", "\\"))


def default_output_dir_for_video(video_path: str | Path) -> Path:
    """Place automatic results beside the source video, never beside the app."""
    text = str(video_path).strip()
    if not text:
        raise ValueError("请选择航拍视频")
    video = Path(normalize_windows_path_text(text)).expanduser().resolve()
    return video.parent / f"{video.stem}_拼接结果"


def processing_cache_dir(output_dir: str | Path) -> Path:
    """Return the only resettable cache location for a completed task."""
    return Path(output_dir).expanduser().resolve() / WORK_DIR_NAME


def clear_processing_cache(output_dir: str | Path) -> Path:
    """Remove intermediate data only; user-facing results remain untouched."""
    output_root = Path(output_dir).expanduser().resolve()
    cache_dir = processing_cache_dir(output_root)
    if cache_dir.parent != output_root:
        raise RuntimeError("Refusing to clear a cache outside the result folder.")
    if cache_dir.is_dir():
        shutil.rmtree(cache_dir)
    return cache_dir


def format_duration(seconds: float) -> str:
    """Format a video duration for the compact source summary."""
    total_seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def format_file_size(byte_count: int) -> str:
    """Format a source file size without fake precision."""
    size = max(0, int(byte_count))
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    unit = units[0]
    for candidate in units:
        unit = candidate
        if value < 1024.0 or candidate == units[-1]:
            break
        value /= 1024.0
    if unit == "B":
        return f"{size:d} B"
    precision = 0 if value >= 100 else 1
    return f"{value:.{precision}f} {unit}"


def run_gui() -> int:
    QtCore, QtGui, QtWidgets = _qt()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    icon_path = _resource_path(
        "road_stitcher/assets/road_stitcher_icon_v2.png"
    )
    app_icon = QtGui.QIcon(str(icon_path))

    class MainWindow(QtWidgets.QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.thread: threading.Thread | None = None
            self.events: queue.Queue[tuple[str, object]] = queue.Queue()
            self.poll_timer = QtCore.QTimer(self)
            self.poll_timer.setInterval(75)
            self.poll_timer.timeout.connect(self._drain_events)
            self.control: PipelineControl | None = None
            self.output_dir: Path | None = None
            self.output_is_automatic = False
            self.setWindowTitle("航拍道路拼接工具")
            self.setWindowIcon(app_icon)
            self.resize(1080, 840)
            self.setMinimumSize(900, 720)
            self.setAcceptDrops(True)
            self._build_ui()
            self._apply_style()

        def _build_ui(self) -> None:
            central = QtWidgets.QWidget()
            central.setObjectName("appRoot")
            self.setCentralWidget(central)
            layout = QtWidgets.QVBoxLayout(central)
            layout.setContentsMargins(28, 22, 28, 24)
            layout.setSpacing(16)

            header = QtWidgets.QHBoxLayout()
            header.setSpacing(14)
            brand_icon = QtWidgets.QLabel()
            brand_icon.setObjectName("brandIcon")
            brand_icon.setPixmap(app_icon.pixmap(48, 48))
            brand_icon.setFixedSize(52, 52)
            brand_icon.setAlignment(QtCore.Qt.AlignCenter)
            header.addWidget(brand_icon)

            title_column = QtWidgets.QVBoxLayout()
            title_column.setSpacing(2)
            title = QtWidgets.QLabel("航拍道路拼接")
            title.setObjectName("title")
            subtitle = QtWidgets.QLabel(
                "把垂直航拍视频整理成连续、可检查的地面影像"
            )
            subtitle.setObjectName("subtitle")
            title_column.addWidget(title)
            title_column.addWidget(subtitle)
            header.addLayout(title_column)
            header.addStretch()

            version_label = QtWidgets.QLabel(f"v{__version__}")
            version_label.setObjectName("version")
            privacy_label = QtWidgets.QLabel("本地离线处理 · 数据不上传")
            privacy_label.setObjectName("privacyBadge")
            header.addWidget(version_label)
            header.addWidget(privacy_label)
            layout.addLayout(header)

            self.notice_label = QtWidgets.QLabel()
            self.notice_label.setObjectName("notice")
            self.notice_label.setWordWrap(True)
            self.notice_label.setVisible(False)
            layout.addWidget(self.notice_label)

            content = QtWidgets.QHBoxLayout()
            content.setSpacing(16)

            task_panel = QtWidgets.QFrame()
            task_panel.setObjectName("taskPanel")
            task_layout = QtWidgets.QVBoxLayout(task_panel)
            task_layout.setContentsMargins(22, 20, 22, 22)
            task_layout.setSpacing(9)

            task_title = QtWidgets.QLabel("任务文件")
            task_title.setObjectName("sectionTitle")
            task_hint = QtWidgets.QLabel(
                "选择原始视频、结果目录，并为本次任务命名。"
            )
            task_hint.setObjectName("sectionHint")
            task_layout.addWidget(task_title)
            task_layout.addWidget(task_hint)
            task_layout.addSpacing(8)

            video_label = QtWidgets.QLabel("航拍视频")
            video_label.setObjectName("fieldLabel")
            task_layout.addWidget(video_label)

            self.video_edit = QtWidgets.QLineEdit()
            self.video_edit.setObjectName("videoPathEdit")
            self.video_edit.setPlaceholderText(
                "选择或拖入 MP4、MOV、MKV 等视频"
            )
            self.video_edit.setAccessibleName("航拍视频路径")
            self.video_edit.editingFinished.connect(self._video_path_edited)
            video_button = QtWidgets.QPushButton("选择视频")
            video_button.setObjectName("browseButton")
            video_button.clicked.connect(self._choose_video)
            video_row = QtWidgets.QHBoxLayout()
            video_row.setSpacing(8)
            video_row.addWidget(self.video_edit, 1)
            video_row.addWidget(video_button)
            task_layout.addLayout(video_row)

            self.video_summary = QtWidgets.QLabel(
                "支持 MP4 / MOV / M4V / AVI / MKV，可将视频直接拖入窗口。"
            )
            self.video_summary.setObjectName("sourceSummary")
            self.video_summary.setWordWrap(True)
            task_layout.addWidget(self.video_summary)

            task_layout.addSpacing(5)
            output_label_row = QtWidgets.QHBoxLayout()
            output_label_row.setSpacing(8)
            output_label = QtWidgets.QLabel("结果目录")
            output_label.setObjectName("fieldLabel")
            self.output_mode_label = QtWidgets.QLabel("自动跟随视频位置")
            self.output_mode_label.setObjectName("autoTag")
            output_label_row.addWidget(output_label)
            output_label_row.addWidget(self.output_mode_label)
            output_label_row.addStretch()
            task_layout.addLayout(output_label_row)

            self.output_edit = QtWidgets.QLineEdit()
            self.output_edit.setObjectName("outputPathEdit")
            self.output_edit.setPlaceholderText(
                "未选择时自动保存到视频所在目录"
            )
            self.output_edit.setAccessibleName("结果保存目录")
            self.output_edit.editingFinished.connect(
                lambda: self._normalize_path_field(self.output_edit)
            )
            self.output_edit.textEdited.connect(self._mark_output_manual)
            output_button = QtWidgets.QPushButton("选择目录")
            output_button.setObjectName("browseButton")
            output_button.clicked.connect(self._choose_output)
            output_row = QtWidgets.QHBoxLayout()
            output_row.setSpacing(8)
            output_row.addWidget(self.output_edit, 1)
            output_row.addWidget(output_button)
            task_layout.addLayout(output_row)
            output_hint = QtWidgets.QLabel(
                "成果图保存在此目录；抽帧、日志和缓存统一放入"
                "“处理数据”子目录。"
            )
            output_hint.setObjectName("sectionHint")
            output_hint.setWordWrap(True)
            task_layout.addWidget(output_hint)

            task_layout.addSpacing(5)
            name_label = QtWidgets.QLabel("项目名称")
            name_label.setObjectName("fieldLabel")
            task_layout.addWidget(name_label)

            self.name_edit = QtWidgets.QLineEdit("航拍道路拼接")
            self.name_edit.setAccessibleName("项目名称")
            task_layout.addWidget(self.name_edit)
            task_layout.addStretch()
            content.addWidget(task_panel, 3)

            settings_panel = QtWidgets.QFrame()
            settings_panel.setObjectName("settingsPanel")
            settings_panel.setMinimumWidth(310)
            settings_panel.setMaximumWidth(350)
            settings_layout = QtWidgets.QVBoxLayout(settings_panel)
            settings_layout.setContentsMargins(20, 20, 20, 22)
            settings_layout.setSpacing(6)

            settings_title = QtWidgets.QLabel("处理方案")
            settings_title.setObjectName("sectionTitle")
            settings_layout.addWidget(settings_title)
            settings_layout.addSpacing(6)

            mode_label = QtWidgets.QLabel("拼接模式")
            mode_label.setObjectName("fieldLabel")
            settings_layout.addWidget(mode_label)
            self.mode_group = QtWidgets.QButtonGroup(self)
            self.mode_group.setExclusive(True)
            self.road_mode_button = QtWidgets.QPushButton("沿道路")
            self.road_mode_button.setObjectName("roadModeButton")
            self.road_mode_button.setProperty("role", "modeButton")
            self.road_mode_button.setCheckable(True)
            self.road_mode_button.setChecked(True)
            self.road_mode_button.setAccessibleName("沿道路拼接模式")
            self.free_mode_button = QtWidgets.QPushButton("自由平移")
            self.free_mode_button.setObjectName("freeModeButton")
            self.free_mode_button.setProperty("role", "modeButton")
            self.free_mode_button.setCheckable(True)
            self.free_mode_button.setAccessibleName("自由平移拼接模式")
            self.mode_group.addButton(self.road_mode_button)
            self.mode_group.addButton(self.free_mode_button)
            self.road_mode_button.clicked.connect(self._update_mode_help)
            self.free_mode_button.clicked.connect(self._update_mode_help)
            mode_row = QtWidgets.QHBoxLayout()
            mode_row.setSpacing(0)
            mode_row.addWidget(self.road_mode_button, 1)
            mode_row.addWidget(self.free_mode_button, 1)
            settings_layout.addLayout(mode_row)

            self.mode_help = QtWidgets.QLabel()
            self.mode_help.setObjectName("modeHelp")
            self.mode_help.setWordWrap(True)
            self.mode_help.setMinimumHeight(48)
            self.mode_help.setAlignment(
                QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter
            )
            settings_layout.addWidget(self.mode_help)

            divider = QtWidgets.QFrame()
            divider.setObjectName("divider")
            divider.setFrameShape(QtWidgets.QFrame.HLine)
            settings_layout.addWidget(divider)

            interval_label = QtWidgets.QLabel("抽帧间隔")
            interval_label.setObjectName("fieldLabel")
            settings_layout.addWidget(interval_label)

            interval_row = QtWidgets.QHBoxLayout()
            interval_row.setSpacing(8)
            self.interval_mode = QtWidgets.QComboBox()
            self.interval_mode.setObjectName("intervalMode")
            self.interval_mode.addItem("自动推荐", "auto")
            self.interval_mode.addItem("手动设置", "manual")
            self.interval_mode.setAccessibleName("抽帧间隔模式")
            self.interval_spin = QtWidgets.QDoubleSpinBox()
            self.interval_spin.setObjectName("intervalSpin")
            self.interval_spin.setRange(0.25, 30.0)
            self.interval_spin.setSingleStep(0.5)
            self.interval_spin.setValue(2.0)
            self.interval_spin.setSuffix(" 秒")
            self.interval_spin.setButtonSymbols(
                QtWidgets.QAbstractSpinBox.NoButtons
            )
            self.interval_spin.setMinimumWidth(82)
            self.interval_spin.setAccessibleName("手动抽帧间隔")
            self.interval_spin.setEnabled(False)
            self.interval_decrease_button = QtWidgets.QPushButton()
            self.interval_decrease_button.setObjectName(
                "intervalDecreaseButton"
            )
            self.interval_decrease_button.setProperty("role", "stepButton")
            self.interval_decrease_button.setText("−")
            self.interval_decrease_button.setAccessibleName("减小抽帧间隔")
            self.interval_decrease_button.setToolTip("减小 0.5 秒")
            self.interval_decrease_button.setAutoRepeat(True)
            self.interval_decrease_button.clicked.connect(
                lambda: self._step_interval(-1)
            )
            self.interval_increase_button = QtWidgets.QPushButton()
            self.interval_increase_button.setObjectName(
                "intervalIncreaseButton"
            )
            self.interval_increase_button.setProperty("role", "stepButton")
            self.interval_increase_button.setText("+")
            self.interval_increase_button.setAccessibleName("增大抽帧间隔")
            self.interval_increase_button.setToolTip("增大 0.5 秒")
            self.interval_increase_button.setAutoRepeat(True)
            self.interval_increase_button.clicked.connect(
                lambda: self._step_interval(1)
            )
            self.interval_spin.valueChanged.connect(
                self._update_interval_buttons
            )
            self.interval_mode.currentIndexChanged.connect(
                self._update_interval_state
            )
            interval_row.addWidget(self.interval_mode, 1)
            interval_controls = QtWidgets.QHBoxLayout()
            interval_controls.setSpacing(4)
            interval_controls.addWidget(self.interval_decrease_button)
            interval_controls.addWidget(self.interval_spin, 1)
            interval_controls.addWidget(self.interval_increase_button)
            interval_row.addLayout(interval_controls, 1)
            settings_layout.addLayout(interval_row)

            output_title = QtWidgets.QLabel("输出内容")
            output_title.setObjectName("fieldLabel")
            settings_layout.addWidget(output_title)
            self.preview_check = QtWidgets.QCheckBox(
                "快速预览（便于检查）"
            )
            self.preview_check.setChecked(True)
            self.high_check = QtWidgets.QCheckBox(
                "高清拼接图（最终成果）"
            )
            self.high_check.setChecked(True)
            self.high_check.setToolTip(
                "高清图需要更多内存和处理时间，超大成果建议保存为 PSB"
            )
            settings_layout.addWidget(self.preview_check)
            settings_layout.addWidget(self.high_check)
            settings_layout.addStretch()
            content.addWidget(settings_panel, 1)
            layout.addLayout(content, 2)

            run_panel = QtWidgets.QFrame()
            run_panel.setObjectName("runPanel")
            run_layout = QtWidgets.QVBoxLayout(run_panel)
            run_layout.setContentsMargins(20, 16, 20, 18)
            run_layout.setSpacing(10)

            stage_row = QtWidgets.QHBoxLayout()
            self.status_dot = QtWidgets.QLabel()
            self.status_dot.setObjectName("statusDot")
            self.status_dot.setProperty("tone", "idle")
            self.status_dot.setFixedSize(9, 9)
            stage_caption = QtWidgets.QLabel("任务状态")
            stage_caption.setObjectName("stageCaption")
            self.stage_label = QtWidgets.QLabel("等待开始")
            self.stage_label.setObjectName("stage")
            self.progress_value_label = QtWidgets.QLabel("0%")
            self.progress_value_label.setObjectName("progressValue")
            stage_row.addWidget(self.status_dot)
            stage_row.addWidget(stage_caption)
            stage_row.addWidget(self.stage_label)
            stage_row.addStretch()
            stage_row.addWidget(self.progress_value_label)
            run_layout.addLayout(stage_row)

            self.progress_bar = QtWidgets.QProgressBar()
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(0)
            self.progress_bar.setTextVisible(False)
            run_layout.addWidget(self.progress_bar)

            button_row = QtWidgets.QHBoxLayout()
            button_row.setSpacing(8)
            self.start_button = QtWidgets.QPushButton("开始处理")
            self.start_button.setObjectName("primaryButton")
            self.start_button.clicked.connect(self._start)
            self.pause_button = QtWidgets.QPushButton("暂停")
            self.pause_button.setEnabled(False)
            self.pause_button.clicked.connect(self._toggle_pause)
            self.cancel_button = QtWidgets.QPushButton("取消")
            self.cancel_button.setObjectName("dangerButton")
            self.cancel_button.setEnabled(False)
            self.cancel_button.clicked.connect(self._cancel)
            self.reset_button = QtWidgets.QPushButton("重置当前任务")
            self.reset_button.setObjectName("resetButton")
            self.reset_button.setToolTip("清空表单，开始处理另一段视频")
            self.reset_button.clicked.connect(self._reset_task)
            self.open_button = QtWidgets.QPushButton("打开结果目录")
            self.open_button.setObjectName("openButton")
            self.open_button.setEnabled(False)
            self.open_button.clicked.connect(self._open_output)
            button_row.addWidget(self.start_button)
            button_row.addWidget(self.pause_button)
            button_row.addWidget(self.cancel_button)
            button_row.addWidget(self.reset_button)
            button_row.addStretch()
            button_row.addWidget(self.open_button)
            run_layout.addLayout(button_row)
            layout.addWidget(run_panel)

            log_header = QtWidgets.QHBoxLayout()
            log_title = QtWidgets.QLabel("处理记录")
            log_title.setObjectName("sectionTitleSmall")
            log_hint = QtWidgets.QLabel("用于检查抽帧、配准和导出过程")
            log_hint.setObjectName("sectionHint")
            log_header.addWidget(log_title)
            log_header.addWidget(log_hint)
            log_header.addStretch()
            layout.addLayout(log_header)
            self.log_edit = QtWidgets.QPlainTextEdit()
            self.log_edit.setReadOnly(True)
            self.log_edit.setPlaceholderText("处理日志将在这里显示")
            self.log_edit.setMaximumBlockCount(3000)
            self.log_edit.setMinimumHeight(132)
            self.log_edit.setMaximumHeight(180)
            layout.addWidget(self.log_edit, 1)

            self._update_mode_help()
            self._update_interval_state()

        def _apply_style(self) -> None:
            self.setStyleSheet(
                """
                QMainWindow, QWidget#appRoot { background: #f2f5f3; }
                QWidget {
                    color: #172321;
                    font-family: "Microsoft YaHei UI";
                    font-size: 14px;
                }
                QLabel#brandIcon {
                    background: #e4efeb;
                    border: 1px solid #cfdfda;
                    border-radius: 13px;
                }
                QLabel#title {
                    color: #14201e;
                    font-size: 27px;
                    font-weight: 700;
                }
                QLabel#subtitle {
                    color: #667571;
                    font-size: 13px;
                }
                QLabel#version {
                    color: #71807c;
                    font-size: 12px;
                    padding: 6px 4px;
                }
                QLabel#privacyBadge {
                    color: #315e57;
                    background: #e2eee9;
                    border: 1px solid #cee0d9;
                    border-radius: 10px;
                    font-size: 12px;
                    font-weight: 600;
                    padding: 7px 11px;
                }
                QFrame#taskPanel, QFrame#runPanel {
                    background: #ffffff;
                    border: 1px solid #d8e1de;
                    border-radius: 14px;
                }
                QFrame#settingsPanel {
                    background: #edf4f1;
                    border: 1px solid #d2e0db;
                    border-radius: 14px;
                }
                QLabel#sectionTitle {
                    color: #172321;
                    font-size: 18px;
                    font-weight: 700;
                }
                QLabel#sectionTitleSmall {
                    color: #263532;
                    font-size: 14px;
                    font-weight: 700;
                }
                QLabel#sectionHint {
                    color: #72807c;
                    font-size: 12px;
                }
                QLabel#sourceSummary {
                    color: #58706a;
                    font-size: 12px;
                    padding: 2px 1px 0 1px;
                }
                QLabel#autoTag {
                    color: #436861;
                    background: #e6efec;
                    border-radius: 6px;
                    font-size: 11px;
                    font-weight: 600;
                    padding: 3px 7px;
                }
                QLabel#fieldLabel, QLabel#stageCaption {
                    color: #4d5d59;
                    font-size: 12px;
                    font-weight: 600;
                }
                QLabel#modeHelp {
                    color: #49635d;
                    background: #e1ece8;
                    border-radius: 8px;
                    font-size: 12px;
                    padding: 9px 10px;
                }
                QFrame#divider {
                    color: #d6e2de;
                    background: #d6e2de;
                    max-height: 1px;
                    margin-top: 7px;
                    margin-bottom: 7px;
                }
                QLineEdit, QComboBox, QDoubleSpinBox {
                    min-height: 22px;
                    color: #172321;
                    background: #ffffff;
                    border: 1px solid #cbd7d3;
                    border-radius: 8px;
                    padding: 8px 10px;
                    selection-background-color: #0b6e69;
                }
                QLineEdit:hover, QComboBox:hover, QDoubleSpinBox:hover {
                    border-color: #9eb6b0;
                }
                QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus {
                    border: 2px solid #0b6e69;
                    padding: 7px 9px;
                }
                QLineEdit::placeholder { color: #94a29e; }
                QComboBox::drop-down {
                    width: 28px;
                    border: none;
                }
                QComboBox QAbstractItemView {
                    background: #ffffff;
                    border: 1px solid #cbd7d3;
                    selection-background-color: #dcebe6;
                    selection-color: #172321;
                    padding: 4px;
                }
                QPushButton {
                    min-height: 22px;
                    color: #33423f;
                    background: #ffffff;
                    border: 1px solid #c8d4d0;
                    border-radius: 8px;
                    padding: 8px 14px;
                    font-weight: 600;
                }
                QPushButton:hover {
                    background: #edf3f1;
                    border-color: #aebfba;
                }
                QPushButton:pressed {
                    background: #e1eae7;
                    padding-top: 9px;
                    padding-bottom: 7px;
                }
                QPushButton:focus {
                    border: 2px solid #0b6e69;
                    padding: 7px 13px;
                }
                QPushButton:disabled {
                    color: #9aa6a2;
                    background: #eef1f0;
                    border-color: #dde3e1;
                }
                QPushButton#browseButton { min-width: 76px; }
                QPushButton[role="stepButton"] {
                    min-width: 32px;
                    max-width: 32px;
                    font-size: 17px;
                    font-weight: 500;
                    padding-left: 0;
                    padding-right: 0;
                }
                QPushButton[role="modeButton"] {
                    color: #53635f;
                    background: #f8faf9;
                    border-color: #cbd8d4;
                    border-radius: 0;
                    min-height: 24px;
                    padding: 8px 10px;
                }
                QPushButton#roadModeButton {
                    border-top-left-radius: 8px;
                    border-bottom-left-radius: 8px;
                }
                QPushButton#freeModeButton {
                    border-left: none;
                    border-top-right-radius: 8px;
                    border-bottom-right-radius: 8px;
                }
                QPushButton[role="modeButton"]:hover {
                    color: #244a44;
                    background: #edf4f1;
                }
                QPushButton[role="modeButton"]:checked {
                    color: #ffffff;
                    background: #176f67;
                    border-color: #176f67;
                }
                QPushButton#freeModeButton:checked {
                    border-left: 1px solid #176f67;
                }
                QPushButton#primaryButton {
                    color: #ffffff;
                    background: #0b6e69;
                    border-color: #0b6e69;
                    min-width: 124px;
                }
                QPushButton#primaryButton:hover {
                    background: #075b57;
                    border-color: #075b57;
                }
                QPushButton#primaryButton:pressed {
                    background: #064d49;
                }
                QPushButton#dangerButton {
                    color: #a33f38;
                    border-color: #dfc1bd;
                }
                QPushButton#dangerButton:hover {
                    color: #8e302a;
                    background: #f9ecea;
                    border-color: #d8aaa4;
                }
                QPushButton#resetButton {
                    color: #53635f;
                    background: #f8faf9;
                }
                QPushButton#resetButton:hover {
                    color: #244a44;
                    background: #edf4f1;
                }
                QPushButton#openButton {
                    color: #0b625d;
                }
                QCheckBox {
                    color: #30413d;
                    spacing: 8px;
                    padding: 3px 0;
                }
                QCheckBox:focus {
                    color: #075b57;
                }
                QLabel#stage {
                    color: #263a36;
                    font-weight: 600;
                }
                QLabel#statusDot {
                    background: #9aa7a3;
                    border-radius: 4px;
                }
                QLabel#statusDot[tone="running"] {
                    background: #0b6e69;
                }
                QLabel#statusDot[tone="paused"] {
                    background: #c17b18;
                }
                QLabel#statusDot[tone="success"] {
                    background: #2d8a62;
                }
                QLabel#statusDot[tone="error"] {
                    background: #bd5148;
                }
                QLabel#progressValue {
                    color: #0b625d;
                    font-size: 12px;
                    font-weight: 700;
                }
                QProgressBar {
                    min-height: 9px;
                    max-height: 9px;
                    border: none;
                    border-radius: 4px;
                    background: #dfe8e5;
                }
                QProgressBar::chunk {
                    background: #0b6e69;
                    border-radius: 4px;
                }
                QPlainTextEdit {
                    color: #35433f;
                    background: #e9efed;
                    border: 1px solid #d2dcda;
                    border-radius: 10px;
                    padding: 11px 12px;
                    font-family: "Cascadia Mono", "Consolas";
                    font-size: 12px;
                    selection-background-color: #b9d8cf;
                }
                QLabel#notice {
                    border-radius: 9px;
                    padding: 10px 13px;
                    font-size: 13px;
                    font-weight: 600;
                }
                QLabel#notice[tone="success"] {
                    color: #245d49;
                    background: #e3f1ea;
                    border: 1px solid #c5dfd1;
                }
                QLabel#notice[tone="error"] {
                    color: #8d3832;
                    background: #faecea;
                    border: 1px solid #e9c8c4;
                }
                QLabel#notice[tone="warning"] {
                    color: #76511c;
                    background: #fbf1df;
                    border: 1px solid #ead5ad;
                }
                """
            )

        def _selected_stitch_mode(self) -> str:
            return "free" if self.free_mode_button.isChecked() else "road"

        def _update_mode_help(self, _checked: bool = False) -> None:
            if self._selected_stitch_mode() == "free":
                self.mode_help.setText(
                    "整幅地面参与匹配，适合固定高度下的"
                    "直线、斜向或弯曲平移。"
                )
            else:
                self.mode_help.setText(
                    "匹配道路标线与护栏，适合道路始终"
                    "位于画面中央的拍摄。"
                )

        def _update_interval_state(self) -> None:
            manual = self.interval_mode.currentData() == "manual"
            self.interval_spin.setEnabled(manual)
            self._update_interval_buttons()
            if manual:
                self.interval_spin.setToolTip("相邻画面应保留足够重叠")
            else:
                self.interval_spin.setToolTip(
                    "程序会分析代表性画面并自动选择间隔"
                )

        def _update_interval_buttons(self) -> None:
            manual = self.interval_mode.currentData() == "manual"
            value = self.interval_spin.value()
            self.interval_decrease_button.setEnabled(
                manual and value > self.interval_spin.minimum()
            )
            self.interval_increase_button.setEnabled(
                manual and value < self.interval_spin.maximum()
            )

        def _step_interval(self, steps: int) -> None:
            if self.interval_spin.isEnabled():
                self.interval_spin.stepBy(steps)

        def _normalize_path_field(
            self,
            field: QtWidgets.QLineEdit,
        ) -> None:
            normalized = normalize_windows_path_text(field.text())
            if normalized != field.text():
                field.setText(normalized)

        def _mark_output_manual(self, _text: str) -> None:
            self.output_is_automatic = False
            self.output_mode_label.setText("自定义位置")

        def _video_path_edited(self) -> None:
            self._normalize_path_field(self.video_edit)
            video_text = self.video_edit.text().strip()
            if video_text and (
                not self.output_edit.text().strip()
                or self.output_is_automatic
            ):
                self._apply_default_output(video_text)
            self._refresh_video_summary(video_text)

        def _apply_default_output(self, video_path: str | Path) -> None:
            default_dir = default_output_dir_for_video(video_path)
            self.output_edit.setText(normalize_windows_path_text(default_dir))
            self.output_is_automatic = True
            self.output_mode_label.setText("自动跟随视频位置")

        def _refresh_video_summary(self, video_path: str | Path) -> None:
            text = str(video_path).strip()
            if not text:
                self.video_summary.setText(
                    "支持 MP4 / MOV / M4V / AVI / MKV，"
                    "可将视频直接拖入窗口。"
                )
                return
            path = Path(normalize_windows_path_text(text))
            if not path.is_file():
                self.video_summary.setText(
                    "尚未找到该视频，请检查路径或重新选择。"
                )
                return
            try:
                info = inspect_video(path)
                file_size = format_file_size(path.stat().st_size)
            except Exception as error:
                self.video_summary.setText(f"无法读取视频信息：{error}")
                return
            self.video_summary.setText(
                f"{info.width} × {info.height}  ·  "
                f"{format_duration(info.duration_seconds)}  ·  "
                f"{file_size}  ·  {info.fps:.1f} fps"
            )

        def _set_video_path(self, path: str | Path) -> None:
            native_path = normalize_windows_path_text(path)
            self.video_edit.setText(native_path)
            if not self.output_edit.text().strip() or (
                self.output_is_automatic
            ):
                self._apply_default_output(native_path)
            if self.name_edit.text() == "航拍道路拼接":
                self.name_edit.setText(Path(native_path).stem)
            self._refresh_video_summary(native_path)

        def _set_notice(self, message: str = "", tone: str = "") -> None:
            self.notice_label.setText(message)
            self.notice_label.setProperty("tone", tone)
            self.notice_label.setVisible(bool(message))
            self.notice_label.style().unpolish(self.notice_label)
            self.notice_label.style().polish(self.notice_label)

        def _set_status_tone(self, tone: str) -> None:
            self.status_dot.setProperty("tone", tone)
            self.status_dot.style().unpolish(self.status_dot)
            self.status_dot.style().polish(self.status_dot)

        def _choose_video(self) -> None:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self,
                "选择航拍视频",
                "",
                "视频文件 (*.mp4 *.mov *.m4v *.avi *.mkv);;所有文件 (*.*)",
            )
            if path:
                self._set_video_path(path)

        def _choose_output(self) -> None:
            path = QtWidgets.QFileDialog.getExistingDirectory(
                self,
                "选择输出目录",
            )
            if path:
                self.output_edit.setText(normalize_windows_path_text(path))
                self.output_is_automatic = False
                self.output_mode_label.setText("自定义位置")

        def _create_config(self) -> JobConfig:
            video_text = self.video_edit.text().strip()
            if not video_text:
                raise ValueError("请选择航拍视频")
            video_text = normalize_windows_path_text(video_text)
            self.video_edit.setText(video_text)
            output_text = self.output_edit.text().strip()
            if not output_text:
                self._apply_default_output(video_text)
                output_text = self.output_edit.text()
            output_text = normalize_windows_path_text(output_text)
            self.output_edit.setText(output_text)
            return JobConfig(
                video_path=Path(video_text),
                output_dir=Path(output_text),
                project_name=self.name_edit.text().strip() or "航拍道路拼接",
                stitch_mode=self._selected_stitch_mode(),
                interval_mode=self.interval_mode.currentData(),
                interval_seconds=self.interval_spin.value(),
                make_preview=self.preview_check.isChecked(),
                make_high_resolution=self.high_check.isChecked(),
            )

        def _start(self) -> None:
            try:
                config = self._create_config()
                config.validate()
                if not config.make_preview and not config.make_high_resolution:
                    raise ValueError("请至少选择一种输出内容")
                # OpenCV/FFmpeg is initialized once on the GUI thread. Some
                # Windows Qt builds can otherwise crash while both libraries
                # perform their first native initialization on a QThread.
                inspect_video(config.video_path)
            except Exception as error:
                self._set_notice(f"无法开始：{error}", "error")
                self.stage_label.setText("请检查任务设置")
                self._set_status_tone("error")
                return

            self._set_notice()
            while True:
                try:
                    self.events.get_nowait()
                except queue.Empty:
                    break
            self.output_dir = config.output_dir
            self.control = PipelineControl()

            def enqueue_progress(
                stage: str,
                progress: float,
                message: str,
            ) -> None:
                self.events.put(("progress", (stage, progress, message)))

            def enqueue_log(message: str) -> None:
                self.events.put(("log", message))

            def run_pipeline() -> None:
                try:
                    runner = PipelineRunner(
                        config,
                        progress_callback=enqueue_progress,
                        log_callback=enqueue_log,
                        control=self.control,
                    )
                    outputs = runner.run()
                except Exception as error:
                    self.events.put(("failed", str(error)))
                else:
                    self.events.put(("finished", outputs))

            # Keep all Qt objects on the main thread. The processing thread
            # communicates through a plain Queue that a QTimer polls.
            self.thread = threading.Thread(
                target=run_pipeline,
                name="road-stitcher-pipeline",
                daemon=True,
            )

            self.start_button.setEnabled(False)
            self.pause_button.setEnabled(True)
            self.cancel_button.setEnabled(True)
            self.reset_button.setEnabled(False)
            self.open_button.setEnabled(True)
            self.pause_button.setText("暂停")
            self.progress_bar.setValue(0)
            self.progress_value_label.setText("0%")
            self.stage_label.setText("正在准备任务")
            self._set_status_tone("running")
            self.log_edit.clear()
            self.poll_timer.start()
            self.thread.start()

        @QtCore.Slot()
        def _drain_events(self) -> None:
            terminal = False
            while True:
                try:
                    event_type, payload = self.events.get_nowait()
                except queue.Empty:
                    break
                if event_type == "progress":
                    stage, progress, message = payload
                    self._on_progress(stage, progress, message)
                elif event_type == "log":
                    self._append_log(str(payload))
                elif event_type == "finished":
                    terminal = True
                    self._on_finished(payload)
                elif event_type == "failed":
                    terminal = True
                    self._on_failed(str(payload))
            if terminal:
                self.poll_timer.stop()
            if (
                self.thread is not None
                and not self.thread.is_alive()
                and self.events.empty()
            ):
                self.thread = None

        @QtCore.Slot(str, float, str)
        def _on_progress(self, stage: str, progress: float, message: str) -> None:
            self.stage_label.setText(message)
            self.progress_bar.setValue(int(progress * 1000))
            self.progress_value_label.setText(f"{int(progress * 100):d}%")
            self._set_status_tone("running")

        @QtCore.Slot(str)
        def _append_log(self, message: str) -> None:
            self.log_edit.appendPlainText(message)
            scrollbar = self.log_edit.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

        @QtCore.Slot(dict)
        def _on_finished(self, outputs: dict) -> None:
            self.progress_bar.setValue(1000)
            self.progress_value_label.setText("100%")
            self.stage_label.setText("处理完成")
            self._set_status_tone("success")
            self._append_log(f"已生成 {len(outputs)} 个结果文件")
            self._set_notice(
                f"处理完成，已生成 {len(outputs)} 个结果文件。"
                "可直接打开结果目录查看。",
                "success",
            )
            self._set_idle()

        @QtCore.Slot(str)
        def _on_failed(self, message: str) -> None:
            self.stage_label.setText("处理失败")
            self._set_status_tone("error")
            self._append_log(message)
            self._set_notice(f"处理失败：{message}", "error")
            self._set_idle()

        def _set_idle(self) -> None:
            self.start_button.setEnabled(True)
            self.pause_button.setEnabled(False)
            self.cancel_button.setEnabled(False)
            self.reset_button.setEnabled(True)
            self.open_button.setEnabled(self.output_dir is not None)
            self.pause_button.setText("暂停")

        def _reset_task(self) -> None:
            if self.thread is not None and self.thread.is_alive():
                return

            dialog = QtWidgets.QMessageBox(self)
            dialog.setIcon(QtWidgets.QMessageBox.Question)
            dialog.setWindowTitle("开始新的任务")
            dialog.setText("要清空当前表单，开始处理另一段视频吗？")
            dialog.setInformativeText(
                "默认不会删除已生成的拼接图片。可选择同时删除本任务的抽帧和对齐缓存。"
            )
            clear_form = dialog.addButton(
                "仅清空表单", QtWidgets.QMessageBox.AcceptRole
            )
            clear_cache = dialog.addButton(
                "清空表单并删除缓存", QtWidgets.QMessageBox.DestructiveRole
            )
            cancel = dialog.addButton("取消", QtWidgets.QMessageBox.RejectRole)
            clear_form.setDefault(True)
            dialog.exec()
            choice = dialog.clickedButton()
            if choice is cancel or choice is None:
                return

            cache_removed = False
            if choice is clear_cache and self.output_dir is not None:
                try:
                    clear_processing_cache(self.output_dir)
                    cache_removed = True
                except OSError as error:
                    self._set_notice(f"无法删除任务缓存：{error}", "error")
                    return

            self.video_edit.clear()
            self.output_edit.clear()
            self.output_is_automatic = False
            self.output_mode_label.setText("自动跟随视频位置")
            self.name_edit.setText("航拍道路拼接")
            self.road_mode_button.setChecked(True)
            self.interval_mode.setCurrentIndex(0)
            self.interval_spin.setValue(2.0)
            self.preview_check.setChecked(True)
            self.high_check.setChecked(True)
            self._refresh_video_summary("")
            self.log_edit.clear()
            self.output_dir = None
            self.control = None
            self.progress_bar.setValue(0)
            self.progress_value_label.setText("0%")
            self.stage_label.setText("等待开始")
            self._set_status_tone("")
            self.open_button.setEnabled(False)
            message = "已清空表单，可选择新的航拍视频。"
            if cache_removed:
                message = "已清空表单并删除任务缓存，拼接成果图片已保留。"
            self._set_notice(message, "success")

        def _toggle_pause(self) -> None:
            if self.control is None:
                return
            if self.control.pause_event.is_set():
                self.control.resume()
                self.pause_button.setText("暂停")
                self.stage_label.setText("继续处理")
                self._set_status_tone("running")
                self._set_notice()
            else:
                self.control.pause()
                self.pause_button.setText("继续")
                self.stage_label.setText("将在安全位置暂停")
                self._set_status_tone("paused")
                self._set_notice(
                    "已请求暂停。当前计算步骤结束后，任务会停在安全位置。",
                    "warning",
                )

        def _cancel(self) -> None:
            if self.control is not None:
                self.control.cancel()
                self.stage_label.setText("正在取消")
                self._set_status_tone("paused")
                self._set_notice(
                    "正在取消任务，已完成的抽帧和配准缓存会保留。",
                    "warning",
                )
                self.cancel_button.setEnabled(False)

        def _open_output(self) -> None:
            if self.output_dir is None:
                return
            self.output_dir.mkdir(parents=True, exist_ok=True)
            os.startfile(self.output_dir)

        def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:
            urls = event.mimeData().urls()
            if len(urls) != 1 or not urls[0].isLocalFile():
                event.ignore()
                return
            path = Path(urls[0].toLocalFile())
            if path.suffix.lower() in {
                ".mp4",
                ".mov",
                ".m4v",
                ".avi",
                ".mkv",
            }:
                event.acceptProposedAction()
            else:
                event.ignore()

        def dropEvent(self, event: QtGui.QDropEvent) -> None:
            urls = event.mimeData().urls()
            if len(urls) == 1 and urls[0].isLocalFile():
                self._set_video_path(urls[0].toLocalFile())
                self._set_notice()
                event.acceptProposedAction()
                return
            event.ignore()

        def closeEvent(self, event: QtGui.QCloseEvent) -> None:
            if self.thread is not None and self.thread.is_alive():
                answer = QtWidgets.QMessageBox.question(
                    self,
                    "任务仍在运行",
                    "关闭软件会取消当前任务。确定关闭吗？",
                )
                if answer != QtWidgets.QMessageBox.Yes:
                    event.ignore()
                    return
                if self.control is not None:
                    self.control.cancel()
                self.thread.join(timeout=3.0)
            event.accept()

    app.setApplicationName("航拍道路拼接工具")
    app.setOrganizationName("Road Stitcher")
    app.setWindowIcon(app_icon)
    window = MainWindow()
    window.show()
    return app.exec()
