import sys
import os
import re
import subprocess
import shutil
import tempfile
import json
import time
import threading
import psutil
from PyQt5.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QListWidget,
    QListWidgetItem,
    QLabel,
    QLineEdit,
    QComboBox,
    QFileDialog,
    QMessageBox,
    QGroupBox,
    QProgressBar,
    QTextEdit,
    QCheckBox,
    QSplitter,
    QSizePolicy,
    QSpinBox,
    QDialog,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
)
from string import Template
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QPoint
from PyQt5.QtGui import QIcon, QColor, QPainter, QPixmap, QPen, QPolygon, QTextCursor

# 增加这个函数，用于寻找打包后的资源文件
def resource_path(relative_path):
    """获取资源的绝对路径，兼容 PyInstaller 单文件打包"""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)


# ==========================================
# 日志落盘（ffmpeg-gui.log，单文件 10MB 上限）
# ==========================================
MAX_LOG_BYTES = 10 * 1024 * 1024
KEEP_LOG_BYTES = 5 * 1024 * 1024


def app_dir():
    """程序所在目录：打包后为 exe 目录，源码运行时为 main.py 所在目录"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


class LogFileWriter:
    """把界面日志同步写入 ffmpeg-gui.log，超出 10MB 时丢弃最早内容"""

    def __init__(self):
        self.enabled = True
        self.path = os.path.join(app_dir(), "ffmpeg-gui.log")
        self._lock = threading.Lock()
        if not self._probe(self.path):
            # 程序目录不可写时（比如装在 Program Files）退到用户目录
            fallback_dir = os.path.join(
                os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ffmpeg-gui"
            )
            try:
                os.makedirs(fallback_dir, exist_ok=True)
            except OSError:
                pass
            self.path = os.path.join(fallback_dir, "ffmpeg-gui.log")
            self.enabled = self._probe(self.path)

    @staticmethod
    def _probe(path):
        try:
            with open(path, "a", encoding="utf-8"):
                pass
            return True
        except OSError:
            return False

    def write(self, text):
        if not self.enabled:
            return
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with self._lock:
                with open(self.path, "a", encoding="utf-8") as f:
                    for line in str(text).split("\n"):
                        if line.strip():
                            f.write("[{}] {}\n".format(stamp, line))
                        else:
                            f.write("\n")
        except OSError:
            self.enabled = False
            return

        self._rotate_if_needed()

    def _rotate_if_needed(self):
        """超过 10MB 时只保留最后 5MB，避免日志无限增长"""
        try:
            if os.path.getsize(self.path) <= MAX_LOG_BYTES:
                return
        except OSError:
            return

        with self._lock:
            try:
                if os.path.getsize(self.path) <= MAX_LOG_BYTES:
                    return
                with open(self.path, "rb") as f:
                    f.seek(-KEEP_LOG_BYTES, os.SEEK_END)
                    data = f.read()
                # 丢掉被截断的第一行
                cut = data.find(b"\n")
                if cut != -1:
                    data = data[cut + 1:]
                marker = "[{}] ===== 日志超过 10MB，已丢弃最早的内容 =====\n".format(
                    time.strftime("%Y-%m-%d %H:%M:%S")
                ).encode("utf-8")
                with open(self.path, "wb") as f:
                    f.write(marker)
                    f.write(data)
            except OSError:
                pass


class LogView(QTextEdit):
    """日志框：append 到界面的同时写入日志文件

    追加时判断滚动条是否停在底部：
    - 在底部（含几个像素误差）→ 追加后继续贴底，跟随最新日志
    - 已被用户向上翻阅 → 保持当前视图不动，不把人拽回底部
    滚回底部后自动恢复跟随，无需额外开关。
    """

    FOLLOW_TOLERANCE = 6    # 距底部多少像素内仍视为"在跟随"
    MAX_BLOCKS = 3000       # 界面里最多保留的日志行数，超出丢弃最早的

    def __init__(self, writer=None, parent=None):
        super().__init__(parent)
        self._writer = writer

    def append(self, text):
        bar = self.verticalScrollBar()
        doc = self.document()
        following = bar.value() >= bar.maximum() - self.FOLLOW_TOLERANCE
        keep = bar.value()

        super().append(text)
        if self._writer:
            self._writer.write(text)

        # 清旧行前记一下文档高度，用于换算视图该往上挪多少
        height_before_trim = doc.size().height() if not following else 0
        self._trim()

        if following:
            bar.setValue(bar.maximum())
        else:
            # 从文档开头删掉若干行后，同样的滚动值会往下偏，需要减去被删掉的高度
            removed = max(height_before_trim - doc.size().height(), 0)
            bar.setValue(max(int(keep - removed), 0))

    def _trim(self):
        doc = self.document()
        extra = doc.blockCount() - self.MAX_BLOCKS
        if extra <= 0:
            return
        cursor = QTextCursor(doc)      # 用独立游标，避免破坏用户正在拖选的文本
        cursor.movePosition(QTextCursor.Start)
        cursor.movePosition(QTextCursor.Down, QTextCursor.KeepAnchor, extra)
        cursor.removeSelectedText()


# ==========================================
# 视觉样式：全局统一圆角 + 统一配色
# ==========================================
# 所有控件共用同一套圆角与边框取值，避免"有的圆有的方"
RADIUS = 8            # 按钮 / 输入框 / 列表框 / 日志框 / 进度条
RADIUS_CARD = 10      # 分组卡片（QGroupBox）
RADIUS_SMALL = 5      # 复选框、列表项等小元素

COLOR_BG = "#F3F5F9"        # 窗口底色
COLOR_CARD = "#FFFFFF"      # 卡片 / 输入类背景
COLOR_BORDER = "#DCE2EC"    # 常规边框
COLOR_BORDER_HOVER = "#B9C4D6"
COLOR_TEXT = "#1F2430"
COLOR_MUTED = "#6B7280"
COLOR_ACCENT = "#0078D4"       # 主题色：焦点、选中、主操作、进度条、勾选框
COLOR_ACCENT_DARK = "#006CBE"
COLOR_ACCENT_HOVER = "#1F8ADF"
COLOR_DANGER = "#E5484D"       # 停止
COLOR_DANGER_DARK = "#CE3F44"


def _ui_icon_dir():
    """QSS 的 image 只认文件路径，这里把勾选/箭头图标画成 PNG 放到临时目录"""
    path = os.path.join(tempfile.gettempdir(), "ffmpeg-gui-ui")
    os.makedirs(path, exist_ok=True)
    return path


def _draw_polyline_icon(path, size, points, color, width):
    """画一条圆头折线图标（勾选、下拉箭头都用它）"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(color))
    pen.setWidth(width)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.drawPolyline(QPolygon([QPoint(x, y) for x, y in points]))
    painter.end()
    pm.save(path, "PNG")


def _draw_triangle_icon(path, size, points, color):
    """画一个实心小三角，用于数字框的加减按钮"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(color))
    painter.drawPolygon(QPolygon([QPoint(x, y) for x, y in points]))
    painter.end()
    pm.save(path, "PNG")


def build_stylesheet():
    """生成全局 QSS（图标路径在运行时确定，方便单文件打包）"""
    icon_dir = _ui_icon_dir()
    icons = {
        "check": ("check.png", lambda p: _draw_polyline_icon(
            p, 32, [(7, 17), (13, 23), (25, 9)], "#FFFFFF", 5)),
        "chevron": ("chevron.png", lambda p: _draw_polyline_icon(
            p, 24, [(6, 9), (12, 15), (18, 9)], COLOR_MUTED, 3)),
        "up": ("arrow-up.png", lambda p: _draw_triangle_icon(
            p, 12, [(2, 8), (10, 8), (6, 3)], "#374151")),
        "down": ("arrow-down.png", lambda p: _draw_triangle_icon(
            p, 12, [(2, 4), (10, 4), (6, 9)], "#374151")),
    }
    values = {
        "bg": COLOR_BG, "card": COLOR_CARD, "border": COLOR_BORDER,
        "border_hover": COLOR_BORDER_HOVER, "text": COLOR_TEXT,
        "muted": COLOR_MUTED, "accent": COLOR_ACCENT,
        "accent_hover": COLOR_ACCENT_HOVER, "accent_dark": COLOR_ACCENT_DARK,
        "danger": COLOR_DANGER, "danger_dark": COLOR_DANGER_DARK,
        "r": f"{RADIUS}px", "rc": f"{RADIUS_CARD}px", "rs": f"{RADIUS_SMALL}px",
        "r_inner": f"{max(RADIUS - 2, 2)}px",
    }
    for key, (filename, painter) in icons.items():
        path = os.path.join(icon_dir, filename)
        # 每次都重画：成本极低（4 张小图），可避免旧版本残留的图标被复用
        try:
            painter(path)
        except Exception:
            pass
        values[key] = path.replace("\\", "/")
    return Template(QSS_TEMPLATE).substitute(values)


QSS_TEMPLATE = """
QWidget {
    font-family: "Microsoft YaHei", "微软雅黑", sans-serif;
    font-size: 14px;
    color: $text;
}
QMainWindow, QDialog, QMessageBox {
    background-color: $bg;
}
QToolTip {
    background-color: #2B3243;
    color: #FFFFFF;
    border: none;
    border-radius: $rs;
    padding: 6px 9px;
}

/* ---------- 分组卡片 ---------- */
QGroupBox {
    background-color: $card;
    border: 1px solid $border;
    border-radius: $rc;
    margin-top: 0px;
    padding: 24px 10px 8px 10px;
    font-size: 14px;
    font-weight: bold;
    color: $text;
}
QGroupBox::title {
    subcontrol-origin: padding;
    subcontrol-position: top left;
    left: 12px;
    top: 6px;
    padding: 0px;
    background-color: transparent;
    color: $text;
}

/* ---------- 输入类控件 ---------- */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QAbstractSpinBox {
    background-color: $card;
    border: 1px solid $border;
    border-radius: $r;
    padding: 4px 8px;
    min-height: 20px;
    color: $text;
    selection-background-color: $accent;
    selection-color: #FFFFFF;
}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {
    border-color: $border_hover;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border: 1px solid $accent;
}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {
    background-color: #F2F4F8;
    border-color: #E4E9F1;
    color: #9AA3B2;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 24px;
    border: none;
    background: transparent;
}
QComboBox::down-arrow {
    image: url($chevron);
    width: 11px;
    height: 11px;
}
QComboBox QAbstractItemView {
    background-color: $card;
    border: 1px solid $border;
    border-radius: $r;
    padding: 4px;
    outline: none;
    selection-background-color: #E5F2FB;
    selection-color: $text;
}
QSpinBox::up-button, QDoubleSpinBox::up-button {
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 20px;
    margin: 3px 3px 0px 0px;
    border: none;
    border-top-right-radius: $r_inner;
    background: transparent;
}
QSpinBox::down-button, QDoubleSpinBox::down-button {
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 20px;
    margin: 0px 3px 3px 0px;
    border: none;
    border-bottom-right-radius: $r_inner;
    background: transparent;
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {
    background-color: #EAF3FC;
}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
    image: url($up);
    width: 10px;
    height: 7px;
}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
    image: url($down);
    width: 10px;
    height: 7px;
}
/* ---------- 按钮 ---------- */
QPushButton {
    background-color: $card;
    border: 1px solid $border;
    border-radius: $r;
    padding: 5px 14px;
    min-height: 20px;
    color: $text;
}
QPushButton:hover {
    background-color: #F3F9FE;
    border-color: $border_hover;
}
QPushButton:pressed {
    background-color: #E1F1FB;
}
QPushButton:disabled {
    background-color: #F2F4F8;
    border-color: #E6EAF2;
    color: #A8B0BE;
}
QPushButton#primaryButton {
    background-color: $accent;
    border: 1px solid $accent_dark;
    color: #FFFFFF;
    font-weight: bold;
}
QPushButton#primaryButton:hover {
    background-color: $accent_hover;
    border-color: $accent_dark;
}
QPushButton#primaryButton:pressed {
    background-color: $accent_dark;
}
QPushButton#primaryButton:disabled {
    background-color: #E8EDF4;
    border-color: #E1E7F0;
    color: #A8B0BE;
}
QPushButton#dangerButton {
    background-color: $danger;
    border: 1px solid $danger_dark;
    color: #FFFFFF;
    font-weight: bold;
}
QPushButton#dangerButton:hover {
    background-color: #EB5A5F;
    border-color: $danger_dark;
}
QPushButton#dangerButton:pressed {
    background-color: $danger_dark;
}
QPushButton#dangerButton:disabled {
    background-color: #E8EDF4;
    border-color: #E1E7F0;
    color: #A8B0BE;
}

/* ---------- 列表 / 文本 / 表格 ---------- */
QListWidget, QTextEdit, QPlainTextEdit, QTableWidget, QScrollArea {
    background-color: $card;
    border: 1px solid $border;
    border-radius: $r;
}
QListWidget {
    padding: 4px;
    outline: none;
}
QTextEdit, QPlainTextEdit {
    padding: 6px;
    font-size: 13px;
}
QListWidget::item {
    padding: 4px 6px;
    border-radius: $rs;
}
QListWidget::item:hover {
    background-color: #F3F9FE;
}
QListWidget::item:selected {
    background-color: #E5F2FB;
    color: $text;
}
QScrollArea > QWidget > QWidget {
    background: transparent;
}
QTableWidget {
    gridline-color: #EDF1F7;
    selection-background-color: #E5F2FB;
    selection-color: $text;
}
QTableWidget::item {
    /* 只设 padding：同时设 border 会让 Qt 把单元格文本按单行截断（换行失效） */
    padding: 4px 6px;
}
QHeaderView::section {
    background-color: #F7F9FC;
    color: $muted;
    border: none;
    border-bottom: 1px solid #E4E9F1;
    padding: 6px 8px;
    font-weight: bold;
}
QTableCornerButton::section {
    background-color: #F7F9FC;
    border: none;
}

/* ---------- 复选框 ---------- */
QCheckBox {
    spacing: 7px;
    background: transparent;
}
QCheckBox:disabled {
    color: #A8B0BE;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid #C6D0DF;
    border-radius: $rs;
    background-color: $card;
}
QCheckBox::indicator:hover {
    border-color: $accent;
}
QCheckBox::indicator:checked {
    background-color: $accent;
    border-color: $accent;
    image: url($check);
}
QCheckBox::indicator:checked:hover {
    background-color: $accent_hover;
    border-color: $accent_hover;
}
QCheckBox::indicator:disabled {
    background-color: #F2F4F8;
    border-color: #E4E9F1;
}
QCheckBox::indicator:checked:disabled {
    background-color: #A9CBEA;
    border-color: #A9CBEA;
}

/* ---------- 进度条 ---------- */
QProgressBar {
    background-color: #EDF1F7;
    border: 1px solid $border;
    border-radius: $r;
    min-height: 22px;
    text-align: center;
    color: $text;
    font-size: 12px;
}
QProgressBar::chunk {
    background-color: $accent;
    border-radius: $r_inner;
    margin: 1px;
}

/* ---------- 滚动条 ---------- */
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 2px 2px 2px 0px;
}
QScrollBar::handle:vertical {
    background-color: #C9D2E0;
    border-radius: 4px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background-color: #AEBACD;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: transparent;
}
QScrollBar:horizontal {
    background: transparent;
    height: 10px;
    margin: 0px 2px 2px 2px;
}
QScrollBar::handle:horizontal {
    background-color: #C9D2E0;
    border-radius: 4px;
    min-width: 30px;
}
QScrollBar::handle:horizontal:hover {
    background-color: #AEBACD;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0px;
}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background: transparent;
}

/* ---------- 其余容器 ---------- */
QLabel {
    background: transparent;
}
QSplitter::handle {
    background-color: transparent;
}
"""

# ==========================================
# 支持拖拽的控件
# ==========================================
VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".flv", ".ts", ".webm",
    ".m4v", ".wmv", ".mpg", ".mpeg", ".rmvb",
}
AUDIO_EXTS = {
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma",
}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS
SUB_EXTS = {".srt", ".ass", ".vtt", ".ssa", ".sub"}

# 各输出容器能承载的字幕编码器（查不到即表示该容器不支持文字字幕）
SUB_CODEC_BY_EXT = {
    ".mp4": "mov_text",
    ".m4v": "mov_text",
    ".mov": "mov_text",
    ".mkv": "copy",        # 保留 ASS/SSA 特效、字体与定位
    ".webm": "webvtt",
}

# 位图字幕：只能 copy 进 MKV，无法转成文字字幕
BITMAP_SUB_EXTS = {".sub", ".sup", ".idx"}

# 轨道管理用：各容器允许原样复制的字幕编码（None 表示全部支持）
CONTAINER_SUB_CODECS = {
    ".mp4": {"mov_text"},
    ".m4v": {"mov_text"},
    ".mov": {"mov_text"},
    ".mkv": None,
    ".webm": {"webvtt"},
}


def subtitle_codec_allowed(container_ext, codec):
    """判断某个字幕编码能否原样复制进指定容器"""
    if container_ext not in CONTAINER_SUB_CODECS:
        return False
    allowed = CONTAINER_SUB_CODECS[container_ext]
    return True if allowed is None else codec in allowed


# ==========================================
# 智能压缩：bpp 预筛
# ==========================================
SMART_BPP_SKIP = 0.090      # H.264 基准：低于此值视为已压得足够
SMART_BPP_REVIEW = 0.100    # H.264 基准：边缘区上限

# 编码效率系数（相对 H.264）。>1 表示更高效：同 bpp 画质更好，门槛上调、更倾向跳过
SMART_CODEC_FACTORS = {
    "h264": 1.00, "avc": 1.00,
    "hevc": 1.10, "h265": 1.10,
    "av1": 1.15,
    "vp9": 1.10,
    "vp8": 1.05,
    "mpeg4": 0.85,
    "mpeg2video": 0.75,
    "mpeg1video": 0.70,
    "h263": 0.70,
    "wmv2": 0.70, "wmv3": 0.70,
    "prores": 0.0, "dnxhd": 0.0, "ffv1": 0.0, "rawvideo": 0.0, "v210": 0.0,
}


def parse_frame_rate(value):
    """把 30000/1001、29.97 这类帧率写法转成浮点，失败返回 0"""
    try:
        text = str(value).strip()
        if not text or text in ("0/0", "N/A"):
            return 0.0
        if "/" in text:
            num, den = text.split("/", 1)
            den = float(den)
            fps = float(num) / den if den else 0.0
        else:
            fps = float(text)
        return fps if fps > 0 else 0.0
    except (ValueError, ZeroDivisionError, AttributeError):
        return 0.0


PROGRESS_LINE_RE = re.compile(r"^(frame|size|Lsize)\s*=")


def is_progress_line(line):
    """ffmpeg 的统计行总是以 frame= / size= / Lsize= 开头

    不能用 "size=" in line 判断：`Input #0 ... testsrc2=size=640x480` 这类
    流信息行也含 size=，会被误当成进度行而被节流吞掉。
    """
    return bool(PROGRESS_LINE_RE.match(line.lstrip()))


def normalize_progress_line(line):
    """规整 ffmpeg 进度行的空格：等号后不留空白，连续空白压成一个

    frame=   11 fps=0.0 q=30.0 size=       0KiB
    → frame=11 fps=0.0 q=30.0 size=0KiB

    只处理进度行，普通日志（含缩进、路径里的空格）原样返回。
    """
    if not is_progress_line(line):
        return line
    text = " ".join(line.split())
    return re.sub(r"=\s+", "=", text)


def smart_thresholds(codec_name, encoder_tag=""):
    """返回 (系数, 跳过阈值, 边缘上限)；x265 源再上调一档，避免同参重压"""
    factor = SMART_CODEC_FACTORS.get((codec_name or "").lower(), 1.0)
    if "x265" in (encoder_tag or "").lower():
        factor *= 1.15
    return factor, SMART_BPP_SKIP * factor, SMART_BPP_REVIEW * factor


# ---- VFR（可变帧率）判定与试压 ----
SMART_VFR_TOLERANCE = 0.02      # 名义帧率与平均帧率相差超过 2% 视为 VFR
SMART_TRIAL_SAVING = 0.25       # 试压后预计省下 25% 以上才算"值得压"
SMART_TRIAL_SECONDS = 6.0       # 单段试压时长
SMART_TRIAL_POINTS = (0.25, 0.75)   # 采样位置（片长的百分比），避开片头静止画面

_FPS_MODE_SUPPORT = {}


def is_vfr(r_fps, avg_fps):
    """名义帧率与平均帧率明显不一致 → VFR"""
    if r_fps <= 0 or avg_fps <= 0:
        return False
    return abs(r_fps - avg_fps) > max(r_fps, avg_fps) * SMART_VFR_TOLERANCE


def supports_fps_mode(ffmpeg_path):
    """-fps_mode 是 ffmpeg 5.0 才有的参数，旧版不加这个参数（默认同样保留时间戳）

    注意：这个函数会起一个子进程，不要在界面线程上临时调用（杀软扫 ffmpeg.exe
    时可能卡上一两秒）。启动时由 warm_up_fps_mode() 在后台预热，结果缓存复用。
    """
    if not ffmpeg_path:
        return False
    cached = _FPS_MODE_SUPPORT.get(ffmpeg_path)
    if cached is not None:
        return cached
    ok = False
    try:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        out = subprocess.run(
            [ffmpeg_path, "-version"], capture_output=True, text=True,
            encoding="utf-8", errors="ignore", timeout=15,
            startupinfo=startupinfo, creationflags=0x08000000,
        ).stdout
        m = re.search(r"ffmpeg version\s+(?:n)?(\d+)", out)
        if m:
            ok = int(m.group(1)) >= 5
    except Exception:
        ok = False
    _FPS_MODE_SUPPORT[ffmpeg_path] = ok
    return ok


def warm_up_fps_mode(ffmpeg_path):
    """启动后在后台把 -fps_mode 支持情况查好，避免生成任务时卡界面"""
    if not ffmpeg_path:
        return
    try:
        supports_fps_mode(ffmpeg_path)
    except Exception:
        pass


def build_ext_filter(name, exts):
    """把扩展名集合转换成 QFileDialog 可用的过滤器字符串"""
    return "{} ({})".format(name, " ".join("*" + ext for ext in sorted(exts)))


# Windows 进程异常终止码（NTSTATUS）：说明进程被系统直接终止，而非 ffmpeg 逻辑报错
NTSTATUS_HINTS = {
    3221225477: "进程崩溃：访问冲突，常见于内存或驱动异常",
    3221225501: "进程崩溃：非法指令，通常是编码器指令集或 CPU 稳定性问题",
    3221225725: "进程崩溃：栈溢出",
    3221226505: "进程崩溃：栈缓冲区越界",
    3221225786: "进程被外部中断",
}

# ffmpeg 在 Windows 下常把负的 AVERROR(errno) 当退出码，按无符号 32 位呈现
ERRNO_HINTS = {
    -2: "找不到文件或路径（ENOENT），请检查文件是否被移动/删除",
    -5: "读写出错（EIO），可能是磁盘问题或文件损坏",
    -13: "没有访问权限（EACCES）",
    -22: "参数无效（EINVAL），请检查输入文件或命令参数",
    -28: "磁盘空间不足（ENOSPC）",
}


def describe_return_code(code):
    """把 ffmpeg 返回码翻译成可读说明"""
    if code is None:
        return "未取得返回码"

    # 0) 非 Windows 平台被信号强制结束时为负数
    if code < 0:
        return "{}（进程被信号 {} 终止）".format(code, -code)

    # 1) 系统级异常终止（崩溃）
    if 0xC0000000 <= code <= 0xC0000FFF:
        hint = NTSTATUS_HINTS.get(code, "进程被系统异常终止")
        return "{}（0x{:08X}：{}）".format(code, code, hint)

    # 2) 负 errno / 负的 AVERROR 被当成无符号数
    if code >= 0x80000000:
        signed = code - 0x100000000
        hint = ERRNO_HINTS.get(signed, "FFmpeg 内部错误码")
        return "{}（0x{:08X}，即 {}：{}）".format(code, code, signed, hint)

    # 3) 普通退出码
    if code == 1:
        return "1（FFmpeg 执行失败：参数、编码或输入文件问题）"
    return "{}（0x{:08X}）".format(code, code)


# 崩溃自动重试：仅针对进程级崩溃，普通报错重试也没有意义
CRASH_RETRY_TIMES = 1
CRASH_RETRY_DELAY = 3.0


def is_crash_code(code):
    """返回码是否属于进程级崩溃（0xC0000000 ~ 0xC0000FFF）"""
    return isinstance(code, int) and 0xC0000000 <= code <= 0xC0000FFF


def suppress_windows_error_dialogs():
    """关闭 WER 崩溃弹窗。否则 ffmpeg 崩溃时进程会被对话框挂住，重试无从触发。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        SEM_FAILCRITICALERRORS = 0x0001
        SEM_NOGPFAULTERRORBOX = 0x0002
        SEM_NOOPENFILEERRORBOX = 0x8000
        ctypes.windll.kernel32.SetErrorMode(
            SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX
        )
    except Exception:
        pass






class DropListWidget(QListWidget):
    """支持拖入文件/文件夹的列表控件"""
    files_dropped = pyqtSignal(list)   # 拖入后收集到的文件路径列表

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if not event.mimeData().hasUrls():
            event.ignore()
            return

        paths = []
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if not path:
                continue
            if os.path.isdir(path):
                for root, _, names in os.walk(path):
                    for name in names:
                        if os.path.splitext(name)[1].lower() in MEDIA_EXTS:
                            paths.append(os.path.join(root, name))
            elif os.path.isfile(path):
                if os.path.splitext(path)[1].lower() in MEDIA_EXTS:
                    paths.append(path)

        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()


class DropSubtitleEdit(QLineEdit):
    """支持拖入字幕文件的输入框"""
    file_dropped = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if not event.mimeData().hasUrls():
            event.ignore()
            return
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if os.path.isfile(path) and os.path.splitext(path)[1].lower() in SUB_EXTS:
                self.setText(path)
                self.file_dropped.emit(path)
                event.acceptProposedAction()
                return
        event.ignore()

# ==========================================
# 核心执行线程，防止界面卡死
# ==========================================
class FFmpegWorker(QThread):
    progress_signal = pyqtSignal(int, int)  # 当前进度, 总数
    log_signal = pyqtSignal(str)  # 日志输出
    finished_signal = pyqtSignal(list)  # 完成信号，携带每个任务的执行结果

    def __init__(self, tasks):
        super().__init__()
        self.tasks = tasks  # 包含 (cmd, output_file) 的列表
        self.process = None
        self.is_paused = False
        self.is_stopped = False
        self.results = []   # 每个任务的执行台账
        self.current_output = ""   # 正在处理的任务输出路径

    def pause_process(self):
        if self.process and not self.is_stopped:
            try:
                psutil.Process(self.process.pid).suspend()
                self.is_paused = True
            except Exception as e:
                self.log_signal.emit(f"⚠️ 暂停失败: {e}")

    def resume_process(self):
        if self.process and not self.is_stopped and self.is_paused:
            try:
                psutil.Process(self.process.pid).resume()
                self.is_paused = False
            except Exception as e:
                self.log_signal.emit(f"⚠️ 恢复失败: {e}")

    def stop_process(self):
        self.is_stopped = True
        if self.process:
            try:
                if self.is_paused:
                    psutil.Process(self.process.pid).resume()
                self.process.terminate()
            except Exception:
                pass

    @staticmethod
    def _input_from_cmd(cmd):
        """从命令里取出第一个 -i 的输入路径（合并模式拿到的是临时列表文件）"""
        try:
            return cmd[cmd.index("-i") + 1]
        except (ValueError, IndexError):
            return ""

    @staticmethod
    def _file_meta(path):
        try:
            st = os.stat(path)
            return (st.st_size, st.st_mtime)
        except OSError:
            return None

    @classmethod
    def _cleanup_partial(cls, output_file, source_file, before_meta):
        """删除失败/中止产生的半成品。

        两条保护：不删源文件；执行前就存在且本次未被改动的文件（可能是上一次的成品）也不删。
        """
        try:
            if not output_file or not os.path.exists(output_file):
                return False
            if source_file and os.path.normpath(output_file) == os.path.normpath(source_file):
                return False
            after_meta = cls._file_meta(output_file)
            if after_meta is None:
                return False
            if before_meta is not None and after_meta == before_meta:
                return False
            os.remove(output_file)
            return True
        except OSError:
            return False

    def _wait_interruptible(self, seconds):
        """重试前的等待，期间可被停止操作打断"""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.is_stopped:
                return False
            time.sleep(0.2)
        return True

    def _run_attempt(self, cmd, output_file):
        """执行一次 ffmpeg，返回 (返回码, 是否被用户停止)"""
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE

        # 智能音频占位符处理（用副本，避免重试时占位符已被清掉）
        if "__SMART_AUDIO__" in cmd:
            cmd = list(cmd)
            cmd.remove("__SMART_AUDIO__")
            input_file = self._input_from_cmd(cmd)
            audio_params = []
            if input_file:
                audio_bitrate = self.get_audio_bitrate(input_file)
                if audio_bitrate > 192000:
                    self.log_signal.emit(f"   🔊 音频智能转码 (源码率 {audio_bitrate/1000:.0f}k > 192k)")
                    audio_params = ["-c:a", "aac", "-b:a", "192k"]
                else:
                    self.log_signal.emit(f"   🔊 音频智能复制 (源码率 {audio_bitrate/1000:.0f}k <= 192k)")
                    audio_params = ["-c:a", "copy"]
            else:
                audio_params = ["-c:a", "copy"]
            out_file = cmd[-1]
            cmd = cmd[:-1] + audio_params + [out_file]

        self.log_signal.emit(f"💻 执行命令: {subprocess.list2cmdline(cmd)}")

        self.process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="ignore", startupinfo=startupinfo,
        )
        self.is_paused = False

        last_emit = 0
        last_emitted = ""
        last_progress = ""
        try:
            for line in self.process.stdout:
                # 如果中途被强制停止，直接跳出读取循环
                if self.is_stopped:
                    break

                line = line.strip()
                if not line:
                    continue

                if "error" in line.lower() or "warning" in line.lower():
                    self.log_signal.emit(line)
                    continue

                if is_progress_line(line):
                    # 进度行密度高，2 秒才显示一条，避免刷屏
                    text = normalize_progress_line(line)
                    last_progress = text
                    now = time.time()
                    if now - last_emit >= 2.0:
                        self.log_signal.emit(text)
                        last_emit = now
                        last_emitted = text
                else:
                    self.log_signal.emit(line)
        except Exception as read_err:
            self.log_signal.emit(f"⚠️ 读取日志时发生中断: {read_err}")
        finally:
            # 安全地等待并关闭
            if self.process:
                self.process.wait()
                if self.process.stdout:
                    self.process.stdout.close()

        # 补发最后一条进度（例如收尾的 Lsize 行），已经显示过的不重复打印
        if last_progress and last_progress != last_emitted:
            self.log_signal.emit(last_progress)

        # 在置空 self.process 之前，先获取 returncode
        return_code = None
        if self.process:
            return_code = self.process.returncode
        self.process = None
        return return_code, self.is_stopped

    def run(self):
        total = len(self.tasks)
        self.results = []
        max_attempts = 1 + CRASH_RETRY_TIMES

        for idx, (cmd, output_file) in enumerate(self.tasks):
            if self.is_stopped:
                break

            self.log_signal.emit(f"\n[{idx+1}/{total}] 开始处理: {os.path.basename(output_file)}")

            # 本任务执行台账：成功/失败、返回码、尝试次数
            record = {
                "output": output_file,
                "code": None,
                "ok": False,
                "stopped": False,
                "error": "",
                "attempts": 0,
                "retried": False,
            }
            source_file = self._input_from_cmd(cmd)
            attempt = 0
            self.current_output = output_file

            while attempt < max_attempts:
                attempt += 1
                record["attempts"] = attempt
                if attempt > 1:
                    self.log_signal.emit(
                        f"🔄 第 {attempt}/{max_attempts} 次尝试: {os.path.basename(output_file)}"
                    )

                before_meta = self._file_meta(output_file)
                try:
                    return_code, aborted = self._run_attempt(cmd, output_file)
                except Exception as e:
                    record["error"] = str(e)
                    self.log_signal.emit(f"❌ 发生严重异常: {str(e)}")
                    self.process = None
                    if self._cleanup_partial(output_file, source_file, before_meta):
                        self.log_signal.emit(f"🧹 已清理半成品: {os.path.basename(output_file)}")
                    break

                # 用户中途点了停止
                if aborted:
                    cleaned = self._cleanup_partial(output_file, source_file, before_meta)
                    self.log_signal.emit(
                        "⏹ 任务被终止"
                        + (f"，已清理半成品: {os.path.basename(output_file)}" if cleaned else "")
                    )
                    record["stopped"] = True
                    record["error"] = "任务被手动停止"
                    break

                record["code"] = return_code

                if return_code == 0:
                    record["ok"] = True
                    record["retried"] = attempt > 1
                    prefix = "✅ 重试后成功" if attempt > 1 else "✅ 成功"
                    self.log_signal.emit(f"{prefix}: {os.path.basename(output_file)}")
                    break

                # 失败：一律清理半成品
                cleaned = self._cleanup_partial(output_file, source_file, before_meta)

                # 仅进程级崩溃才自动重试，普通报错直接失败
                if is_crash_code(return_code) and attempt < max_attempts:
                    self.log_signal.emit(
                        f"⚠️ 异常退出: {os.path.basename(output_file)} "
                        f"返回码 {describe_return_code(return_code)}"
                        + ("，已清理半成品" if cleaned else "")
                    )
                    self.log_signal.emit(
                        f"⏳ {int(CRASH_RETRY_DELAY)} 秒后自动重试"
                        f"（第 {attempt + 1}/{max_attempts} 次）"
                    )
                    if not self._wait_interruptible(CRASH_RETRY_DELAY):
                        record["stopped"] = True
                        record["error"] = "重试等待期间被停止"
                        break
                    continue

                self.log_signal.emit(
                    f"❌ 失败: {os.path.basename(output_file)} "
                    f"返回码 {describe_return_code(return_code)}"
                    + ("，已清理半成品" if cleaned else "")
                )
                break

            self.results.append(record)
            self.current_output = ""
            self.progress_signal.emit(idx + 1, total)

        self.finished_signal.emit(self.results)

    def get_audio_bitrate(self, file_path):
        """获取第一条音频流的码率(bps)，失败返回0"""
        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            
            # 用 ffprobe 查找 ffmpeg 的路径
            ffprobe_path = shutil.which("ffprobe")
            if not ffprobe_path:
                return 0

            cmd = [
                ffprobe_path, "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=bit_rate",
                "-of", "default=noprint_wrappers=1:nokey=1",
                file_path
            ]
            res = subprocess.run(
                cmd, capture_output=True, text=True, 
                check=True, timeout=5, startupinfo=startupinfo
            )
            out = res.stdout.strip()
            return int(float(out)) if out and out != "N/A" else 0
        except Exception:
            return 0

class DurationWorker(QThread):
    """后台读取媒体时长和文件大小，避免阻塞 UI"""
    result_signal = pyqtSignal(str, float, float)   # file_path, duration, size_mb

    def __init__(self, ffprobe_path, file_path):
        super().__init__()
        self.ffprobe_path = ffprobe_path
        self.file_path = file_path

    def run(self):
        if not self.ffprobe_path:
            return
        try:
            cmd = [
                self.ffprobe_path, "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "csv=p=0", self.file_path
            ]
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE

            result = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="ignore", check=True, timeout=10,
                startupinfo=startupinfo
            )
            duration = float(result.stdout.strip())

            # 取文件大小（MB）
            try:
                size_mb = os.path.getsize(self.file_path) / 1024 / 1024
            except OSError:
                size_mb = 0.0

            self.result_signal.emit(self.file_path, duration, size_mb)
        except Exception:
            pass

class MediaInfoWorker(QThread):
    """后台批量读取媒体信息，避免阻塞 UI"""
    info_signal = pyqtSignal(str)       # 每行日志
    finished_signal = pyqtSignal()      # 全部完成

    def __init__(self, ffprobe_path, files):
        super().__init__()
        self.ffprobe_path = ffprobe_path
        self.files = files

    def probe_one(self, file_path):
        """用 ffprobe 获取完整媒体信息，返回字典"""
        if not self.ffprobe_path:
            return {"error": "未找到 ffprobe，请检查 FFmpeg 环境"}
        try:
            cmd = [
                self.ffprobe_path,
                "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                file_path,
            ]
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE

            result = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="ignore", check=True,
                timeout=15, startupinfo=startupinfo
            )
            return json.loads(result.stdout)
        except Exception as e:
            return {"error": str(e)}

    def fmt_time(self, seconds):
        seconds = int(seconds)
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    def run(self):
        self.info_signal.emit("=" * 60)
        self.info_signal.emit(f"📽️ 媒体信息查询（共 {len(self.files)} 个文件）")
        self.info_signal.emit("=" * 60)

        for file_path in self.files:
            info = self.probe_one(file_path)
            if "error" in info:
                self.info_signal.emit(f"\n❌ {os.path.basename(file_path)}")
                self.info_signal.emit(f"   错误: {info['error']}")
                continue

            self.info_signal.emit(f"\n📁 文件: {os.path.basename(file_path)}")
            self.info_signal.emit(f"   路径: {file_path}")

            fmt = info.get("format", {})

            try:
                duration = float(fmt.get("duration", 0))
                duration_str = f"{self.fmt_time(duration)} ({duration:.2f} 秒)"
            except (ValueError, TypeError):
                duration_str = "未知"

            try:
                size_str = f"{int(fmt.get('size', 0)) / 1024 / 1024:.2f} MB"
            except (ValueError, TypeError):
                size_str = "未知"

            try:
                bit_rate_str = f"{int(fmt.get('bit_rate', 0)) / 1000:.0f} kbps"
            except (ValueError, TypeError):
                bit_rate_str = "未知"

            self.info_signal.emit(f"   格式: {fmt.get('format_long_name', '未知')}")
            self.info_signal.emit(f"   时长: {duration_str}")
            self.info_signal.emit(f"   大小: {size_str}")
            self.info_signal.emit(f"   总码率: {bit_rate_str}")

            for stream in info.get("streams", []):
                codec_type = stream.get("codec_type", "")
                index = stream.get("index", "?")
                codec = stream.get("codec_name", "未知")

                if codec_type == "video":
                    self.info_signal.emit(f"   🎬 视频流 #{index}:")
                    self.info_signal.emit(f"      编码: {codec}")
                    self.info_signal.emit(f"      分辨率: {stream.get('width')}x{stream.get('height')}")
                    self.info_signal.emit(f"      帧率: {stream.get('r_frame_rate', '未知')}")
                    self.info_signal.emit(f"      像素格式: {stream.get('pix_fmt', '未知')}")
                elif codec_type == "audio":
                    self.info_signal.emit(f"   🔊 音频流 #{index}:")
                    self.info_signal.emit(f"      编码: {codec}")
                    self.info_signal.emit(f"      采样率: {stream.get('sample_rate', '未知')} Hz")
                    self.info_signal.emit(f"      声道数: {stream.get('channels', '未知')}")
                elif codec_type == "subtitle":
                    self.info_signal.emit(f"   📝 字幕流 #{index}:")
                    self.info_signal.emit(f"      编码: {codec}")

        self.info_signal.emit("\n" + "=" * 60 + "\n")
        self.finished_signal.emit()

class OverheadScannerWorker(QThread):
    """后台扫描文件冗余度"""
    log_signal = pyqtSignal(str)
    finished_signal = pyqtSignal()

    def __init__(self, ffprobe_path, files):
        super().__init__()
        self.ffprobe_path = ffprobe_path
        self.files = files

    def run(self):
        self.log_signal.emit("=" * 60)
        self.log_signal.emit(f"🔍 开始扫描文件冗余度（共 {len(self.files)} 个文件）")
        self.log_signal.emit("=" * 60)

        for file_path in self.files:
            filename = os.path.basename(file_path)
            try:
                cmd = [
                    self.ffprobe_path, "-v", "error",
                    "-show_entries", "format=bit_rate,size,duration",
                    "-show_entries", "stream=codec_type,bit_rate",
                    "-of", "json", file_path
                ]
                
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE

                result = subprocess.run(
                    cmd, capture_output=True, text=True,
                    encoding="utf-8", errors="ignore", check=True,
                    timeout=15, startupinfo=startupinfo
                )
                
                d = json.loads(result.stdout)
                
                # 计算容器总码率
                fmt = d.get('format', {})
                fmt_rate_str = fmt.get('bit_rate', '0')
                
                # 兼容某些容器没有 bit_rate 的情况，用大小和时长反推
                if fmt_rate_str in ('0', 'N/A') and 'size' in fmt and 'duration' in fmt:
                    try:
                        size = int(fmt['size'])
                        duration = float(fmt['duration'])
                        fmt_rate = (size * 8) / duration if duration > 0 else 0
                    except:
                        fmt_rate = 0
                else:
                    try:
                        fmt_rate = int(float(fmt_rate_str))
                    except:
                        fmt_rate = 0

                # 计算音视频流码率之和
                stream_rate = 0
                for s in d.get('streams', []):
                    if s.get('codec_type') in ('video', 'audio'):
                        br = s.get('bit_rate', '0')
                        try:
                            stream_rate += int(float(br))
                        except:
                            pass

                # 判断冗余度
                if fmt_rate > 0 and stream_rate > 0:
                    overhead = (fmt_rate - stream_rate) / fmt_rate * 100
                    
                    # 防止某些 VBR 流码率虚高导致负数
                    if overhead < 0:
                        overhead = 0.0
                        
                    if overhead > 15:
                        status = "⚠️ 存在明显封装冗余或垃圾数据"
                    else:
                        status = "✅ 码率匹配正常"
                        
                    self.log_signal.emit(f"📄 {filename}")
                    self.log_signal.emit(f"   容器总码率: {fmt_rate/1e6:.2f} Mbps | 流码率之和: {stream_rate/1e6:.2f} Mbps")
                    self.log_signal.emit(f"   冗余占比: {overhead:.1f}% - {status}")
                else:
                    self.log_signal.emit(f"📄 {filename}")
                    self.log_signal.emit("   ❓ 无法计算（可能缺少元数据或流码率信息）")
                    
            except Exception as e:
                self.log_signal.emit(f"📄 {filename}")
                self.log_signal.emit(f"   ❌ 扫描失败: {str(e)}")
                
            self.log_signal.emit("")  # 空行分隔

        self.log_signal.emit("=" * 60 + "\n")
        self.finished_signal.emit()

class TrackProbeWorker(QThread):
    """后台读取单个文件的轨道列表（视频/音频/字幕/附件）"""
    result_signal = pyqtSignal(dict)

    def __init__(self, ffprobe_path, file_path):
        super().__init__()
        self.ffprobe_path = ffprobe_path
        self.file_path = file_path

    def run(self):
        if not self.ffprobe_path:
            self.result_signal.emit({"error": "未找到 ffprobe，无法读取轨道信息"})
            return
        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE

            cmd = [
                self.ffprobe_path, "-v", "error",
                "-print_format", "json",
                "-show_streams", "-show_format",
                self.file_path,
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="ignore", check=True,
                timeout=20, startupinfo=startupinfo,
            )
            self.result_signal.emit(self._parse(json.loads(result.stdout)))
        except Exception as e:
            self.result_signal.emit({"error": str(e)})

    def _parse(self, data):
        tracks = []
        counters = {}
        for s in data.get("streams", []):
            kind = s.get("codec_type", "")
            if kind not in ("video", "audio", "subtitle", "attachment"):
                continue

            disp = s.get("disposition", {}) or {}
            # 封面图在 mp4 里是 video 流，归到附件，避免显示成"第二条视频轨"
            if kind == "video" and disp.get("attached_pic"):
                kind = "attachment"

            counters[kind] = counters.get(kind, 0) + 1
            tags = s.get("tags", {}) or {}
            tracks.append({
                "index": s.get("index", -1),
                "kind": kind,
                "ordinal": counters[kind],
                "codec": s.get("codec_name", "未知"),
                "lang": (tags.get("language") or "").strip(),
                "title": (tags.get("title") or "").strip(),
                "channels": s.get("channels", 0),
                "sample_rate": s.get("sample_rate", ""),
                "bitrate": self._kbps(s.get("bit_rate")),
                "width": s.get("width", 0),
                "height": s.get("height", 0),
                "fps": self._fps(s.get("r_frame_rate")),
                "default": bool(disp.get("default", 0)),
                "forced": bool(disp.get("forced", 0)),
            })

        fmt = data.get("format", {}) or {}
        return {
            "path": self.file_path,
            "tracks": tracks,
            "format": fmt,
            "error": "",
        }

    @staticmethod
    def _kbps(value):
        try:
            v = int(float(value))
            return f"{v // 1000} kbps" if v > 0 else ""
        except (TypeError, ValueError):
            return ""

    @staticmethod
    def _fps(value):
        try:
            num, den = str(value).split("/")
            fps = float(num) / float(den)
            return f"{fps:.0f} fps" if fps > 0 else ""
        except (ValueError, ZeroDivisionError, AttributeError):
            return ""


class TrackDialog(QDialog):
    """轨道管理：勾选要保留的音轨/字幕轨，确定后由主窗口执行无编码重封装"""

    def __init__(self, parent=None, file_path="", ffprobe_path=None):
        super().__init__(parent)
        self.setWindowTitle("轨道管理")
        self.resize(720, 540)
        self.setMinimumSize(620, 420)

        self.file_path = file_path
        self.ffprobe_path = ffprobe_path
        self.ext = os.path.splitext(file_path)[1].lower()
        self.plan = None          # 确定后写回主窗口
        self.info = None
        self.checks = {}          # 绝对流索引 -> (QCheckBox, track)
        self._worker = None

        outer = QVBoxLayout(self)
        outer.setSpacing(8)

        self.lbl_title = QLabel(f"文件：{os.path.basename(file_path)}")
        self.lbl_title.setStyleSheet("font-weight: bold;")
        outer.addWidget(self.lbl_title)

        self.lbl_meta = QLabel("正在读取轨道信息…")
        self.lbl_meta.setStyleSheet("color: #666;")
        outer.addWidget(self.lbl_meta)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.inner = QWidget()
        self.tracks_layout = QVBoxLayout(self.inner)
        self.tracks_layout.setSpacing(8)
        self.scroll.setWidget(self.inner)
        outer.addWidget(self.scroll, 1)

        self.lbl_hint = QLabel(
            "勾选 = 保留，取消 = 删除。全程 -c copy，不重新编码，画面与原片完全一致。"
        )
        self.lbl_hint.setStyleSheet("color: #666;")
        outer.addWidget(self.lbl_hint)

        btn_row = QHBoxLayout()
        self.btn_all = QPushButton("全选")
        self.btn_none = QPushButton("全不选")
        self.btn_ok = QPushButton("确定")
        self.btn_ok.setObjectName("primaryButton")
        self.btn_ok.setEnabled(False)
        self.btn_cancel = QPushButton("取消")
        btn_row.addWidget(self.btn_all)
        btn_row.addWidget(self.btn_none)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_cancel)
        btn_row.addWidget(self.btn_ok)
        outer.addLayout(btn_row)

        self.btn_all.clicked.connect(lambda: self._set_all(True))
        self.btn_none.clicked.connect(lambda: self._set_all(False))
        self.btn_ok.clicked.connect(self._on_ok)
        self.btn_cancel.clicked.connect(self.reject)

        self._start_probe()

    def _start_probe(self):
        self._worker = TrackProbeWorker(self.ffprobe_path, self.file_path)
        self._worker.result_signal.connect(self._on_probed)
        self._worker.start()

    def _on_probed(self, info):
        self.info = info
        if info.get("error"):
            self.lbl_meta.setText(f"❌ 读取失败：{info['error']}")
            return

        self.lbl_meta.setText(self._meta_text(info))
        self._build_rows(info)
        self.btn_ok.setEnabled(True)

    def _meta_text(self, info):
        fmt = info.get("format", {}) or {}
        try:
            total = int(float(fmt.get("duration", 0) or 0))
            duration = f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"
        except (TypeError, ValueError):
            duration = "未知时长"

        counts = {}
        for t in info.get("tracks", []):
            counts[t["kind"]] = counts.get(t["kind"], 0) + 1

        parts = [self.ext.lstrip(".").upper() or "未知容器", duration]
        if counts.get("video"):
            parts.append(f"视频 {counts['video']} 条")
        if counts.get("audio"):
            parts.append(f"音频 {counts['audio']} 条")
        if counts.get("subtitle"):
            parts.append(f"字幕 {counts['subtitle']} 条")
        if counts.get("attachment"):
            parts.append(f"附件 {counts['attachment']} 个")
        return " · ".join(parts) + "　（索引按文件内流序号）"

    def _build_rows(self, info):
        tracks = info.get("tracks", [])

        video = [t for t in tracks if t["kind"] == "video"]
        if video:
            box = QGroupBox(f"视频轨（{len(video)} 条，不可删除）")
            lay = QVBoxLayout(box)
            for t in video:
                lay.addWidget(QLabel(self._row_text(t, "V")))
            for i in range(lay.count()):
                lay.itemAt(i).widget().setStyleSheet("color: #888;")
            self.tracks_layout.addWidget(box)

        audio = [t for t in tracks if t["kind"] == "audio"]
        box = QGroupBox(f"音频轨（{len(audio)} 条）")
        lay = QVBoxLayout(box)
        if not audio:
            empty = QLabel("此文件没有音频轨")
            empty.setStyleSheet("color: #888;")
            lay.addWidget(empty)
        for t in audio:
            cb = QCheckBox(self._row_text(t, "A"))
            cb.setChecked(True)
            lay.addWidget(cb)
            self.checks[t["index"]] = (cb, t)
        self.tracks_layout.addWidget(box)

        subs = [t for t in tracks if t["kind"] == "subtitle"]
        box = QGroupBox(f"字幕轨（{len(subs)} 条）")
        lay = QVBoxLayout(box)
        if not subs:
            empty = QLabel("此文件没有字幕轨")
            empty.setStyleSheet("color: #888;")
            lay.addWidget(empty)
        for t in subs:
            cb = QCheckBox(self._row_text(t, "S"))
            if subtitle_codec_allowed(self.ext, t["codec"]):
                cb.setChecked(True)
            else:
                # 容器装不下这种字幕编码，只能删除，避免执行时报错
                cb.setChecked(False)
                cb.setEnabled(False)
                cb.setText(cb.text() + "　—— 当前容器不支持该编码，只能删除")
                cb.setStyleSheet("color: #999;")
            lay.addWidget(cb)
            self.checks[t["index"]] = (cb, t)
        self.tracks_layout.addWidget(box)

        attachments = [t for t in tracks if t["kind"] == "attachment"]
        if attachments:
            note = QLabel(
                f"附件 {len(attachments)} 个（字体、封面等），"
                + ("输出 MKV 时自动原样保留" if self.ext == ".mkv" else "当前容器不保留附件")
            )
            note.setStyleSheet("color: #888;")
            self.tracks_layout.addWidget(note)

        self.tracks_layout.addStretch()

    @staticmethod
    def _row_text(t, prefix):
        parts = [f"{prefix}{t['ordinal']}", t["codec"]]
        if t.get("lang"):
            parts.append(t["lang"])
        if t.get("title"):
            parts.append(t["title"])
        if t["kind"] == "video":
            if t.get("width") and t.get("height"):
                parts.append(f"{t['width']}×{t['height']}")
            if t.get("fps"):
                parts.append(t["fps"])
        if t["kind"] == "audio":
            if t.get("channels"):
                parts.append(f"{t['channels']} 声道")
        if t.get("bitrate"):
            parts.append(t["bitrate"])
        if t.get("default"):
            parts.append("default")
        if t.get("forced"):
            parts.append("forced")
        return "   ".join(parts)

    def _set_all(self, checked):
        for cb, _ in self.checks.values():
            if cb.isEnabled():
                cb.setChecked(checked)

    def _on_ok(self):
        keep = [t["index"] for t in self.info.get("tracks", []) if t["kind"] == "video"]
        keep_audio, keep_sub = [], []
        default_audio, default_sub = None, None

        for idx, (cb, t) in self.checks.items():
            if t["kind"] == "audio" and t.get("default"):
                default_audio = idx
            if t["kind"] == "subtitle" and t.get("default"):
                default_sub = idx
            if cb.isChecked():
                keep.append(idx)
                if t["kind"] == "audio":
                    keep_audio.append(idx)
                elif t["kind"] == "subtitle":
                    keep_sub.append(idx)

        if not keep_audio:
            res = QMessageBox.question(
                self, "确认删除音频",
                "没有保留任何音频轨，输出的视频将没有声音。\n\n确定继续吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if res != QMessageBox.Yes:
                return

        try:
            size = os.path.getsize(self.file_path)
            mtime = os.path.getmtime(self.file_path)
        except OSError as e:
            QMessageBox.warning(self, "提示", f"无法读取源文件信息：{e}")
            return

        self.plan = {
            "path": self.file_path,
            "size": size,
            "mtime": mtime,
            "keep": sorted(keep),
            "keep_audio": keep_audio,
            "keep_sub": keep_sub,
            # 原默认轨被删掉时，把剩下的第一条同类型轨设为默认
            "fix_default_audio": bool(keep_audio) and default_audio is not None
                                 and default_audio not in keep_audio,
            "fix_default_sub": bool(keep_sub) and default_sub is not None
                               and default_sub not in keep_sub,
        }
        self.accept()

    def closeEvent(self, event):
        # 探测线程未结束时等待，避免 QThread 被销毁导致崩溃
        if self._worker and self._worker.isRunning():
            self._worker.wait(3000)
        super().closeEvent(event)


class SmartScanWorker(QThread):
    """后台计算 bpp。接口与 FFmpegWorker 对齐，便于复用暂停 / 停止按钮。"""
    progress_signal = pyqtSignal(int, int)     # 已检测数, 总数
    finished_signal = pyqtSignal(list, bool)   # 结果列表, 是否被取消

    def __init__(self, ffprobe_path, files):
        super().__init__()
        self.ffprobe_path = ffprobe_path
        self.files = files
        self.is_paused = False
        self.is_stopped = False
        self.results = []

    def pause_process(self):
        self.is_paused = True

    def resume_process(self):
        self.is_paused = False

    def stop_process(self):
        self.is_stopped = True
        self.is_paused = False

    def _paused_gate(self):
        while self.is_paused and not self.is_stopped:
            time.sleep(0.1)
        return not self.is_stopped

    def run(self):
        total = len(self.files)
        results = []
        for idx, path in enumerate(self.files):
            if not self._paused_gate():
                break
            results.append(self.scan_one(path))
            self.progress_signal.emit(idx + 1, total)
        self.results = results
        self.finished_signal.emit(results, self.is_stopped)

    def scan_one(self, path):
        item = {
            "path": path,
            "name": os.path.basename(path),
            "ok": False,
            "reason": "",
            "codec": "",
            "encoder": "",
            "width": 0,
            "height": 0,
            "fps": 0.0,
            "r_fps": 0.0,
            "avg_fps": 0.0,
            "vfr": False,
            "duration": 0.0,
            "size": 0,
            "audio_bitrate": 0,
            "trial": None,
            "bitrate": 0,
            "bitrate_source": "",
            "bpp": 0.0,
            "factor": 1.0,
            "skip": SMART_BPP_SKIP,
            "review": SMART_BPP_REVIEW,
            "verdict": "error",
        }

        if not self.ffprobe_path:
            item["reason"] = "未找到 ffprobe"
            return item

        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            cmd = [
                self.ffprobe_path, "-v", "error",
                "-print_format", "json",
                "-show_streams", "-show_format",
                path,
            ]
            res = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="ignore", check=True,
                timeout=20, startupinfo=startupinfo,
            )
            data = json.loads(res.stdout)
        except subprocess.CalledProcessError:
            item["reason"] = "无法读取（损坏/不支持）"
            return item
        except Exception as e:
            item["reason"] = f"读取失败：{e}"
            return item

        video = None
        for s in data.get("streams", []):
            disp = s.get("disposition", {}) or {}
            if s.get("codec_type") == "video" and not disp.get("attached_pic"):
                video = s
                break
        if video is None:
            item["reason"] = "没有视频流"
            return item

        fmt = data.get("format", {}) or {}
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
        # bpp 用平均帧率算更贴近真实内容；名义帧率只用来判断 VFR
        r_fps = parse_frame_rate(video.get("r_frame_rate"))
        avg_fps = parse_frame_rate(video.get("avg_frame_rate"))
        fps = avg_fps or r_fps
        vfr = is_vfr(r_fps, avg_fps)

        duration = self._float(fmt.get("duration"))
        size = self._int(fmt.get("size")) or os.path.getsize(path)
        audio_rate = 0
        for s in data.get("streams", []):
            if s.get("codec_type") == "audio":
                audio_rate += self._int(s.get("bit_rate"))

        vbitrate = self._int(video.get("bit_rate"))
        source = "视频流码率"
        if vbitrate <= 0:
            total_rate = self._int(fmt.get("bit_rate"))
            if total_rate <= 0:
                if size > 0 and duration > 0:
                    total_rate = int(size * 8 / duration)
            if total_rate > 0:
                if 0 < audio_rate < total_rate:
                    vbitrate = total_rate - audio_rate
                    source = "总码率减音频"
                else:
                    vbitrate = total_rate
                    source = "总码率估算"

        item.update({
            "codec": video.get("codec_name", "未知"),
            "encoder": (video.get("tags", {}) or {}).get("encoder", ""),
            "width": width,
            "height": height,
            "fps": fps,
            "r_fps": r_fps,
            "avg_fps": avg_fps,
            "vfr": vfr,
            "duration": duration,
            "size": size,
            "audio_bitrate": audio_rate,
            "bitrate": vbitrate,
            "bitrate_source": source,
        })

        if width <= 0 or height <= 0:
            item["reason"] = "分辨率无效"
            return item
        if fps <= 0:
            item["reason"] = "帧率无效"
            return item
        if vbitrate <= 0:
            item["reason"] = "无法获取视频码率"
            return item

        bpp = vbitrate / (width * height * fps)
        factor, skip, review = smart_thresholds(item["codec"], item["encoder"])
        if vfr and bpp < review:
            # VFR 的 bpp 不可靠（帧率口径不一致 + 帧间冗余高），交给试压判断
            verdict = "vfr"
        elif bpp < skip:
            verdict = "skip"
        elif bpp < review:
            verdict = "review"
        else:
            verdict = "ok"

        item.update({
            "ok": True,
            "bpp": bpp,
            "factor": factor,
            "skip": skip,
            "review": review,
            "verdict": verdict,
        })
        return item

    @staticmethod
    def _int(value):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0


class SmartTrialWorker(QThread):
    """抽样试压：用与正式压缩相同的参数压两段短片，按比例估算全片体积

    只对 VFR 或用户手动勾选的文件跑，避免批量时每个文件都多花十几秒。
    """

    progress_signal = pyqtSignal(int, int, str)   # 已完成, 总数, 当前文件名
    file_signal = pyqtSignal(int, dict)           # 行号, 试压结果
    finished_signal = pyqtSignal(bool)            # 是否被取消
    log_signal = pyqtSignal(str)                  # 命令行与过程输出（转到主日志）

    def __init__(self, ffmpeg_path, ffprobe_path, jobs):
        super().__init__()
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.jobs = jobs            # [{row, path, name, crf, fps, duration, size, audio_bitrate}]
        self.is_stopped = False
        self._proc = None

    def stop_process(self):
        self.is_stopped = True
        proc = self._proc
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass

    def run(self):
        total = len(self.jobs)
        for idx, job in enumerate(self.jobs):
            if self.is_stopped:
                break
            self.log_signal.emit(
                "🧪 试压 {}/{}：{}".format(idx + 1, total, job["name"])
            )
            self.progress_signal.emit(idx, total, job["name"])
            result = self._trial_one(job)
            self.file_signal.emit(job["row"], result)
            self.progress_signal.emit(idx + 1, total, job["name"])
        self.finished_signal.emit(self.is_stopped)

    def _trial_one(self, job):
        duration = job.get("duration") or 0
        if duration <= 0:
            return {"ok": False, "error": "时长未知", "saving": 0.0, "est_bytes": 0}

        window = min(SMART_TRIAL_SECONDS, max(duration / 3.0, 0.5))
        tmpdir = tempfile.mkdtemp(prefix="ffmpeg-gui-trial-")
        total_bytes = 0
        total_secs = 0.0
        ok_samples = 0
        try:
            for idx, ratio in enumerate(SMART_TRIAL_POINTS):
                if self.is_stopped:
                    break
                start = duration * ratio - window / 2.0
                start = max(0.0, min(start, max(duration - window, 0.0)))
                out = os.path.join(tmpdir, "t%d.mp4" % idx)
                cmd = [
                    self.ffmpeg_path, "-y",
                    "-ss", "{:.3f}".format(start), "-t", "{:.3f}".format(window),
                    "-i", job["path"],
                    "-an", "-sn", "-dn",
                    "-c:v", "libx265", "-preset", "medium", "-crf", str(job["crf"]),
                    "-x265-params", "log-level=error",
                    "-pix_fmt", "yuv420p", "-tag:v", "hvc1",
                ]
                if job.get("passthrough") and supports_fps_mode(self.ffmpeg_path):
                    # 保留源时间戳，不补帧也不丢帧
                    cmd.extend(["-fps_mode", "passthrough"])
                cmd.append(out)

                self.log_signal.emit(
                    "🧪 试压片段 {}/{}：{:.2f}s 起，长 {:.1f}s".format(
                        idx + 1, len(SMART_TRIAL_POINTS), start, window
                    )
                )
                self.log_signal.emit("   💻 " + subprocess.list2cmdline(cmd))

                code, detail = self._run(cmd)
                if code != 0 or not os.path.exists(out):
                    self.log_signal.emit(
                        "   ❌ 片段 {} 失败（返回码 {}）{}".format(
                            idx + 1, code, ("　" + detail) if detail else ""
                        )
                    )
                    continue
                secs = self._probe_duration(out) or window
                total_bytes += os.path.getsize(out)
                total_secs += secs
                ok_samples += 1
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        if ok_samples == 0 or total_secs <= 0:
            self.log_signal.emit("   ⚠️ 试压没有成功的片段，无法估算")
            return {"ok": False, "error": "试压失败", "saving": 0.0, "est_bytes": 0}

        video_bps = total_bytes * 8.0 / total_secs
        est_bytes = video_bps / 8.0 * duration + (job.get("audio_bitrate") or 0) / 8.0 * duration
        source_size = job.get("size") or 0
        saving = (1.0 - est_bytes / source_size) if source_size > 0 else 0.0

        change = ("省 {:.0%}".format(saving) if saving >= 0
                  else "反而增大 {:.0%}".format(-saving))
        self.log_signal.emit(
            "   📊 试压估算：{:.2f} MB（源 {:.2f} MB，{}）".format(
                est_bytes / 1e6, source_size / 1e6, change
            )
        )
        return {
            "ok": True,
            "samples": ok_samples,
            "est_bytes": int(est_bytes),
            "saving": saving,
            "error": "",
        }

    def _run(self, cmd):
        """执行一次片段试压，返回 (返回码, 出错时的最后一行输出)

        输出用后台线程读取：主循环要留着轮询"停止"，不能阻塞在 readline 上。
        """
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="ignore", startupinfo=startupinfo,
            )
        except Exception as e:
            self._proc = None
            return -1, str(e)

        proc = self._proc
        stats = {"line": "", "err": ""}

        def reader():
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                if is_progress_line(line):
                    stats["line"] = normalize_progress_line(line)
                elif "error" in line.lower():
                    stats["err"] = line

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            while True:
                code = proc.poll()
                if code is not None:
                    break
                if self.is_stopped:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    break
                time.sleep(0.05)
            thread.join(2.0)
            if self.is_stopped:
                return -1, "已停止"
            if stats["line"]:
                self.log_signal.emit("   " + stats["line"])
            return proc.returncode, stats["err"]
        finally:
            self._proc = None

    def _probe_duration(self, path):
        if not self.ffprobe_path:
            return 0.0
        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            out = subprocess.run(
                [self.ffprobe_path, "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", path],
                capture_output=True, text=True, encoding="utf-8", errors="ignore",
                timeout=15, startupinfo=startupinfo,
            ).stdout.strip()
            return float(out) if out else 0.0
        except Exception:
            return 0.0


class SmartScanDialog(QDialog):
    """bpp 检测结果：勾选要送去压缩的文件；VFR 文件可抽样试压后再决定"""

    VERDICT_TEXT = {
        "ok": "可压", "review": "边缘", "vfr": "待试压",
        "skip": "跳过", "error": "检测失败",
    }
    VERDICT_COLOR = {
        "ok": "#1D9E75", "review": "#BA7517", "vfr": "#7A5AF8",
        "skip": "#888780", "error": "#A32D2D",
    }
    ORDER = {"ok": 0, "review": 1, "vfr": 2, "skip": 3, "error": 4}

    def __init__(self, results, parent=None, ffmpeg_path=None, ffprobe_path=None,
                 crf=22, log_sink=None):
        super().__init__(parent)
        self.setWindowTitle("bpp 检测结果")
        self.resize(1000, 560)
        self.setMinimumSize(860, 460)

        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.crf = crf
        self._trial = None
        self._log = log_sink or (lambda text: None)

        self.results = sorted(
            results, key=lambda r: (self.ORDER[r["verdict"]], -r["bpp"])
        )
        self._loading = True
        self.check_boxes = {}      # 行号 -> QCheckBox

        outer = QVBoxLayout(self)
        outer.setSpacing(8)

        title = QLabel(
            "共检测 {} 个文件：可压 {} ｜ 边缘 {} ｜ 待试压 {} ｜ 跳过 {} ｜ 失败 {}".format(
                len(results), self._count("ok"), self._count("review"),
                self._count("vfr"), self._count("skip"), self._count("error"),
            )
        )
        title.setStyleSheet("font-weight: bold;")
        outer.addWidget(title)

        hint = QLabel(
            "bpp = 视频码率 ÷ (宽 × 高 × 帧率)，数值越低说明源片压得越充分。\n"
            "跳过阈值按编码器折算：H.264 < {:.3f}、HEVC < {:.3f}、AV1 < {:.3f}；"
            "边缘区为阈值至 {:.3f}（H.264）。\n"
            "VFR（可变帧率）视频的 bpp 参考性较差，会标为「待试压」；勾选后点「试压」可实测压缩比。".format(
                SMART_BPP_SKIP, SMART_BPP_SKIP * 1.10,
                SMART_BPP_SKIP * 1.15, SMART_BPP_REVIEW,
            )
        )
        hint.setStyleSheet("color: #666;")
        outer.addWidget(hint)

        self.table = QTableWidget(len(self.results), 7)
        self.table.setHorizontalHeaderLabels(
            ["选择", "文件", "编码 / 分辨率 / 帧率", "视频码率", "bpp", "判定", "说明"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.setWordWrap(True)
        # 表格只用于勾选，不需要焦点框（否则单元格会套一圈虚线）
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 52)
        self.table.setColumnWidth(1, 330)
        self.table.setColumnWidth(2, 190)
        self.table.setColumnWidth(3, 95)
        self.table.setColumnWidth(4, 70)
        self.table.setColumnWidth(5, 70)

        for row, r in enumerate(self.results):
            self._add_row(row, r)
        self._loading = False
        outer.addWidget(self.table, 1)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("显示："))
        self.combo_filter = QComboBox()
        self.combo_filter.addItems(
            ["全部", "仅建议压缩", "仅边缘", "仅待试压", "仅跳过", "仅检测失败"]
        )
        self.combo_filter.currentIndexChanged.connect(self._apply_filter)
        filter_row.addWidget(self.combo_filter)
        filter_row.addStretch()
        self.lbl_count = QLabel()
        filter_row.addWidget(self.lbl_count)
        outer.addLayout(filter_row)

        trial_row = QHBoxLayout()
        self.trial_bar = QProgressBar()
        self.trial_bar.setTextVisible(True)
        self.trial_bar.setVisible(False)
        self.lbl_trial = QLabel("")
        self.lbl_trial.setVisible(False)
        trial_row.addWidget(self.trial_bar, 1)
        trial_row.addWidget(self.lbl_trial)
        outer.addLayout(trial_row)

        btn_row = QHBoxLayout()
        self.btn_all = QPushButton("全选")
        self.btn_none = QPushButton("全不选")
        self.btn_suggest = QPushButton("仅选建议压缩")
        self.btn_trial = QPushButton("🧪 试压")
        self.btn_trial.setToolTip(
            "对已勾选的文件做抽样试压：\n"
            "按正式压缩参数各压两段 6 秒短片，估算全片体积。\n"
            "VFR 文件默认不勾选，勾上需要验证的条目再点这里。"
        )
        self.btn_cancel = QPushButton("取消")
        self.btn_add = QPushButton("一键添加")
        self.btn_add.setObjectName("primaryButton")
        btn_row.addWidget(self.btn_all)
        btn_row.addWidget(self.btn_none)
        btn_row.addWidget(self.btn_suggest)
        btn_row.addWidget(self.btn_trial)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_cancel)
        btn_row.addWidget(self.btn_add)
        outer.addLayout(btn_row)

        self.btn_all.clicked.connect(lambda: self._set_all(True))
        self.btn_none.clicked.connect(lambda: self._set_all(False))
        self.btn_suggest.clicked.connect(self._set_suggest)
        self.btn_trial.clicked.connect(self._toggle_trial)
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_add.clicked.connect(self.accept)

        self._update_count()

    def _count(self, verdict):
        return sum(1 for r in self.results if r["verdict"] == verdict)

    def _add_row(self, row, r):
        # 用真实的 QCheckBox 而非表格项自带的勾选框：表格项勾选框一旦加样式，
        # Qt 会把同行文本按单行截断（路径显示不全），改用控件可以两者兼得
        check = QCheckBox()
        check.setChecked(bool(r["ok"]) and r["verdict"] == "ok")
        check.setEnabled(bool(r["ok"]))
        check.stateChanged.connect(self._update_count)
        holder = QWidget()
        holder_layout = QHBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.setAlignment(Qt.AlignCenter)
        holder_layout.addWidget(check)
        self.table.setCellWidget(row, 0, holder)
        self.check_boxes[row] = check

        name = QTableWidgetItem("{}\n{}".format(r["name"], os.path.dirname(r["path"])))
        name.setToolTip(r["path"])
        self.table.setItem(row, 1, name)

        if r["ok"]:
            codec = r["codec"]
            tech = "{} · {}×{} · {:.2f}fps".format(
                codec, r["width"], r["height"], r["fps"]
            )
            if r.get("vfr"):
                tech += " · VFR"
            if r["verdict"] == "review":
                note = "阈值区间 {:.3f}–{:.3f}，可手动勾选".format(r["skip"], r["review"])
            elif r["verdict"] == "vfr":
                note = "VFR 帧率不稳，bpp 仅供参考"
            elif r["verdict"] == "skip":
                note = "低于 {:.3f}，压缩收益小".format(r["skip"])
            else:
                note = "高于 {:.3f}，推荐压缩".format(r["review"])
        else:
            tech = "—"
            note = r["reason"]
        self.table.setItem(row, 2, QTableWidgetItem(tech))

        rate = "{:.2f} Mbps".format(r["bitrate"] / 1e6) if r["bitrate"] else "—"
        rate_item = QTableWidgetItem(rate)
        rate_item.setToolTip("码率来源：" + (r["bitrate_source"] or "未知"))
        self.table.setItem(row, 3, rate_item)

        bpp_item = QTableWidgetItem("{:.4f}".format(r["bpp"]) if r["ok"] else "—")
        bpp_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.table.setItem(row, 4, bpp_item)

        verdict = QTableWidgetItem(self.VERDICT_TEXT[r["verdict"]])
        verdict.setForeground(QColor(self.VERDICT_COLOR[r["verdict"]]))
        verdict.setToolTip(self._tip_for(r, note))
        self.table.setItem(row, 5, verdict)

        note_item = QTableWidgetItem(note)
        note_item.setToolTip(note)
        if not r["ok"]:
            note_item.setForeground(QColor(self.VERDICT_COLOR["error"]))
        self.table.setItem(row, 6, note_item)

        self.table.setRowHeight(row, 44)

    @staticmethod
    def _tip_for(r, note):
        """判定列/说明列的悬停提示：补上列里放不下的细节"""
        if not r.get("ok"):
            return note
        if r.get("vfr"):
            return ("{}（名义 {:.2f}fps / 平均 {:.2f}fps，压缩时保留原始时间戳，不改帧率）"
                    "\n勾选后可点「试压」实测压缩比").format(
                note, r["r_fps"], r["avg_fps"]
            )
        return note

    def _apply_filter(self):
        mode = self.combo_filter.currentIndex()
        wanted = {0: None, 1: "ok", 2: "review", 3: "vfr", 4: "skip", 5: "error"}[mode]
        for row, r in enumerate(self.results):
            self.table.setRowHidden(row, wanted is not None and r["verdict"] != wanted)

    def _set_all(self, checked):
        for row, r in enumerate(self.results):
            if not r["ok"]:
                continue
            self.check_boxes[row].setChecked(checked)
        self._update_count()

    def _set_suggest(self):
        for row, r in enumerate(self.results):
            if not r["ok"]:
                continue
            self.check_boxes[row].setChecked(r["verdict"] == "ok")
        self._update_count()

    def selected_results(self):
        chosen = []
        for row, r in enumerate(self.results):
            if r["ok"] and self.check_boxes[row].isChecked():
                chosen.append(r)
        return chosen

    def selected_files(self):
        return [r["path"] for r in self.selected_results()]

    def _update_count(self, *_):
        n = len(self.selected_results())
        self.lbl_count.setText("已选 {} 个".format(n))
        self.btn_add.setEnabled(n > 0 and not self._trial_running())
        self._update_trial_button()

    # ---------- 抽样试压 ----------
    def _trial_running(self):
        return bool(self._trial and self._trial.isRunning())

    def _trial_targets(self):
        """试压对象 = 用户勾选的文件，未勾选的一律不动"""
        rows = []
        for row, r in enumerate(self.results):
            if r["ok"] and not r.get("trial") and self.check_boxes[row].isChecked():
                rows.append(row)
        return rows

    def _update_trial_button(self):
        if self._trial_running():
            return
        n = len(self._trial_targets())
        self.btn_trial.setEnabled(n > 0)
        self.btn_trial.setText("🧪 试压（{}）".format(n) if n else "🧪 试压")

    def _toggle_trial(self):
        if self._trial_running():
            self._trial.stop_process()
            self.btn_trial.setEnabled(False)
            self.lbl_trial.setText("正在停止…")
            return
        self._start_trial()

    def _start_trial(self):
        rows = self._trial_targets()
        if not rows:
            return
        jobs = []
        for row in rows:
            r = self.results[row]
            jobs.append({
                "row": row,
                "path": r["path"],
                "name": r["name"],
                "crf": self.crf,
                # 正式压缩对 VFR 文件保留原始时间戳，试压必须用同样的参数
                "passthrough": bool(r.get("vfr")),
                "duration": r["duration"],
                "size": r["size"],
                "audio_bitrate": r["audio_bitrate"],
            })

        self._set_trial_running(True, len(jobs))
        self._log(
            "\n🧪 开始试压 {} 个文件（每个 2 段 × {:.0f} 秒，CRF {}）".format(
                len(jobs), SMART_TRIAL_SECONDS, self.crf
            )
        )
        self._trial = SmartTrialWorker(self.ffmpeg_path, self.ffprobe_path, jobs)
        self._trial.progress_signal.connect(self._on_trial_progress)
        self._trial.file_signal.connect(self._on_trial_file)
        self._trial.finished_signal.connect(self._on_trial_finished)
        self._trial.log_signal.connect(self._on_trial_log)   # 经对话框转发到主日志
        self._trial.start()

    def _on_trial_log(self, text):
        self._log(text)

    def _set_trial_running(self, running, total=0):
        for w in (self.btn_all, self.btn_none, self.btn_suggest,
                  self.btn_cancel, self.btn_add):
            w.setEnabled(not running)
        self.combo_filter.setEnabled(not running)
        # 试压期间锁住勾选框，避免跑到一半改了对象导致数字对不上
        for row, cb in self.check_boxes.items():
            cb.setEnabled(self.results[row]["ok"] and not running)
        self.trial_bar.setVisible(running)
        self.lbl_trial.setVisible(running)
        if running:
            self.trial_bar.setRange(0, max(total, 1))
            self.trial_bar.setValue(0)
            self.btn_trial.setEnabled(True)
            self.btn_trial.setText("⏹ 停止试压")
            self.lbl_trial.setText("准备中…")
        else:
            self.btn_trial.setText("🧪 试压")
            self.lbl_trial.setText("")

    def _on_trial_progress(self, done, total, name):
        self.trial_bar.setRange(0, max(total, 1))
        self.trial_bar.setValue(done)
        self.lbl_trial.setText("试压 {}/{}：{}".format(min(done + 1, total), total, name))

    def _on_trial_file(self, row, data):
        r = self.results[row]
        r["trial"] = data
        note_item = self.table.item(row, 6)
        verdict_item = self.table.item(row, 5)
        if not data.get("ok"):
            text = "试压失败：{}".format(data.get("error") or "未知原因")
            note_item.setText(text)
            note_item.setToolTip(text)
            return

        saving = data["saving"]
        est_mb = data["est_bytes"] / 1e6
        if saving >= SMART_TRIAL_SAVING:
            r["verdict"] = "ok"
            text = "试压：预计 {:.2f} MB（省 {:.0%}）".format(est_mb, saving)
            self.check_boxes[row].setChecked(True)
        else:
            r["verdict"] = "skip"
            if saving >= 0:
                text = "试压：预计 {:.2f} MB（省 {:.0%}），收益不足".format(est_mb, saving)
            else:
                text = "试压：预计 {:.2f} MB，比源文件还大，建议跳过".format(est_mb)
            self.check_boxes[row].setChecked(False)

        verdict_item.setText(self.VERDICT_TEXT[r["verdict"]])
        verdict_item.setForeground(QColor(self.VERDICT_COLOR[r["verdict"]]))
        verdict_item.setToolTip(text)
        note_item.setText(text)
        note_item.setToolTip(text)
        self._update_count()

    def _on_trial_finished(self, cancelled):
        self._set_trial_running(False)
        if cancelled:
            self.lbl_trial.setText("试压已停止")
            self._log("⏹ 试压已停止")
        else:
            self._log("🧪 试压结束，命令行与过程输出见上方日志。")
        self._update_count()

    def _stop_trial_worker(self):
        if self._trial_running():
            self._trial.stop_process()
            self._trial.wait(5000)

    def reject(self):
        self._stop_trial_worker()
        super().reject()

    def closeEvent(self, event):
        self._stop_trial_worker()
        super().closeEvent(event)


# ==========================================
# 主界面
# ==========================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FFmpeg GUI 批量处理工具")
        self.resize(900, 850)

        # 检查 FFmpeg 环境
        self.ffmpeg_path = shutil.which("ffmpeg")
        self.ffprobe_path = shutil.which("ffprobe")
        self.current_duration = None  # 当前选中文件的时长（秒）
        self._duration_token = 0
        self._track_plan = None       # 轨道管理：当前文件的保留方案
        self._closing = False         # 关闭流程中，避免收尾弹窗打断退出
        self._smart_scanned = False   # 智能压缩：当前列表是否已完成 bpp 筛选
        self._smart_vfr = set()       # 这批文件里哪些是 VFR（压缩时保留原始时间戳）
        self.scan_worker = None
        self.log_writer = LogFileWriter()

        self.init_ui()
        self.log_text.append("=" * 28 + " 启动 FFmpeg GUI " + "=" * 28)
        self.check_env()
        # 环境日志之后再检测硬件，保证启动日志顺序清晰
        self.detect_hardware_accel()

    def init_ui(self):
        # 主部件和布局
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        # --- 1. 文件选择区（列表在左，按钮竖排在右） ---
        file_group = QGroupBox("1. 选择媒体文件（支持批量）")
        file_layout = QHBoxLayout()
        file_layout.setContentsMargins(10, 15, 10, 10)

        self.file_list = DropListWidget()
        self.file_list.setSelectionMode(QListWidget.ExtendedSelection)

        # 右侧按钮竖排
        BUTTON_HEIGHT = 32
        BUTTON_GAP = 8
        file_btn_layout = QVBoxLayout()
        file_btn_layout.setSpacing(BUTTON_GAP)
        file_btn_layout.setContentsMargins(0, 0, 0, 0)

        self.btn_add_files = QPushButton("添加文件")
        self.btn_add_folder = QPushButton("添加文件夹")
        self.btn_remove_selected = QPushButton("移除选中")
        self.btn_clear_files = QPushButton("清空列表")
        self.btn_media_info = QPushButton("📽️ 查看媒体信息")
        self.btn_scan_overhead = QPushButton("🔍 扫描冗余度")

        for btn in [self.btn_add_files, self.btn_add_folder,
                    self.btn_remove_selected, self.btn_clear_files,
                    self.btn_media_info, self.btn_scan_overhead]:
            btn.setFixedHeight(BUTTON_HEIGHT)
            file_btn_layout.addWidget(btn)

        # 核心：计算 6 个按钮的总高度
        list_height = BUTTON_HEIGHT * 6 + BUTTON_GAP * 5
        
        # 核心：将列表框的高度严格固定为和按钮组一样高
        self.file_list.setFixedHeight(list_height)
        
        # 将列表和按钮组加入水平布局
        file_layout.addWidget(self.file_list)
        file_layout.addLayout(file_btn_layout)
        
        # 关键修复：移除 addLayout 的 alignment 参数，直接设置布局本身的对齐
        # 让内部的控件保持顶部对齐
        file_layout.setAlignment(Qt.AlignTop)
        
        file_group.setLayout(file_layout)

        # 关键：强制 QGroupBox 的高度只适应内容，不随窗口纵向拉伸
        file_group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        # --- 2. 操作模式与参数区（全部水平排一行） ---
        param_group = QGroupBox("2. 操作模式与参数")
        param_layout = QHBoxLayout()
        param_layout.setSpacing(10)

        PARAM_H = 32   # 参数区所有控件统一高度

        param_layout.addWidget(QLabel("处理模式:"))
        self.combo_mode = QComboBox()
        self.combo_mode.addItems(
            [
                "无编码截取视频 (极速)",
                "无编码合并视频 (极速)",
                "将字幕封装进视频 (无编码)",
                "提取视频中的音频 (无编码)",
                "修复视频索引 (无编码 Remux)",
                "轨道管理 (删除音轨/字幕轨)",
                "极简压缩 (H.265, CRF23)",
                "智能压缩 (bpp 预筛)",
                "视频转码为 MP4 (H.264)",
            ]
        )
        self.combo_mode.setFixedHeight(PARAM_H)
        param_layout.addWidget(self.combo_mode)

        # 轨道管理按钮：仅在该模式下出现，且需要列表里只选中一个文件
        self.btn_track = QPushButton("轨道管理…")
        self.btn_track.setFixedHeight(PARAM_H)
        self.btn_track.setVisible(False)
        self.btn_track.setEnabled(False)
        param_layout.addWidget(self.btn_track)

        # 截取参数
        self.check_start = QCheckBox("从")
        self.check_start.setFixedHeight(PARAM_H)
        self.edit_start = QLineEdit()
        self.edit_start.setInputMask("00:00:00;_")
        self.edit_start.setText("00:00:00")
        self.edit_start.setFixedWidth(110)
        self.edit_start.setFixedHeight(PARAM_H)
        self.check_end = QCheckBox("到")
        self.check_end.setFixedHeight(PARAM_H)
        self.edit_end = QLineEdit()
        self.edit_end.setInputMask("00:00:00;_")
        self.edit_end.setText("00:00:00")
        self.edit_end.setFixedWidth(110)
        self.edit_end.setFixedHeight(PARAM_H)
        # 智能音频控制开关（仅在极简压缩中显示）
        self.check_smart_audio = QCheckBox("智能音频控制 (源>192k则重编码)")
        self.check_smart_audio.setFixedHeight(PARAM_H)

        # --- 新增：CRF 质量输入框 ---
        self.lbl_crf = QLabel("CRF:")
        self.lbl_crf.setFixedHeight(PARAM_H)
        self.spin_crf = QSpinBox()
        self.spin_crf.setRange(18, 35)       # 常用的 CRF 范围
        self.spin_crf.setValue(23)           # 默认 23
        self.spin_crf.setFixedHeight(PARAM_H)
        self.spin_crf.setFixedWidth(60)

        # --- 新增：硬件加速复选框 ---
        self.check_nvenc = QCheckBox("Nvidia 硬编")
        self.check_nvenc.setFixedHeight(PARAM_H)
        self.check_nvenc.setEnabled(False)   # 初始禁用，检测到才启用

        self.check_qsv = QCheckBox("Intel 硬编")
        self.check_qsv.setFixedHeight(PARAM_H)
        self.check_qsv.setEnabled(False)     # 初始禁用，检测到才启用

        # 字幕参数
        self.lbl_sub = QLabel("字幕文件:")
        self.lbl_sub.setFixedHeight(PARAM_H)
        self.edit_sub = DropSubtitleEdit()
        self.edit_sub.setReadOnly(True)
        self.edit_sub.setMinimumWidth(200)
        self.edit_sub.setFixedHeight(PARAM_H)
        self.btn_select_sub = QPushButton("选择字幕...")
        self.btn_select_sub.setFixedHeight(PARAM_H)

        # 全部加入同一行
        param_layout.addWidget(self.check_start)
        param_layout.addWidget(self.edit_start)
        param_layout.addWidget(self.check_end)
        param_layout.addWidget(self.edit_end)
        param_layout.addWidget(self.lbl_sub)
        param_layout.addWidget(self.edit_sub, stretch=1)
        param_layout.addWidget(self.btn_select_sub)
        param_layout.addWidget(self.check_smart_audio)
        param_layout.addWidget(self.lbl_crf)
        param_layout.addWidget(self.spin_crf)
        param_layout.addWidget(self.check_nvenc)
        param_layout.addWidget(self.check_qsv)

        param_layout.addStretch()

        param_group.setLayout(param_layout)

        # --- 3. 输出设置区 ---
        out_group = QGroupBox("3. 输出设置")
        out_layout = QVBoxLayout()

        dir_layout = QHBoxLayout()
        self.check_same_dir = QCheckBox("输出到源文件所在目录")
        self.check_same_dir.setChecked(True)
        self.edit_out_dir = QLineEdit()
        self.edit_out_dir.setPlaceholderText("选择输出目录...")
        self.edit_out_dir.setEnabled(False)
        self.btn_select_out_dir = QPushButton("浏览...")
        self.btn_select_out_dir.setEnabled(False)

        dir_layout.addWidget(self.check_same_dir)
        dir_layout.addWidget(self.edit_out_dir, stretch=1)
        dir_layout.addWidget(self.btn_select_out_dir)
        out_layout.addLayout(dir_layout)

        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel("输出文件名后缀:"))
        self.edit_suffix = QLineEdit("_processed")
        self.edit_suffix.setMaximumWidth(200)
        name_layout.addWidget(self.edit_suffix)
        name_layout.addStretch()
        out_layout.addLayout(name_layout)

        out_group.setLayout(out_layout)

        # --- 4. 执行与日志区（两个按钮同行各半） ---
        run_group = QGroupBox("4. 执行与日志")
        run_layout = QVBoxLayout()

        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)

        self.btn_run = QPushButton("开始批量处理")
        self.btn_run.setObjectName("primaryButton")
        self.btn_run.setFixedHeight(40)

        # --- 新增：暂停和停止按钮 ---
        self.btn_pause = QPushButton("⏸ 暂停")
        self.btn_pause.setFixedHeight(40)
        self.btn_pause.setEnabled(False)  # 初始禁用

        self.btn_stop = QPushButton("⏹ 停止")
        self.btn_stop.setFixedHeight(40)
        self.btn_stop.setObjectName("dangerButton")   # 红色警示
        self.btn_stop.setEnabled(False)
        # ---------------------------

        self.btn_open_out = QPushButton("📂 打开输出文件夹")
        self.btn_open_out.setFixedHeight(40)

        # 四个按钮同一行
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.btn_run, stretch=2)
        btn_row.addWidget(self.btn_pause, stretch=1)
        btn_row.addWidget(self.btn_stop, stretch=1)
        btn_row.addWidget(self.btn_open_out, stretch=2)

        self.log_text = LogView(self.log_writer)
        self.log_text.setReadOnly(True)
        self.log_text.setPlaceholderText("执行日志将显示在这里...")
        self.log_text.setMinimumHeight(224)

        # 任务完成后自动关机：空闲时也能勾选（可提前勾好再开始），本轮结束即自动取消
        self.check_auto_shutdown = QCheckBox("任务完成后自动关机（60 秒倒计时，可取消）")
        self.check_auto_shutdown.setToolTip(
            "勾选后，这一轮任务跑完会执行 shutdown /s /t 60：\n"
            "· Windows 会先弹出系统提示，60 秒后才真正关机\n"
            "· 期间可在收尾弹窗点「取消关机」，或命令行执行 shutdown /a 撤销\n"
            "· 手动停止、关闭程序、或一个任务都没跑时不会关机\n"
            "· 任务若一直暂停着，不会触发（恢复并跑完才会计时）\n"
            "· 只对当前这一轮有效，跑完自动取消勾选"
        )

        run_layout.addWidget(self.progress_bar)
        run_layout.addLayout(btn_row)
        run_layout.addWidget(self.log_text)
        run_layout.addWidget(self.check_auto_shutdown)
        run_group.setLayout(run_layout)

        # 添加到主布局
        main_layout.addWidget(file_group, stretch=0)
        main_layout.addWidget(param_group, stretch=0)
        main_layout.addWidget(out_group, stretch=0)
        main_layout.addWidget(run_group, stretch=1)

        # --- 信号连接 ---
        self.btn_add_files.clicked.connect(self.add_files)
        self.btn_remove_selected.clicked.connect(self.remove_selected)
        self.btn_clear_files.clicked.connect(self.clear_files)
        self.btn_media_info.clicked.connect(self.show_media_info)
        self.combo_mode.currentIndexChanged.connect(self.update_mode_params)
        self.btn_select_sub.clicked.connect(self.select_subtitle)
        self.check_same_dir.stateChanged.connect(self.toggle_out_dir)
        self.btn_select_out_dir.clicked.connect(self.select_out_dir)
        self.btn_run.clicked.connect(self.start_processing)
        self.btn_open_out.clicked.connect(self.open_output_folder)
        self.file_list.itemSelectionChanged.connect(self.on_file_selection_changed)
        self.btn_add_folder.clicked.connect(self.add_folder)
        self.file_list.files_dropped.connect(self._on_files_dropped)
        self.edit_sub.file_dropped.connect(self._on_subtitle_dropped)
        self.btn_scan_overhead.clicked.connect(self.scan_overhead)
        self.btn_pause.clicked.connect(self.toggle_pause)
        self.btn_stop.clicked.connect(self.stop_processing)
        self.btn_track.clicked.connect(self.open_track_dialog)
        # 硬件加速互斥逻辑
        self.check_nvenc.stateChanged.connect(self.on_nvenc_toggled)
        self.check_qsv.stateChanged.connect(self.on_qsv_toggled)

        # 初始更新参数显示
        self.update_mode_params(0)

    def check_env(self):
        """检查 FFmpeg 环境"""
        if not self.ffmpeg_path:
            self.log_text.append("❌ 未检测到 ffmpeg：系统 PATH 中找不到 ffmpeg.exe")
            self.log_text.append("   请安装 FFmpeg，并把 bin 目录加入系统环境变量 PATH，然后重启本程序。")
            self.log_text.append("   验证方法：命令行执行 ffmpeg -version\n")
        else:
            self.log_text.append(f"✅ 已检测到 FFmpeg: {self.ffmpeg_path}")
            if self.ffprobe_path:
                self.log_text.append(f"✅ 已检测到 ffprobe: {self.ffprobe_path}")
            else:
                self.log_text.append(
                    "⚠️ 未检测到 ffprobe：媒体信息、时长读取、冗余度扫描将不可用。"
                )
            self.log_text.append(
                "提示：无编码模式（截取、封装、提取）速度极快；转码模式较慢。\n"
            )
            self.log_text.append(
                "提示：可以直接把媒体文件/文件夹拖入列表，把字幕文件拖入字幕框。\n"
            )
            # -fps_mode 支持情况在后台查好，避免点「开始批量处理」时现起子进程卡界面
            threading.Thread(
                target=warm_up_fps_mode, args=(self.ffmpeg_path,), daemon=True
            ).start()

    def on_nvenc_toggled(self, state):
        """勾选 N卡 时，取消勾选 Intel"""
        if state == Qt.Checked:
            self.check_qsv.setChecked(False)

    def on_qsv_toggled(self, state):
        """勾选 Intel 时，取消勾选 N卡"""
        if state == Qt.Checked:
            self.check_nvenc.setChecked(False)

    def detect_hardware_accel(self):
        """启动时检测硬件加速支持情况"""
        if not self.ffmpeg_path:
            return

        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE

            # 一次调用同时拿到版本号和编码器列表（版本信息在 banner 里，走 stderr）
            cmd = [self.ffmpeg_path, "-encoders"]
            result = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='ignore', check=True,
                startupinfo=startupinfo
            )
            encoders = result.stdout

            # 顺手把 -fps_mode 支持情况记进缓存，后面生成任务时不必再起进程
            m = re.search(r"ffmpeg version\s+(?:n)?(\d+)", encoders)
            if m:
                _FPS_MODE_SUPPORT[self.ffmpeg_path] = int(m.group(1)) >= 5

            has_nvenc = "hevc_nvenc" in encoders
            has_qsv = "hevc_qsv" in encoders

            self.check_nvenc.setEnabled(has_nvenc)
            self.check_qsv.setEnabled(has_qsv)

            msg = "🔍 硬件加速检测: "
            if has_nvenc:
                msg += "[Nvidia NVENC 支持] "
            if has_qsv:
                msg += "[Intel QSV 支持] "
            
            if not has_nvenc and not has_qsv:
                msg += "未检测到支持的硬编硬件，将默认使用 CPU 软编。"
            else:
                self.log_text.append(msg)
                
        except Exception as e:
            self.log_text.append(f"⚠️ 硬件加速检测失败: {str(e)}")

    def update_mode_params(self, index):
        mode = self.combo_mode.currentText()
        # 切换模式后，之前的 bpp 筛选结果不再适用
        self._reset_smart_state()

        # 隐藏所有动态参数 (加入新增的控件)
        for w in [
            self.check_start, self.edit_start, self.check_end, self.edit_end,
            self.lbl_sub, self.edit_sub, self.btn_select_sub, 
            self.check_smart_audio, self.lbl_crf, self.spin_crf,
            self.check_nvenc, self.check_qsv, self.btn_track
        ]:
            w.hide()

        if "无编码截取" in mode:
            self.check_start.show()
            self.edit_start.show()
            self.check_end.show()
            self.edit_end.show()
        elif "字幕封装" in mode:
            self.lbl_sub.show()
            self.edit_sub.show()
            self.btn_select_sub.show()
        elif "轨道" in mode:
            self._update_track_button_state()
        elif "极简压缩" in mode:
            self.check_smart_audio.show()
            self.lbl_crf.show()
            self.spin_crf.show()
            self.check_nvenc.show()
            self.check_qsv.show()
        elif "智能压缩" in mode:
            # 智能压缩固定 CPU 软编，不显示显卡选项；CRF 默认 22
            self.check_smart_audio.show()
            self.lbl_crf.show()
            self.spin_crf.show()
            if self.spin_crf.value() == 23:
                self.spin_crf.setValue(22)
        elif "视频转码" in mode:
            self.lbl_crf.show()
            self.spin_crf.show()
            self.check_nvenc.show()
            self.check_qsv.show()

    def _update_track_button_state(self):
        """轨道管理按钮：只在该模式下显示，且列表里只选中一个文件时才可点击"""
        if "轨道" not in self.combo_mode.currentText():
            self.btn_track.setVisible(False)
            return

        self.btn_track.setVisible(True)
        selected = self.file_list.selectedItems()
        has_one = len(selected) == 1
        self.btn_track.setEnabled(has_one)

        if not has_one:
            self.btn_track.setText("轨道管理…")
            self.btn_track.setToolTip("请先在列表里只选中一个文件")
            return

        plan = self._track_plan
        if plan and os.path.normpath(plan["path"]) == os.path.normpath(selected[0].text()):
            self.btn_track.setText("轨道管理…（已配置）")
            self.btn_track.setToolTip("已配置保留方案，改完记得点「开始批量处理」")
        else:
            self.btn_track.setText("轨道管理…")
            self.btn_track.setToolTip("选择要保留的音轨 / 字幕轨")

    def open_track_dialog(self):
        selected = self.file_list.selectedItems()
        if len(selected) != 1:
            QMessageBox.information(self, "提示", "请先在列表里只选中一个文件。")
            return

        file_path = selected[0].text()
        dlg = TrackDialog(self, file_path, self.ffprobe_path)
        if dlg.exec_() == QDialog.Accepted and dlg.plan:
            self._track_plan = dlg.plan
            self.log_text.append(
                f"🎛 轨道管理：{os.path.basename(file_path)} 已配置保留 "
                f"{len(dlg.plan['keep'])} 条轨道，点击「开始批量处理」执行无编码重封装。"
            )
            self._update_track_button_state()

    def add_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择媒体文件", "",
            f"{build_ext_filter('媒体文件', MEDIA_EXTS)};;所有文件 (*)",
        )
        if files:
            self._add_files_to_list(files)

    def _on_files_dropped(self, files):
        """处理从外部拖入到列表的媒体文件"""
        added = self._add_files_to_list(files)
        if added > 0:
            self.log_text.append(f"📥 拖入添加了 {added} 个文件")
        else:
            self.log_text.append("📥 拖入的文件已在列表中")

    def _on_subtitle_dropped(self, path):
        """处理从外部拖入的字幕文件"""
        self.log_text.append(f"📥 已选择字幕: {os.path.basename(path)}")

    def add_folder(self):
        """选择文件夹，递归扫描其中所有媒体文件并加入列表"""
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if not folder:
            return

        # 直接复用模块级 MEDIA_EXTS：字幕文件不属于媒体，不进列表
        found = []
        for root, dirs, names in os.walk(folder):
            dirs.sort()
            for name in sorted(names):
                if os.path.splitext(name)[1].lower() in MEDIA_EXTS:
                    found.append(os.path.join(root, name))

        if not found:
            QMessageBox.information(self, "提示", "所选文件夹中没有找到媒体文件")
            return

        added = self._add_files_to_list(found)
        self.log_text.append(f"📂 从文件夹「{os.path.basename(folder)}」中添加了 {added} 个文件")

    def _add_files_to_list(self, files):
        """把文件去重后加入 file_list，返回实际新增数量"""
        existing = set(
            self.file_list.item(i).text() for i in range(self.file_list.count())
        )
        added = 0
        for f in files:
            f = os.path.normpath(f)
            if f not in existing:
                self.file_list.addItem(f)
                existing.add(f)
                added += 1

        if added > 0:
            self._reset_smart_state()
            if not self.file_list.selectedItems():
                self.file_list.setCurrentRow(0)

        return added

    def get_duration(self, file_path):
        """用 ffprobe 获取媒体时长（秒）"""
        if not self.ffprobe_path:
            return None
        try:
            cmd = [
                self.ffprobe_path,
                "-v",
                "quiet",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                file_path,
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                check=True,
            )
            return float(result.stdout.strip())
        except Exception:
            return None

    def show_media_info(self):
        """用 ffprobe 展示媒体信息（后台线程，避免卡 UI）"""
        # 优先处理选中项；若无选中，只查最后一个文件（避免扫描整个列表）
        selected = self.file_list.selectedItems()
        if selected:
            files = [item.text() for item in selected]
        elif self.file_list.count() > 0:
            files = [self.file_list.item(self.file_list.count() - 1).text()]
        else:
            QMessageBox.warning(self, "提示", "请先添加媒体文件")
            return

        # 防止重复点击
        if hasattr(self, "_media_info_worker") and self._media_info_worker is not None:
            if self._media_info_worker.isRunning():
                QMessageBox.information(self, "提示", "媒体信息查询正在进行中，请稍候...")
                return

        self.btn_media_info.setEnabled(False)
        self.log_text.append("\n⏳ 正在读取媒体信息，请稍候...")

        self._media_info_worker = MediaInfoWorker(self.ffprobe_path, files)
        self._media_info_worker.info_signal.connect(self.append_log)
        self._media_info_worker.finished_signal.connect(self._on_media_info_finished)
        self._media_info_worker.start()

    def _on_media_info_finished(self):
        self.btn_media_info.setEnabled(True)

    def format_time(self, seconds):
        """将秒数格式化为 HH:MM:SS"""
        seconds = int(seconds)
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    def on_file_selection_changed(self):
        self._update_track_button_state()

        selected = self.file_list.selectedItems()
        if len(selected) != 1:
            return

        file_path = selected[0].text()

        # 递增 token，只有最新的 worker 结果才被接受
        self._duration_token = getattr(self, "_duration_token", 0) + 1
        token = self._duration_token

        self._duration_worker = DurationWorker(self.ffprobe_path, file_path)
        self._duration_worker.result_signal.connect(
            lambda fp, dur, size, t=token: self._on_duration_ready(fp, dur, size, t)
        )
        self._duration_worker.start()

    def _on_duration_ready(self, file_path, duration, size_mb, token):
        # 过期结果直接丢弃
        if token != self._duration_token:
            return
        self.current_duration = duration
        duration_str = self.format_time(duration)
        self.log_text.append(
            f"📽️ 已读取时长: {duration_str}（{duration:.2f} 秒），"
            f"文件大小: {size_mb:.2f} MB"
        )

    def remove_selected(self):
        for item in self.file_list.selectedItems():
            self.file_list.takeItem(self.file_list.row(item))
        self._reset_smart_state()
        self._update_track_button_state()

    def clear_files(self):
        self.file_list.clear()
        self._reset_smart_state()
        self._update_track_button_state()

    def select_subtitle(self):
        file, _ = QFileDialog.getOpenFileName(
            self, "选择字幕文件", "", "字幕文件 (*.srt *.ass *.vtt);;所有文件 (*)"
        )
        if file:
            self.edit_sub.setText(file)

    def toggle_out_dir(self, state):
        """切换输出目录方式"""
        is_same = state == Qt.Checked
        self.edit_out_dir.setEnabled(not is_same)
        self.btn_select_out_dir.setEnabled(not is_same)

    def select_out_dir(self):
        dir_path = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if dir_path:
            self.edit_out_dir.setText(dir_path)

    def _build_track_tasks(self, suffix):
        """轨道管理：只为当前选中的单个文件生成一条无编码重封装命令"""
        selected = self.file_list.selectedItems()
        if len(selected) != 1:
            QMessageBox.warning(self, "提示", "轨道管理只处理单个文件，请先在列表里只选中一个文件。")
            return []

        input_file = selected[0].text()
        plan = self._track_plan
        if not plan:
            QMessageBox.warning(self, "提示", "请先点击「轨道管理…」选择要保留的轨道。")
            return []

        if os.path.normpath(plan["path"]) != os.path.normpath(input_file):
            QMessageBox.warning(
                self, "提示", "轨道配置与当前选中的文件不一致，请重新点击「轨道管理…」。"
            )
            return []

        # 配置后源文件被替换/改动时，流索引可能已经变了，必须重新配置
        try:
            if (os.path.getsize(input_file) != plan["size"]
                    or os.path.getmtime(input_file) != plan["mtime"]):
                QMessageBox.warning(
                    self, "文件已变化", "源文件在配置轨道后被修改，请重新点击「轨道管理…」。"
                )
                return []
        except OSError:
            QMessageBox.warning(self, "提示", "源文件无法访问，请检查文件是否还在。")
            return []

        base_name, ext = os.path.splitext(os.path.basename(input_file))

        if self.check_same_dir.isChecked():
            out_dir = os.path.dirname(input_file)
        else:
            out_dir = self.edit_out_dir.text().strip()
            if not out_dir:
                QMessageBox.warning(self, "错误", "请选择输出目录！")
                return []

        output_file = os.path.normpath(os.path.join(out_dir, f"{base_name}{suffix}{ext}"))

        cmd = [self.ffmpeg_path, "-y", "-i", input_file]
        for idx in plan["keep"]:
            cmd.extend(["-map", f"0:{idx}"])
        # MKV 的字体等附件（ASS 字幕依赖）跟着一起保留
        if ext.lower() == ".mkv":
            cmd.extend(["-map", "0:t?"])
        cmd.extend(["-c", "copy", "-map_chapters", "0", "-map_metadata", "0"])
        if plan.get("fix_default_audio"):
            cmd.extend(["-disposition:a:0", "default"])
        if plan.get("fix_default_sub"):
            cmd.extend(["-disposition:s:0", "default"])
        if ext.lower() in (".mp4", ".m4v", ".mov"):
            cmd.extend(["-movflags", "+faststart"])
        cmd.append(output_file)

        return [(cmd, output_file)]

    def _reset_smart_state(self):
        """列表或处理模式变化后，bpp 筛选结果与 VFR 名单都作废"""
        self._smart_scanned = False
        self._smart_vfr = set()

    def _build_smart_tasks(self, suffix, crf_val):
        """智能压缩：按极简压缩的 CPU 参数生成任务（列表已由 bpp 筛选）"""
        if not self._smart_scanned:
            QMessageBox.warning(
                self, "提示", "请先点击「开始批量处理」完成 bpp 检测与筛选。"
            )
            return []
        if self.file_list.count() == 0:
            QMessageBox.warning(self, "提示", "列表中没有文件，请先检测并选择要压缩的项目。")
            return []

        tasks = []
        for i in range(self.file_list.count()):
            input_file = self.file_list.item(i).text()
            base_name, _ = os.path.splitext(os.path.basename(input_file))
            out_dir = self._output_dir_for(input_file)
            if not out_dir:
                QMessageBox.warning(self, "错误", "请选择输出目录！")
                return []

            output_file = os.path.normpath(
                os.path.join(out_dir, f"{base_name}{suffix}.mp4")
            )
            cmd = [
                self.ffmpeg_path, "-y", "-i", input_file,
                "-c:v", "libx265", "-preset", "medium", "-crf", crf_val,
                "-x265-params", "log-level=error",
                "-pix_fmt", "yuv420p", "-tag:v", "hvc1",
            ]
            # VFR 源：保留原始时间戳，不补帧也不丢帧（只改编码，不动时间轴）
            # 只读启动时缓存的结果，这里绝不能现起子进程，否则界面会卡住
            if (os.path.normpath(input_file) in self._smart_vfr
                    and _FPS_MODE_SUPPORT.get(self.ffmpeg_path) is True):
                cmd.extend(["-fps_mode", "passthrough"])
            if self.check_smart_audio.isChecked():
                cmd.append("__SMART_AUDIO__")
            else:
                cmd.extend(["-c:a", "copy"])
            cmd.extend(["-movflags", "+faststart", output_file])
            tasks.append((cmd, output_file))

        return tasks

    def generate_tasks(self):
        """生成所有执行任务"""
        tasks = []
        mode = self.combo_mode.currentText()
        suffix = self.edit_suffix.text().strip()

        # 提前获取用户设置的 CRF 值
        crf_val = str(self.spin_crf.value())

        # ============ 特例：轨道管理（单文件，删除指定音轨/字幕轨） ============
        if "轨道" in mode:
            return self._build_track_tasks(suffix)

        # ============ 智能压缩：列表已由 bpp 筛选，CPU H.265 ============
        if "智能压缩" in mode:
            return self._build_smart_tasks(suffix, crf_val)

        # ============ 特例：无编码合并视频 ============
        if "无编码合并" in mode:
            if self.file_list.count() < 2:
                QMessageBox.warning(self, "错误", "合并视频至少需要两个文件！")
                return []

            first_file = self.file_list.item(0).text()
            base_name, ext = os.path.splitext(os.path.basename(first_file))

            if self.check_same_dir.isChecked():
                out_dir = os.path.dirname(first_file)
            else:
                out_dir = self.edit_out_dir.text().strip()
                if not out_dir:
                    QMessageBox.warning(self, "错误", "请选择输出目录！")
                    return []

            out_name = f"{base_name}{suffix}{ext}"
            output_file = os.path.normpath(os.path.join(out_dir, out_name))

            list_file = os.path.join(tempfile.gettempdir(), "ffmpeg_concat_list.txt")
            with open(list_file, "w", encoding="utf-8") as f:
                for i in range(self.file_list.count()):
                    file_path = self.file_list.item(i).text()
                    escaped = file_path.replace("'", "'\\''")
                    f.write(f"file '{escaped}'\n")

            cmd = [
                self.ffmpeg_path, "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", list_file,
                "-c", "copy",
                output_file
            ]
            tasks.append((cmd, output_file))
            return tasks

        # ============ 常规模式：逐个文件处理 ============
        for i in range(self.file_list.count()):
            input_file = self.file_list.item(i).text()
            base_name, ext = os.path.splitext(os.path.basename(input_file))

            # 确定输出目录
            if self.check_same_dir.isChecked():
                out_dir = os.path.dirname(input_file)
            else:
                out_dir = self.edit_out_dir.text().strip()
                if not out_dir:
                    QMessageBox.warning(self, "错误", "请选择输出目录！")
                    return []

            # 确定输出文件名
            if "提取" in mode:
                out_name = f"{base_name}{suffix}.mka"  # 提取音频，默认使用 .mka
            elif "转码" in mode or "压缩" in mode:
                out_name = f"{base_name}{suffix}.mp4"  # 转码和压缩统一输出 mp4
            else:
                out_name = f"{base_name}{suffix}{ext}"

            output_file = os.path.normpath(os.path.join(out_dir, out_name))

            # 构建命令
            cmd = [self.ffmpeg_path, "-y"]

            if "无编码截取" in mode:
                # -i 必须在 -ss/-to 前面
                cmd = [self.ffmpeg_path, "-y", "-i", input_file]

                if self.check_start.isChecked():
                    st = self.edit_start.text().strip()
                    if st and "_" not in st:   # 排除 inputMask 未填完整的情况
                        cmd.extend(["-ss", st])

                if self.check_end.isChecked():
                    et = self.edit_end.text().strip()
                    if et and "_" not in et:
                        cmd.extend(["-to", et])

                cmd.extend(["-c", "copy", "-avoid_negative_ts", "make_zero", output_file])

            elif "字幕封装" in mode:
                sub_file = self.edit_sub.text().strip()
                if not sub_file:
                    QMessageBox.warning(self, "错误", "请选择字幕文件！")
                    return []

                ext_lower = ext.lower()
                sub_codec = SUB_CODEC_BY_EXT.get(ext_lower)

                # 容器不支持字幕：直接拦下，避免 ffmpeg 报难懂的错误
                if not sub_codec:
                    QMessageBox.warning(
                        self, "容器不支持字幕封装",
                        f"「{ext_lower}」容器无法封装文字字幕。\n\n"
                        f"文件：{os.path.basename(input_file)}\n\n"
                        "请使用 MP4 / MKV 格式的源文件；"
                        "可先用「视频转码为 MP4」统一格式后再处理。",
                    )
                    return []

                # 位图字幕只能原样复制进 MKV，转不成 MP4 的文字字幕
                sub_ext = os.path.splitext(sub_file)[1].lower()
                if sub_ext in BITMAP_SUB_EXTS and sub_codec != "copy":
                    QMessageBox.warning(
                        self, "位图字幕不支持",
                        "位图字幕（.sub / .sup / .idx）只能原样封装进 MKV，"
                        "无法转换为 MP4 的文字字幕。\n\n"
                        "请改用 MKV 源文件，或换一份文字字幕（.srt / .ass / .vtt）。",
                    )
                    return []

                # 保留源文件原有字幕轨；新字幕作为第一条字幕轨插入并设为默认
                cmd.extend([
                    "-i", input_file, "-i", sub_file,
                    "-map", "0:v:0", "-map", "0:a?",
                    "-map", "1:s:0", "-map", "0:s?",
                    "-c:v", "copy", "-c:a", "copy", "-c:s", "copy",
                ])
                if sub_codec != "copy":
                    cmd.extend(["-c:s:0", sub_codec])
                cmd.extend(["-disposition:s:0", "default"])

                if ext_lower in (".mp4", ".m4v", ".mov"):
                    cmd.extend(["-movflags", "+faststart"])
                cmd.append(output_file)

            elif "提取" in mode:
                cmd.extend(["-i", input_file, "-vn", "-c:a", "copy", output_file])

            elif "修复视频" in mode:
                cmd.extend(["-i", input_file, "-c", "copy"])
                # 只有输出是 mp4 时，faststart 才有意义（把 moov 移到文件头）
                if ext.lower() == ".mp4":
                    cmd.extend(["-movflags", "+faststart"])
                cmd.append(output_file)

            elif "极简压缩" in mode:
                cmd.extend(["-i", input_file])
                
                # 视频编码器与参数映射
                if self.check_nvenc.isChecked():
                    # N卡 H.265: 使用 cq、vbr，并且必须加 -b:v 0 解除码率上限
                    cmd.extend([
                        "-c:v", "hevc_nvenc", "-preset", "p4", 
                        "-rc", "vbr", "-cq", crf_val, "-b:v", "0", 
                        "-pix_fmt", "yuv420p", "-tag:v", "hvc1"
                    ])
                elif self.check_qsv.isChecked():
                    # Intel 核显 H.265: 使用 global_quality，同样需要 -b:v 0
                    cmd.extend([
                        "-c:v", "hevc_qsv", "-preset", "medium", 
                        "-global_quality", crf_val, "-b:v", "0", 
                        "-pix_fmt", "yuv420p", "-tag:v", "hvc1"
                    ])
                else:
                    # CPU 纯软编: 加上 x265-params 防止底层日志刷屏
                    cmd.extend([
                        "-c:v", "libx265", "-preset", "medium", "-crf", crf_val,
                        "-x265-params", "log-level=error",
                        "-pix_fmt", "yuv420p", "-tag:v", "hvc1"
                    ])
                
                # 智能音频控制
                if self.check_smart_audio.isChecked():
                    cmd.append("__SMART_AUDIO__")
                else:
                    cmd.extend(["-c:a", "copy"])
                
                # 添加 faststart 加快移动端/网页加载
                cmd.extend(["-movflags", "+faststart", output_file])

            elif "视频转码" in mode:
                cmd.extend(["-i", input_file])
                
                # 视频编码器与参数映射 (H.264)
                if self.check_nvenc.isChecked():
                    cmd.extend([
                        "-c:v", "h264_nvenc", "-preset", "p4", 
                        "-rc", "vbr", "-cq", crf_val, "-b:v", "0",
                        "-pix_fmt", "yuv420p"
                    ])
                elif self.check_qsv.isChecked():
                    cmd.extend([
                        "-c:v", "h264_qsv", "-preset", "medium", 
                        "-global_quality", crf_val, "-b:v", "0",
                        "-pix_fmt", "yuv420p"
                    ])
                else:
                    cmd.extend([
                        "-c:v", "libx264", "-preset", "medium", "-crf", crf_val,
                        "-pix_fmt", "yuv420p"
                    ])
                
                # 音频固定转码为 AAC，并添加 faststart
                cmd.extend(["-c:a", "aac", "-movflags", "+faststart", output_file])

            tasks.append((cmd, output_file))

        return tasks

    def start_processing(self):
        # 环境检查：没有 ffmpeg 直接拦住，不进入任务生成
        if not self.ffmpeg_path:
            self.log_text.append("❌ 未检测到 ffmpeg，任务已取消。")
            QMessageBox.critical(
                self, "找不到 FFmpeg",
                "未在系统 PATH 中检测到 ffmpeg.exe，无法执行任务。\n\n"
                "请先安装 FFmpeg，并把 bin 目录加入系统环境变量 PATH，"
                "然后重新启动本程序。\n\n"
                "验证方法：在命令行执行 ffmpeg -version",
            )
            return

        if self.file_list.count() == 0:
            QMessageBox.warning(self, "提示", "请先添加需要处理的文件！")
            return

        # 后缀检查：后缀留空会导致输出文件与源文件同名，-y 会直接覆盖源文件
        if not self.edit_suffix.text().strip():
            QMessageBox.warning(
                self, "后缀不能为空",
                "输出文件名后缀不能为空！\n\n"
                "后缀用于区分输出文件和源文件，留空时输出文件会与源文件同名，"
                "处理过程将直接覆盖源文件（且不可恢复）。\n\n"
                "请填写后缀后再开始处理，例如：_processed",
            )
            self.edit_suffix.setFocus()
            self.edit_suffix.selectAll()
            return

        # 智能压缩：第一次点击先做 bpp 检测，筛选完成后再次点击才真正压缩
        if "智能压缩" in self.combo_mode.currentText() and not self._smart_scanned:
            self.start_smart_scan()
            return

        tasks = self.generate_tasks()
        if not tasks:
            return

        self.btn_run.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_pause.setText("⏸ 暂停")
        self.btn_stop.setEnabled(True)
        
        self.progress_bar.setValue(0)
        self.log_text.clear()
        self.log_text.append(f"准备处理 {len(tasks)} 个文件...\n")
        self.log_subtitle_hint()

        self.worker = FFmpegWorker(tasks)
        self.worker.progress_signal.connect(self.update_progress)
        self.worker.log_signal.connect(self.append_log)
        self.worker.finished_signal.connect(self.processing_finished)
        self.worker.start()

    def start_smart_scan(self):
        """智能压缩第一步：后台检测 bpp"""
        files = [self.file_list.item(i).text() for i in range(self.file_list.count())]
        if not files:
            return

        self._set_list_busy(True)
        self.btn_run.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_pause.setText("⏸ 暂停")
        self.btn_stop.setEnabled(True)
        self.progress_bar.setRange(0, len(files))
        self.progress_bar.setValue(0)
        self.log_text.append(f"\n🔍 开始 bpp 检测：{len(files)} 个文件…")

        self.scan_worker = SmartScanWorker(self.ffprobe_path, files)
        self.scan_worker.progress_signal.connect(self._on_scan_progress)
        self.scan_worker.finished_signal.connect(self._on_scan_finished)
        self.worker = self.scan_worker   # 复用暂停 / 停止 / 关闭确认
        self.scan_worker.start()

    def _set_list_busy(self, busy):
        """扫描期间锁住列表与模式，避免边扫边改导致结果错位"""
        for w in (
            self.file_list, self.combo_mode,
            self.btn_add_files, self.btn_add_folder,
            self.btn_remove_selected, self.btn_clear_files,
            self.btn_media_info, self.btn_scan_overhead,
        ):
            w.setEnabled(not busy)
        if not busy:
            self._update_track_button_state()

    def _on_scan_progress(self, done, total):
        self.progress_bar.setValue(done)

    def _on_scan_finished(self, results, cancelled):
        self._set_list_busy(False)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText("⏸ 暂停")
        self.btn_stop.setEnabled(False)
        self.btn_run.setEnabled(True)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

        if cancelled:
            self.log_text.append("⏹ bpp 检测已取消，列表保持不变。")
            return
        if not results:
            self.log_text.append("⚠️ 没有可用的检测结果。")
            return

        self._log_scan_report(results)

        dlg = SmartScanDialog(
            results, self,
            ffmpeg_path=self.ffmpeg_path,
            ffprobe_path=self.ffprobe_path,
            crf=self.spin_crf.value(),
            log_sink=self.append_log,
        )
        if dlg.exec_() != QDialog.Accepted:
            self.log_text.append("已取消筛选，列表保持不变。")
            return

        chosen = dlg.selected_files()
        if not chosen:
            return

        skipped = sum(1 for r in results if r["verdict"] == "skip")
        trialed = sum(1 for r in results if r.get("trial") and r["trial"].get("ok"))

        # 记下哪些文件是 VFR，压缩时对它们保留原始时间戳
        vfr_paths = {os.path.normpath(r["path"]) for r in results if r.get("vfr")}

        self.file_list.clear()
        self._add_files_to_list(chosen)
        self._smart_scanned = True
        self._smart_vfr = vfr_paths
        self.log_text.append(
            "✅ 已用 {} 个文件替换列表（跳过 {} 个，源文件保持原位），"
            "点击「开始批量处理」开始压缩。".format(len(chosen), skipped)
        )
        if trialed:
            self.log_text.append("🧪 其中 {} 个文件做过抽样试压，结果见上方清单。".format(trialed))

    def _log_scan_report(self, results):
        ok = [r for r in results if r["verdict"] == "ok"]
        review = [r for r in results if r["verdict"] == "review"]
        vfr = [r for r in results if r["verdict"] == "vfr"]
        skip = [r for r in results if r["verdict"] == "skip"]
        error = [r for r in results if r["verdict"] == "error"]

        self.log_text.append(
            "\n📋 bpp 检测完成：共 {} 个 ｜ 可压 {} ｜ 边缘 {} ｜ 待试压 {} ｜ 跳过 {} ｜ 失败 {}".format(
                len(results), len(ok), len(review), len(vfr), len(skip), len(error)
            )
        )

        def fmt(r):
            if r["ok"]:
                tag = "  [VFR]" if r.get("vfr") else ""
                return (
                    "  • {}  bpp {:.4f}  {} {}×{} {:.2f}fps{}  {:.2f} Mbps".format(
                        r["path"], r["bpp"], r["codec"], r["width"],
                        r["height"], r["fps"], tag, r["bitrate"] / 1e6,
                    )
                )
            return "  • {}  检测失败：{}".format(r["path"], r["reason"])

        for title, group in (
            ("✅ 可压", ok), ("⚠️ 边缘", review),
            ("🔬 待试压（VFR，bpp 仅供参考）", vfr),
            ("⏭ 跳过", skip), ("❌ 失败", error),
        ):
            if not group:
                continue
            self.log_text.append(title + "：")
            for r in group:
                self.log_text.append(fmt(r))

    def log_subtitle_hint(self):
        """字幕封装到 MP4 时提示样式丢失（只写日志，不打断批量任务）"""
        if "字幕封装" not in self.combo_mode.currentText():
            return

        sub_ext = os.path.splitext(self.edit_sub.text().strip())[1].lower()
        if sub_ext not in (".ass", ".ssa"):
            return

        exts = set()
        for i in range(self.file_list.count()):
            exts.add(os.path.splitext(self.file_list.item(i).text())[1].lower())

        if exts & {".mp4", ".m4v", ".mov"}:
            self.log_text.append(
                "⚠️ 提示：MP4 容器使用 mov_text 封装字幕，ASS/SSA 的字体、位置、颜色等"
                "特效样式会丢失；如需保留特效，请将源文件转为 MKV 后再封装。\n"
            )

    def update_progress(self, current, total):
        percentage = int((current / total) * 100)
        self.progress_bar.setValue(percentage)

    def append_log(self, text):
        # 贴底跟随 / 原地不动、以及 3000 行截断，都由 LogView.append 统一处理
        self.log_text.append(text)

    def schedule_shutdown(self, seconds=60):
        """安排关机（延迟 seconds 秒，期间可用 shutdown /a 撤销）"""
        try:
            result = subprocess.run(
                ["shutdown", "/s", "/t", str(int(seconds))],
                creationflags=0x08000000,   # CREATE_NO_WINDOW：打包成无控制台版时不闪黑框
                check=False,
            )
        except Exception as e:
            self.log_text.append(f"❌ 无法安排自动关机：{e}")
            return False
        if result.returncode != 0:
            self.log_text.append(f"❌ 自动关机未能安排（shutdown 返回码 {result.returncode}）")
            return False
        return True

    def cancel_shutdown(self):
        """撤销已安排的关机"""
        try:
            result = subprocess.run(
                ["shutdown", "/a"], creationflags=0x08000000, check=False
            )
        except Exception as e:
            self.log_text.append(f"❌ 取消关机失败：{e}")
            return False
        # 1116 = 当前没有待处理的关机，属于正常情况
        if result.returncode not in (0, 1116):
            self.log_text.append(f"❌ 取消关机失败（返回码 {result.returncode}）")
            return False
        self.log_text.append("✅ 已取消自动关机。")
        return True

    def processing_finished(self, results=None):
        if self._closing:
            # 关闭流程中不再弹收尾对话框
            return
        results = results or []
        ok_items = [r for r in results if r.get("ok")]
        retried_items = [r for r in ok_items if r.get("retried")]
        stopped_items = [r for r in results if r.get("stopped")]
        failed = [r for r in results if not r.get("ok") and not r.get("stopped")]
        planned = len(getattr(self.worker, "tasks", []) or [])
        not_run = max(planned - len(results), 0)

        self.btn_run.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText("⏸ 暂停")
        self.btn_stop.setEnabled(False)

        # ---- 处理汇总 ----
        self.log_text.append("\n" + "=" * 52)
        self.log_text.append("📊 处理汇总")
        self.log_text.append("=" * 52)
        summary = (
            f"计划 {planned} 个 ｜ ✅ 成功 {len(ok_items)} 个 ｜ ❌ 失败 {len(failed)} 个"
        )
        if retried_items:
            summary += f" ｜ 🔁 重试成功 {len(retried_items)} 个"
        if stopped_items:
            summary += f" ｜ ⏹ 中止 {len(stopped_items)} 个"
        if not_run:
            summary += f" ｜ 未执行 {not_run} 个"
        self.log_text.append(summary)

        if failed:
            self.log_text.append("\n失败清单：")
            for r in failed:
                name = os.path.basename(r["output"])
                attempts = r.get("attempts") or 1
                if r.get("error"):
                    detail = r["error"]
                else:
                    detail = f"返回码 {describe_return_code(r.get('code'))}"
                if attempts > 1:
                    detail += f"，已自动重试 {attempts - 1} 次"
                self.log_text.append(f"  • {name}：{detail}")

        if stopped_items:
            self.log_text.append("\n已中止：")
            for r in stopped_items:
                self.log_text.append(f"  • {os.path.basename(r['output'])}")

        if failed:
            self.log_text.append("\n⚠️ 批量处理结束（存在失败的任务，详见上方清单）。")
        elif stopped_items or not_run:
            self.log_text.append("\n⏹ 批量处理已中止，剩余任务未执行。")
        else:
            self.log_text.append("\n🎉 全部任务处理完成！")

        # ---- 自动关机：只在本轮真跑过任务、且没被手动中止时执行；一次性 ----
        armed = self.check_auto_shutdown.isChecked()
        shutdown_scheduled = False
        if armed:
            self.check_auto_shutdown.setChecked(False)   # 不把状态带到下一轮
            if not results:
                self.log_text.append("⏻ 本轮没有执行任何任务，自动关机未触发。")
            elif stopped_items or not_run:
                self.log_text.append("⏹ 任务被中止，自动关机未触发。")
            elif self.schedule_shutdown(60):
                shutdown_scheduled = True
                self.log_text.append(
                    "⏻ 已安排 60 秒后自动关机"
                    "（取消：点下方「取消关机」按钮，或命令行执行 shutdown /a）"
                )

        # ---- 弹窗：成功、失败、中止用不同图标与标题 ----
        msg = QMessageBox(self)
        retry_note = (
            f"其中 {len(retried_items)} 个文件经自动重试后成功。" if retried_items else ""
        )
        if failed:
            msg.setWindowTitle("处理结束（有失败）")
            msg.setIcon(QMessageBox.Warning)
            msg.setText(f"批量处理结束：成功 {len(ok_items)} 个，失败 {len(failed)} 个。")
            msg.setInformativeText(
                retry_note + "失败原因见日志，失败产生的半成品已自动清理。是否打开输出文件夹？"
            )
        elif stopped_items or not_run:
            msg.setWindowTitle("已中止")
            msg.setIcon(QMessageBox.Information)
            msg.setText(
                f"批量处理已中止：完成 {len(ok_items)} 个，"
                f"中止 {len(stopped_items)} 个，未执行 {not_run} 个。"
            )
            msg.setInformativeText(retry_note + "是否打开输出文件夹？")
        else:
            msg.setWindowTitle("完成")
            msg.setIcon(QMessageBox.Information)
            msg.setText("批量处理已结束！")
            msg.setInformativeText(
                retry_note + f"共完成 {len(ok_items)} 个任务。是否打开输出文件夹？"
            )
        if shutdown_scheduled:
            msg.setInformativeText(
                msg.informativeText()
                + "\n\n60 秒后自动关机；还需继续用电脑的话，点「取消关机」。"
            )
        btn_open = msg.addButton("📂 打开输出文件夹", QMessageBox.AcceptRole)
        msg.addButton("关闭", QMessageBox.RejectRole)
        btn_cancel_shutdown = None
        if shutdown_scheduled:
            btn_cancel_shutdown = msg.addButton("🛑 取消关机", QMessageBox.DestructiveRole)
        msg.exec_()
        clicked = msg.clickedButton()
        if clicked == btn_open:
            self.open_output_folder()
        elif btn_cancel_shutdown is not None and clicked == btn_cancel_shutdown:
            self.cancel_shutdown()

    def toggle_pause(self):
        if hasattr(self, "worker") and self.worker.isRunning():
            if self.worker.is_paused:
                self.worker.resume_process()
                self.btn_pause.setText("⏸ 暂停")
                self.log_text.append("▶️ 任务已继续")
            else:
                self.worker.pause_process()
                self.btn_pause.setText("▶️ 继续")
                self.log_text.append("⏸ 任务已暂停")

    def stop_processing(self):
        if hasattr(self, "worker") and self.worker.isRunning():
            self.worker.stop_process()
            self.btn_pause.setEnabled(False)
            self.btn_stop.setEnabled(False)
            if isinstance(self.worker, SmartScanWorker):
                self.log_text.append("⏹ 正在取消 bpp 检测...")
            else:
                self.log_text.append("⏹ 正在停止当前任务并清理半成品...")

    def closeEvent(self, event):
        """关闭程序：有任务在跑时二次确认"""
        worker = getattr(self, "worker", None)
        if worker is not None and worker.isRunning():
            box = QMessageBox(self)
            box.setWindowTitle("正在处理")
            box.setIcon(QMessageBox.Warning)
            box.setText("当前还有任务正在处理。")
            if isinstance(worker, SmartScanWorker):
                tip = "退出会中止正在进行的 bpp 检测。\n\n确定要退出吗？"
            else:
                tip = "退出会立即终止 ffmpeg 进程，并清理未完成的半成品文件。\n\n确定要退出吗？"
            box.setInformativeText(tip)
            btn_quit = box.addButton("确认退出", QMessageBox.DestructiveRole)
            box.addButton("取消", QMessageBox.RejectRole)
            box.exec_()
            if box.clickedButton() is not btn_quit:
                event.ignore()
                return

            self._closing = True
            self.log_text.append("⏹ 用户关闭程序，正在终止当前任务…")
            worker.stop_process()
            if not worker.wait(5000):
                self.log_text.append("⚠️ 等待 ffmpeg 退出超时，界面将直接关闭。")

        self.log_text.append("=" * 28 + " 退出 FFmpeg GUI " + "=" * 28)
        event.accept()

    def _output_dir_for(self, input_file):
        """按当前输出设置，返回该文件应该输出到哪个目录"""
        if self.check_same_dir.isChecked():
            return os.path.dirname(input_file)
        return self.edit_out_dir.text().strip()

    def _expected_output_path(self, input_file):
        """按当前模式推算该文件处理后应生成的文件路径（仅用于判断是否已处理）"""
        mode = self.combo_mode.currentText()
        suffix = self.edit_suffix.text().strip()

        if "无编码合并" in mode:
            if self.file_list.count() == 0:
                return ""
            first_file = self.file_list.item(0).text()
            base_name, ext = os.path.splitext(os.path.basename(first_file))
            out_dir = self._output_dir_for(first_file)
        else:
            base_name, ext = os.path.splitext(os.path.basename(input_file))
            out_dir = self._output_dir_for(input_file)
            if "提取" in mode:
                ext = ".mka"
            elif "转码" in mode or "压缩" in mode:
                ext = ".mp4"

        if not out_dir:
            return ""
        return os.path.normpath(os.path.join(out_dir, f"{base_name}{suffix}{ext}"))

    def _is_task_finished(self, expected_path):
        """该文件是否已经跑过（成功、失败、正在处理都算有明确去向）"""
        if not expected_path:
            return False

        if os.path.exists(expected_path):
            return True

        worker = getattr(self, "worker", None)
        if worker is not None:
            current = getattr(worker, "current_output", "")
            if current and os.path.normpath(current) == expected_path:
                return True
            for r in getattr(worker, "results", []) or []:
                if os.path.normpath(r.get("output", "")) == expected_path:
                    return True
        return False

    def open_output_folder(self):
        """打开输出文件夹：优先按列表中选中的文件定位"""
        if self.file_list.count() == 0:
            QMessageBox.warning(self, "提示", "列表中没有文件，请先添加媒体文件。")
            return

        selected = self.file_list.selectedItems()
        target = ""
        if selected:
            current = self.file_list.currentItem()
            target = current.text() if current in selected else selected[0].text()

        if target:
            out_dir = self._output_dir_for(target)
            if not out_dir or not os.path.isdir(out_dir):
                QMessageBox.warning(self, "提示", "输出目录不存在，请检查输出设置。")
                return

            expected = self._expected_output_path(target)
            if not self._is_task_finished(expected):
                box = QMessageBox(self)
                box.setWindowTitle("尚未处理")
                box.setIcon(QMessageBox.Question)
                box.setText(f"「{os.path.basename(target)}」还没有处理结果。")
                box.setInformativeText(
                    f"是否打开它将要输出的目录？\n{out_dir}"
                )
                btn_yes = box.addButton("确认", QMessageBox.AcceptRole)
                box.addButton("取消", QMessageBox.RejectRole)
                box.exec_()
                if box.clickedButton() is not btn_yes:
                    return

            os.startfile(out_dir)
            return

        # 没有选中项时退回"当前输出位置"
        if not self.check_same_dir.isChecked():
            out_dir = self.edit_out_dir.text().strip()
        else:
            out_dir = os.path.dirname(self.file_list.item(0).text())

        if out_dir and os.path.isdir(out_dir):
            os.startfile(out_dir)
        else:
            QMessageBox.warning(self, "提示", "请先添加文件或指定输出目录")

    def scan_overhead(self):
        """扫描列表文件的冗余度"""
        if self.file_list.count() == 0:
            QMessageBox.warning(self, "提示", "列表中没有文件，请先添加")
            return

        if hasattr(self, "_overhead_worker") and self._overhead_worker is not None:
            if self._overhead_worker.isRunning():
                QMessageBox.information(self, "提示", "扫描正在进行中，请稍候...")
                return

        # 如果有选中项则只扫选中项，否则扫描整个列表
        selected = self.file_list.selectedItems()
        if selected:
            files = [item.text() for item in selected]
        else:
            files = [self.file_list.item(i).text() for i in range(self.file_list.count())]

        self.btn_scan_overhead.setEnabled(False)
        self.log_text.append("\n⏳ 正在扫描文件冗余度，请稍候...")

        self._overhead_worker = OverheadScannerWorker(self.ffprobe_path, files)
        self._overhead_worker.log_signal.connect(self.append_log)
        self._overhead_worker.finished_signal.connect(self._on_overhead_finished)
        self._overhead_worker.start()

    def _on_overhead_finished(self):
        self.btn_scan_overhead.setEnabled(True)

if __name__ == "__main__":
    # 先关掉 WER 崩溃弹窗，再启动界面：否则 ffmpeg 崩溃会弹框并卡住整个队列
    suppress_windows_error_dialogs()

    app = QApplication(sys.argv)
    
    # 使用 resource_path 获取正确路径，并且给主窗口也设置一次图标
    from PyQt5.QtGui import QIcon, QColor
    icon_path = resource_path("app.ico")
    app.setWindowIcon(QIcon(icon_path))
    
    app.setStyleSheet(build_stylesheet())
    window = MainWindow()
    window.setWindowIcon(QIcon(icon_path))  # 给窗口单独设置一次
    window.show()
    sys.exit(app.exec_())
