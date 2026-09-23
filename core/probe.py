import subprocess
import json
import shutil

def get_ffprobe_path():
    """自动查找 ffprobe，优先系统 PATH"""
    path = shutil.which("ffprobe")
    if path:
        return path
    raise FileNotFoundError(
        "未找到 ffprobe，请确认 FFmpeg 的 bin 目录已加入系统 PATH，"
        "或在 core/probe.py 中硬编码路径。"
    )

def probe_media(file_path):
    """调用 ffprobe 获取媒体信息，返回字典"""
    ffprobe = get_ffprobe_path()
    cmd = [
        ffprobe,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        file_path
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=True
        )
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        return {"error": f"ffprobe 执行失败: {e.stderr}"}
    except json.JSONDecodeError:
        return {"error": "无法解析 ffprobe 输出"}