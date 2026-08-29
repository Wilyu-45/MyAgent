"""
框架清单管理 (单一数据源)
========================
manifest.json 描述每个框架: 适配器模块 / 所在 venv / pip 包 / 验证目标。
- 新增框架: 编辑 manifest.json + 编写适配器 (或用 `python -m planner.frameworks add`)
- 更新框架: `python -m planner.frameworks update [all|名字]`
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

ADAPTERS_DIR = Path(__file__).resolve().parent          # planner/adapters
PROJECT_ROOT = ADAPTERS_DIR.parent.parent               # D:\agent
MANIFEST_FILE = ADAPTERS_DIR / "manifest.json"

MAIN_VENV = "myagent"
MAIN_VENV_PYTHON = PROJECT_ROOT / MAIN_VENV / "Scripts" / "python.exe"


# ============================================================
# 清单读写
# ============================================================
def load_manifest() -> dict:
    with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("frameworks", {})


def save_manifest(frameworks: dict) -> None:
    with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
        json.dump({"$comment": "Agent 框架清单 (单一数据源)。新增/更新框架只改这里 + 对应适配器文件。",
                   "frameworks": frameworks},
                  f, ensure_ascii=False, indent=2)
        f.write("\n")


def get_entry(name: str) -> dict:
    frameworks = load_manifest()
    if name not in frameworks:
        raise KeyError(f"框架 '{name}' 不在清单中, 可用: {', '.join(frameworks)}")
    return frameworks[name]


# ============================================================
# venv 解析
# ============================================================
def resolve_python(entry: dict) -> Path:
    """根据 entry['venv'] 解析 python 解释器路径。"""
    venv = entry.get("venv", MAIN_VENV)
    if venv == MAIN_VENV:
        return MAIN_VENV_PYTHON
    return PROJECT_ROOT / venv / "Scripts" / "python.exe"


def ensure_venv(venv_name: str) -> Path:
    """确保 venv 存在 (不存在则创建), 返回 python 路径。"""
    if venv_name == MAIN_VENV:
        return MAIN_VENV_PYTHON
    py = PROJECT_ROOT / venv_name / "Scripts" / "python.exe"
    if not py.is_file():
        base = PROJECT_ROOT / venv_name
        print(f"[venv] 创建 {base} ...")
        r = subprocess.run([str(MAIN_VENV_PYTHON), "-m", "venv", str(base)])
        if r.returncode != 0:
            raise RuntimeError(f"创建 venv 失败: {base}")
    return py


# ============================================================
# 版本查询
# ============================================================
def run_py(python: Path, code: str, timeout: int = 300) -> str:
    """在指定 python 里执行一段代码, 返回 stdout。"""
    r = subprocess.run(
        [str(python), "-c", code],
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(f"python 执行失败: {(r.stderr or r.stdout)[-500:]}")
    return (r.stdout or "").strip()


def get_installed_version(python: Path, package: str) -> Optional[str]:
    """查询某 venv 中已安装的包版本 (去掉 extras, 如 fastmcp-slim[server] -> fastmcp-slim)。"""
    base = package.split("[")[0].strip()
    try:
        return run_py(python, f"import importlib.metadata as m; print(m.version({base!r}))") or None
    except Exception:
        return None


def get_latest_version(python: Path, package: str) -> Optional[str]:
    """用 pip index versions 查询最新版本。"""
    base = package.split("[")[0].strip()
    r = subprocess.run(
        [str(python), "-m", "pip", "index", "versions", base],
        capture_output=True, text=True, timeout=120,
    )
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.splitlines():
        if "Available versions:" in line:
            parts = line.split(":", 1)[1].strip().split(",")
            return parts[0].strip() if parts else None
    return None


def pip_install(python: Path, packages: list[str], upgrade: bool = False) -> str:
    """在指定 venv 安装/升级包。"""
    cmd = [str(python), "-m", "pip", "install", "--disable-pip-version-check"]
    if upgrade:
        cmd.append("--upgrade")
    cmd += packages
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-8:]
    if r.returncode != 0:
        raise RuntimeError("pip 安装失败:\n" + "\n".join(tail))
    return "\n".join(tail)
