import subprocess
import sys

def compile_ui(ui_file, py_file):
    # 直接调用虚拟环境中的 pyuic5.exe
    pyuic5_path = r".\venv\Scripts\pyuic5.exe"
    cmd = [pyuic5_path, ui_file, "-o", py_file]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"成功编译 {ui_file} -> {py_file}")
    else:
        print(f"编译失败: {result.stderr}")

if __name__ == "__main__":
    compile_ui("main_window.ui", "ui_main_window.py")