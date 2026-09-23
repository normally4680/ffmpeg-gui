import sys
import os
import subprocess
import shutil
import tempfile
import json
import time
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
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal

# ==========================================
# 全局样式表
# ==========================================
GLOBAL_QSS = """
QWidget {
    font-family: "Microsoft YaHei", "微软雅黑", sans-serif;
    font-size: 14px;
}
QGroupBox {
    font-size: 15px;
    font-weight: bold;
    margin-top: 12px;
    padding-top: 8px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
}
QLineEdit, QComboBox {
    padding: 6px 8px;
    min-height: 20px;
}
QPushButton {
    padding: 6px 14px;
    min-height: 20px;
}
QListWidget {
    padding: 4px;
}
QTextEdit {
    font-size: 13px;
    padding: 4px;
}
QCheckBox {
    spacing: 6px;
}
QProgressBar {
    min-height: 22px;
    text-align: center;
}
"""

# ==========================================
# 核心执行线程，防止界面卡死
# ==========================================
class FFmpegWorker(QThread):
    progress_signal = pyqtSignal(int, int)  # 当前进度, 总数
    log_signal = pyqtSignal(str)  # 日志输出
    finished_signal = pyqtSignal()  # 完成信号

    def __init__(self, tasks):
        super().__init__()
        self.tasks = tasks  # 包含 (cmd, output_file) 的列表

    def run(self):
        total = len(self.tasks)
        for idx, (cmd, output_file) in enumerate(self.tasks):
            self.log_signal.emit(f"\n[{idx+1}/{total}] 开始处理: {os.path.basename(output_file)}")

            try:
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE

                process = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    encoding="utf-8", errors="ignore", startupinfo=startupinfo,
                )

                last_emit = 0
                last_line = ""
                for line in process.stdout:
                    line = line.strip()
                    if not line:
                        continue

                    # 错误和警告立即显示
                    if "error" in line.lower() or "warning" in line.lower():
                        self.log_signal.emit(line)
                        continue

                    # 进度行：最多每 2 秒刷新一次
                    now = time.time()
                    if ("frame=" in line or "size=" in line or "speed=" in line):
                        if now - last_emit >= 2.0:
                            self.log_signal.emit(line)
                            last_emit = now
                            last_line = line
                    else:
                        self.log_signal.emit(line)

                process.wait()
                if last_line:
                    self.log_signal.emit(last_line)   # 补最后一条进度

                if process.returncode == 0:
                    self.log_signal.emit(f"✅ 成功: {os.path.basename(output_file)}")
                else:
                    self.log_signal.emit(f"❌ 失败 (返回码 {process.returncode})")

            except Exception as e:
                self.log_signal.emit(f"❌ 发生异常: {str(e)}")

            self.progress_signal.emit(idx + 1, total)

        self.finished_signal.emit()

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

        self.init_ui()
        self.check_env()

    def init_ui(self):
        # 主部件和布局
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        # --- 1. 文件选择区（列表在左，按钮竖排在右） ---
        file_group = QGroupBox("1. 选择媒体文件（支持批量）")
        file_layout = QHBoxLayout()

        self.file_list = QListWidget()
        self.file_list.setSelectionMode(QListWidget.ExtendedSelection)

        # 右侧按钮竖排：统一高度和间距，方便计算总高
        BUTTON_HEIGHT = 36   # 单个按钮高度
        BUTTON_GAP = 8       # 按钮之间间距

        file_btn_layout = QVBoxLayout()
        file_btn_layout.setSpacing(BUTTON_GAP)
        file_btn_layout.setContentsMargins(0, 0, 0, 0)

        self.btn_add_files = QPushButton("添加文件")
        self.btn_add_folder = QPushButton("添加文件夹")
        self.btn_remove_selected = QPushButton("移除选中")
        self.btn_clear_files = QPushButton("清空列表")
        self.btn_media_info = QPushButton("📽️ 查看媒体信息")

        for btn in [self.btn_add_files, self.btn_add_folder,
                    self.btn_remove_selected, self.btn_clear_files,
                    self.btn_media_info]:
            btn.setFixedHeight(BUTTON_HEIGHT)
            file_btn_layout.addWidget(btn)

        # 列表高度 = 5 个按钮 + 4 个间距
        list_height = BUTTON_HEIGHT * 5 + BUTTON_GAP * 4
        self.file_list.setFixedHeight(list_height)

        file_layout.addWidget(self.file_list)
        file_layout.addLayout(file_btn_layout)
        file_group.setLayout(file_layout)

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
                "视频转码为 MP4 (H.264)",
            ]
        )
        self.combo_mode.setFixedHeight(PARAM_H)
        param_layout.addWidget(self.combo_mode)

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

        # 字幕参数
        self.lbl_sub = QLabel("字幕文件:")
        self.lbl_sub.setFixedHeight(PARAM_H)
        self.edit_sub = QLineEdit()
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
        self.btn_run.setStyleSheet("font-weight: bold; background-color: #4CAF50; color: white;")
        self.btn_run.setFixedHeight(40)

        self.btn_open_out = QPushButton("📂 打开输出文件夹")
        self.btn_open_out.setFixedHeight(40)

        # 两个按钮同一行各占一半
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.btn_run, stretch=1)
        btn_row.addWidget(self.btn_open_out, stretch=1)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setPlaceholderText("执行日志将显示在这里...")
        self.log_text.setMinimumHeight(250)

        run_layout.addWidget(self.progress_bar)
        run_layout.addLayout(btn_row)
        run_layout.addWidget(self.log_text)
        run_group.setLayout(run_layout)

        # 添加到主布局
        main_layout.addWidget(file_group, stretch=0)
        main_layout.addWidget(param_group, stretch=0)
        main_layout.addWidget(out_group, stretch=0)
        main_layout.addWidget(run_group, stretch=5)

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

        # 初始更新参数显示
        self.update_mode_params(0)

    def check_env(self):
        """检查 FFmpeg 环境"""
        if not self.ffmpeg_path:
            self.log_text.append(
                "⚠️ 警告：在系统 PATH 中未找到 ffmpeg！请确保已加入环境变量，或修改代码中的路径。"
            )
        else:
            self.log_text.append(f"✅ 已检测到 FFmpeg: {self.ffmpeg_path}")
            self.log_text.append(
                "提示：无编码模式（截取、封装、提取）速度极快；转码模式较慢。\n"
            )

    def update_mode_params(self, index):
        """根据模式更新参数显示"""
        mode = self.combo_mode.currentText()

        # 隐藏所有动态参数
        for w in [
            self.check_start,
            self.edit_start,
            self.check_end,
            self.edit_end,
            self.lbl_sub,
            self.edit_sub,
            self.btn_select_sub,
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

    def add_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择媒体文件", "",
            "媒体文件 (*.mp4 *.mkv *.avi *.mov *.flv *.ts *.mp3 *.wav);;所有文件 (*)",
        )
        if files:
            self._add_files_to_list(files)

    def add_folder(self):
        """选择文件夹，递归扫描其中所有媒体文件并加入列表"""
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if not folder:
            return

        MEDIA_EXTS = {
            ".mp4", ".mkv", ".avi", ".mov", ".flv", ".ts", ".webm",
            ".m4v", ".wmv", ".mpg", ".mpeg", ".rmvb",
            ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma",
            ".srt", ".ass", ".vtt",
        }

        found = []
        for root, _, names in os.walk(folder):
            for name in names:
                ext = os.path.splitext(name)[1].lower()
                if ext in MEDIA_EXTS:
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

        if added > 0 and not self.file_list.selectedItems():
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

    def clear_files(self):
        self.file_list.clear()

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

    def generate_tasks(self):
        """生成所有执行任务"""
        tasks = []
        mode = self.combo_mode.currentText()
        suffix = self.edit_suffix.text().strip()

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
            elif "转码" in mode:
                out_name = f"{base_name}{suffix}.mp4"
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
                cmd.extend(["-i", input_file, "-i", sub_file, "-c", "copy", "-c:s", "mov_text", output_file])

            elif "提取" in mode:
                cmd.extend(["-i", input_file, "-vn", "-c:a", "copy", output_file])

            elif "修复视频" in mode:
                cmd.extend(["-i", input_file, "-c", "copy"])
                # 只有输出是 mp4 时，faststart 才有意义（把 moov 移到文件头）
                if ext.lower() == ".mp4":
                    cmd.extend(["-movflags", "+faststart"])
                cmd.append(output_file)

            elif "视频转码" in mode:
                cmd.extend(["-i", input_file, "-c:v", "libx264", "-c:a", "aac", output_file])

            tasks.append((cmd, output_file))

        return tasks

    def start_processing(self):
        if self.file_list.count() == 0:
            QMessageBox.warning(self, "提示", "请先添加需要处理的文件！")
            return

        tasks = self.generate_tasks()
        if not tasks:
            return

        # 禁用按钮，防止重复点击
        self.btn_run.setEnabled(False)
        self.progress_bar.setValue(0)
        self.log_text.clear()
        self.log_text.append(f"准备处理 {len(tasks)} 个文件...\n")

        # 启动线程
        self.worker = FFmpegWorker(tasks)
        self.worker.progress_signal.connect(self.update_progress)
        self.worker.log_signal.connect(self.append_log)
        self.worker.finished_signal.connect(self.processing_finished)
        self.worker.start()

    def update_progress(self, current, total):
        percentage = int((current / total) * 100)
        self.progress_bar.setValue(percentage)

    def append_log(self, text):
        self.log_text.append(text)
        # 限制日志最多 3000 行，超出删除最旧的
        MAX_LINES = 3000
        doc = self.log_text.document()
        if doc.blockCount() > MAX_LINES:
            cursor = self.log_text.textCursor()
            cursor.movePosition(cursor.Start)
            cursor.movePosition(cursor.Down, cursor.KeepAnchor, doc.blockCount() - MAX_LINES)
            cursor.removeSelectedText()

        self.log_text.verticalScrollBar().setValue(
            self.log_text.verticalScrollBar().maximum()
        )

    def processing_finished(self):
        self.log_text.append("\n🎉 所有任务处理完成！")
        self.btn_run.setEnabled(True)

        # 构造带“打开输出文件夹”按钮的完成提示
        msg = QMessageBox(self)
        msg.setWindowTitle("完成")
        msg.setIcon(QMessageBox.Information)
        msg.setText("批量处理已全部完成！")
        msg.setInformativeText("是否需要打开输出文件夹？")

        btn_open = msg.addButton("📂 打开输出文件夹", QMessageBox.AcceptRole)
        msg.addButton("关闭", QMessageBox.RejectRole)

        msg.exec_()

        if msg.clickedButton() == btn_open:
            self.open_output_folder()

    def open_output_folder(self):
        """打开输出文件夹"""
        # 优先使用显式指定的输出目录
        if not self.check_same_dir.isChecked():
            out_dir = self.edit_out_dir.text().strip()
            if out_dir and os.path.isdir(out_dir):
                os.startfile(out_dir)
                return
            QMessageBox.warning(self, "提示", "输出目录未设置或不存在")
            return

        # 输出到源文件目录：取列表里第一个文件的所在目录
        if self.file_list.count() > 0:
            first_file = self.file_list.item(0).text()
            out_dir = os.path.dirname(first_file)
            if out_dir and os.path.isdir(out_dir):
                os.startfile(out_dir)
                return

        QMessageBox.warning(self, "提示", "请先添加文件或指定输出目录")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet(GLOBAL_QSS)   # ← 应用全局样式
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
