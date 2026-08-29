"""
安全与沙箱层 (Sandbox)
=====================
对应 framework.txt 中 "安全与沙箱层" 的设计:
  - 文件操作白名单: 只允许在指定根目录内读写
  - 风险操作审批:   Shell 执行默认需要 allow_shell=True (可接人工审批钩子)
  - 审计日志:       工具调用由 planner/tools.py 统一记录
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable


class SandboxPolicy:
    """路径白名单 + 风险操作开关。"""

    def __init__(
        self,
        allowed_roots: Iterable[str | Path],
        allow_shell: bool = False,
    ):
        self._roots = [Path(p).expanduser().resolve() for p in allowed_roots]
        self._allow_shell = allow_shell

    # ---------------- 文件操作 ----------------
    def check_path(self, path: str | Path, op: str = "访问") -> Path:
        """校验 path 是否在允许目录内, 返回规范化后的路径; 否则抛 PermissionError。"""
        p = Path(path).expanduser().resolve()
        if not any(p == root or root in p.parents for root in self._roots):
            raise PermissionError(
                f"{op}被沙箱拒绝: {p} 不在允许目录 {[str(r) for r in self._roots]} 内"
            )
        return p

    def ensure_dir(self, path: str | Path) -> Path:
        """校验并创建目录 (仅限白名单内)。"""
        p = self.check_path(path, "创建目录")
        p.mkdir(parents=True, exist_ok=True)
        return p

    # ---------------- 风险操作 ----------------
    def check_shell(self) -> None:
        """Shell 等高危操作: 策略未开启时拒绝。可在此接入人工审批。"""
        if not self._allow_shell:
            raise PermissionError(
                "Shell 执行被沙箱策略禁止 (SandboxPolicy(allow_shell=False)); "
                "如需开启请在 default_registry 中传入 allow_shell=True"
            )

    def __repr__(self) -> str:  # pragma: no cover
        return f"SandboxPolicy(roots={self._roots}, allow_shell={self._allow_shell})"
