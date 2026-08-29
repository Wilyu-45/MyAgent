"""
感知层 (Perception)
===================
对应 framework.txt 中 "感知层" 的设计:
  - 系统状态感知: 平台 / CPU / 内存 / 磁盘 / 前台窗口
  - 视觉感知:     截图 (依赖 Pillow, 未安装时返回提示)
  - 文件系统状态: 由 tools 层提供 (list_files / search_files 等)

本层只做"读取", 不修改系统状态。
"""
from __future__ import annotations

import os
import platform
import shutil
import time


def get_system_info() -> str:
    """汇总系统基本信息 (纯标准库, 有 psutil 时补充内存信息)。"""
    lines = [
        f"平台: {platform.platform()}",
        f"机器: {platform.machine()}",
        f"CPU: {platform.processor() or '未知'} / 逻辑核心 {os.cpu_count()}",
        f"Python: {platform.python_version()}",
        f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    try:  # 内存 (可选依赖)
        import psutil

        vm = psutil.virtual_memory()
        lines.append(f"内存: 总量 {vm.total / 2**30:.1f} GiB, 可用 {vm.available / 2**30:.1f} GiB")
    except Exception:
        pass
    try:  # 当前工作目录磁盘
        du = shutil.disk_usage(os.getcwd())
        lines.append(
            f"磁盘({os.getcwd()}): 总量 {du.total / 2**30:.1f} GiB, 可用 {du.free / 2**30:.1f} GiB"
        )
    except Exception:
        pass
    return "\n".join(lines)


def get_frontmost_window() -> str:
    """当前前台窗口标题 (Windows); 其他平台返回 None。"""
    if platform.system() != "Windows":
        return "(仅 Windows 支持前台窗口检测)"
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        return title or "(前台窗口无标题)"
    except Exception as e:  # pragma: no cover
        return f"(前台窗口检测失败: {e!r})"


def capture_screen() -> str:
    """截取当前屏幕并保存为临时 PNG (依赖 Pillow); 未安装时返回提示。"""
    try:
        from PIL import ImageGrab

        path = os.path.join(os.environ.get("TEMP", "."), f"agent_screen_{int(time.time())}.png")
        img = ImageGrab.grab()
        img.save(path)
        return f"已截图保存到 {path} ({img.size[0]}x{img.size[1]})"
    except Exception as e:
        return f"屏幕截图不可用: {e!r} (需要 pip install pillow)"
