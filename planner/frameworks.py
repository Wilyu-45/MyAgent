"""
Agent 框架管理体系 CLI
======================
用途: 框架升级 / 新框架融入 / 状态与验证, 一条命令完成。

用法:
  python -m planner.frameworks status                    # 框架状态总览 (版本/venv/健康)
  python -m planner.frameworks status --json             # JSON 输出 (供脚本使用)
  python -m planner.frameworks check                     # 检查各框架是否有可用更新
  python -m planner.frameworks update [名字|all] [--dry-run]   # 升级依赖 + 离线验证
  python -m planner.frameworks verify [名字|all] [--real]      # 验证适配器可用 (--real 连模型)
  python -m planner.frameworks add <名字> --packages pkg1 pkg2 [--venv myagent|new] [--dry-run]
                                                         # 融入新框架 (脚手架)
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Optional

from .adapters.manifest import (
    MAIN_VENV,
    MAIN_VENV_PYTHON,
    get_entry,
    get_installed_version,
    get_latest_version,
    load_manifest,
    pip_install,
    resolve_python,
    run_py,
    save_manifest,
)
from .adapters.base import PROJECT_ROOT


# ============================================================
# 工具
# ============================================================
def _import_names(package: str) -> list[str]:
    """pip 包名 -> 可能的 import 模块名 (去 extras)。"""
    base = package.split("[")[0].strip()
    return [base.replace("-", "_"), base.replace("-", ".")]


def _offline_verify(name: str, entry: dict) -> tuple[bool, str]:
    """离线健康检查: 适配器可导入 + 主包已安装。"""
    # 1) 主包导入检查 (在框架所在 venv 里; 优先 manifest 里的 import_name)
    python = resolve_python(entry)
    import_names = [entry["import_name"]] if entry.get("import_name") else _import_names(entry["packages"][0])
    try:
        ok_import = False
        for cand in import_names:
            try:
                run_py(python, f"import {cand}; print('ok')", timeout=120)
                ok_import = True
                break
            except Exception:
                continue
        if not ok_import:
            return False, f"包导入失败: {entry['packages'][0]}"
    except Exception as e:
        return False, f"包导入失败: {str(e)[-200:]}"
    # 2) 适配器导入检查 (主 venv, 仅主 venv 框架)
    if entry.get("venv", MAIN_VENV) == MAIN_VENV:
        try:
            importlib.import_module(f"planner.adapters.{entry['adapter']}")
        except Exception as e:
            return False, f"适配器导入失败: {str(e)[-200:]}"
    return True, "ok"


def _real_verify(name: str, entry: dict, verbose: bool = False) -> tuple[bool, str]:
    """真实验证: 调用 run_framework 跑 verify.goal (需 modelservice 在运行)。"""
    from .adapters import run_framework

    v = entry.get("verify") or {"goal": "1+1等于几?只回答数字", "max_steps": 4}
    try:
        r = run_framework(name, v["goal"], max_steps=v.get("max_steps"), verbose=verbose)
    except Exception as e:
        return False, f"运行失败: {type(e).__name__}: {e}"
    ok = r.get("status") == "finished"
    return ok, f"status={r.get('status')} 步数={r.get('steps', 0)} 答复={str(r.get('final_answer', ''))[:80]}"


# ============================================================
# 子命令
# ============================================================
def cmd_status(as_json: bool = False) -> int:
    fw = load_manifest()
    rows = []
    for name, entry in fw.items():
        python = resolve_python(entry)
        ver = get_installed_version(python, entry["packages"][0]) or "-"
        ok, msg = _offline_verify(name, entry)
        rows.append({
            "framework": name,
            "version": ver,
            "venv": entry.get("venv", MAIN_VENV),
            "packages": entry["packages"],
            "runner": entry.get("runner", False),
            "health": "ok" if ok else f"BROKEN: {msg}",
            "notes": entry.get("notes", ""),
        })
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    print(f"{'框架':<14}{'版本':<16}{'venv':<18}{'健康':<10}说明")
    print("-" * 100)
    for r in rows:
        print(f"{r['framework']:<14}{r['version']:<16}{r['venv']:<18}{r['health']:<10}{r['notes']}")
    return 0


def cmd_check() -> int:
    fw = load_manifest()
    any_update = False
    for name, entry in fw.items():
        python = resolve_python(entry)
        pinned = {p.split("==")[0] for p in entry.get("pinned", [])}
        for pkg in entry["packages"]:
            base = pkg.split("[")[0].strip()
            if base in pinned:
                pin_ver = next(p.split("==")[1] for p in entry.get("pinned", []) if p.split("==")[0] == base)
                print(f"  {name:<14} {pkg:<28} 🔒 锁定 {pin_ver}")
                continue
            inst = get_installed_version(python, pkg)
            latest = get_latest_version(python, pkg)
            if latest is None:
                print(f"  {name:<14} {pkg:<28} 最新版本查询失败 (可能离线)")
            elif latest != inst:
                any_update = True
                print(f"  {name:<14} {pkg:<28} {inst or '-'} -> {latest}  ⬆ 可更新")
            else:
                print(f"  {name:<14} {pkg:<28} {inst} (已是最新)")
    if not any_update:
        print("\n所有框架均为最新版本。")
    return 0


def cmd_update(names: list[str], dry_run: bool) -> int:
    fw = load_manifest()
    targets = names if names else list(fw)
    unknown = [n for n in targets if n not in fw]
    if unknown:
        print(f"未知框架: {unknown}, 可用: {', '.join(fw)}")
        return 1
    for name in targets:
        entry = fw[name]
        python = resolve_python(entry)
        print(f"\n=== [{name}] ({entry.get('venv', MAIN_VENV)}) ===")
        pinned = {p.split("==")[0] for p in entry.get("pinned", [])}
        upgradable: dict[str, str] = {}
        for pkg in entry["packages"]:
            base = pkg.split("[")[0].strip()
            if base in pinned:
                pin_ver = next(p.split("==")[1] for p in entry.get("pinned", []) if p.split("==")[0] == base)
                print(f"  {pkg}: 🔒 已锁定 {pin_ver} (清单 pinned, 跳过更新)")
                continue
            inst = get_installed_version(python, pkg)
            latest = get_latest_version(python, pkg)
            if latest is None:
                print(f"  {pkg}: 当前 {inst or '-'}, 最新版本查询失败 (跳过)")
            elif latest != inst:
                upgradable[pkg] = latest
                print(f"  {pkg}: {inst or '-'} -> {latest}  ⬆")
            else:
                print(f"  {pkg}: {inst} (已是最新)")
        if dry_run or not upgradable:
            continue
        try:
            print("  正在升级...")
            pip_install(python, list(upgradable), upgrade=True)
            for pkg in upgradable:
                after = get_installed_version(python, pkg)
                print(f"  升级完成 {pkg}: {after}")
        except Exception as e:
            print(f"  ✗ 升级失败: {e}")
            print(f"  提示: 可回滚到旧版本, 如 pip install {list(upgradable)[0]}==<旧版本>")
            continue
        ok, msg = _offline_verify(name, entry)
        print(f"  离线验证: {'✅ ' + msg if ok else '❌ ' + msg}")
    return 0


def cmd_verify(names: list[str], real: bool) -> int:
    fw = load_manifest()
    targets = names if names else list(fw)
    failed = []
    for name in targets:
        entry = fw[name]
        if real:
            ok, msg = _real_verify(name, entry)
        else:
            ok, msg = _offline_verify(name, entry)
        mark = "✅" if ok else "❌"
        print(f"  {mark} [{name}] {msg}")
        if not ok:
            failed.append(name)
    if failed:
        print(f"\n失败: {failed} (可用 --real 做真实模型验证, 需 modelservice 在运行)")
        return 1
    return 0


def cmd_add(name: str, packages: list[str], venv: str, dry_run: bool) -> int:
    import re

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-_]*", name or ""):
        print("框架名只能以字母/数字开头, 含字母/数字/连字符/下划线。")
        return 1
    if not packages:
        print("必须用 --packages 指定至少一个 pip 包 (如 --packages openai-agents)。")
        return 1
    fw = load_manifest()
    if name in fw:
        print(f"框架 '{name}' 已在清单中, 如需更新请用 update。")
        return 1

    venv_name = MAIN_VENV if venv == "myagent" else (
        f"myagent_{name.replace('-', '_')}" if venv == "new" else venv
    )
    adapter_file = f"{name.replace('-', '_')}_agent.py"
    entry = {
        "adapter": name.replace("-", "_") + "_agent",
        "venv": venv_name,
        "packages": packages,
        "verify": {"goal": "1+1等于几?只回答数字", "max_steps": 4},
        "notes": f"通过 add 命令添加 ({', '.join(packages)})",
    }

    print(f"[add] 框架={name}  venv={venv_name}  包={packages}")
    if dry_run:
        print("[add] dry-run: 将执行: 1) 生成适配器 2) 创建 venv(如需要) 3) pip 安装 4) 注册清单")
        return 0

    # 1) 生成适配器文件
    tpl = (Path(__file__).parent / "adapters" / "_template.py.txt").read_text(encoding="utf-8")
    tpl = tpl.replace("__FRAMEWORK__", name)
    adapter_path = Path(__file__).parent / "adapters" / adapter_file
    adapter_path.write_text(tpl, encoding="utf-8")
    print(f"  已生成适配器: {adapter_path}")

    # 2) 创建 venv (如需要)
    from .adapters.manifest import ensure_venv

    python = ensure_venv(venv_name)

    # 3) 安装依赖
    try:
        pip_install(python, packages)
        print(f"  已安装: {', '.join(packages)}")
    except Exception as e:
        print(f"  ✗ 安装失败: {e}")
        print("  提示: 修复依赖后重试, 或手动 pip install。")

    # 4) 注册清单
    fw[name] = entry
    save_manifest(fw)
    print(f"  已注册到 manifest.json")

    print("\n下一步:")
    print(f"  1. 编辑 {adapter_path} 实现 run() (参考 smolagents_agent.py)")
    print(f"  2. python -m planner.frameworks verify {name}")
    print(f"  3. python -m planner \"你的目标\" --framework {name}")
    return 0


# ============================================================
# 入口
# ============================================================
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m planner.frameworks",
        description="Agent 框架管理: 升级 / 新增 / 验证 / 状态",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="框架状态总览")
    p_status.add_argument("--json", action="store_true", dest="as_json")

    sub.add_parser("check", help="检查可用更新")

    p_update = sub.add_parser("update", help="升级依赖并离线验证")
    p_update.add_argument("names", nargs="*", default=None, help="框架名, 默认全部")
    p_update.add_argument("--dry-run", action="store_true")

    p_verify = sub.add_parser("verify", help="验证适配器可用")
    p_verify.add_argument("names", nargs="*", default=None, help="框架名, 默认全部")
    p_verify.add_argument("--real", action="store_true", help="真实模型验证 (需 modelservice)")

    p_add = sub.add_parser("add", help="融入新框架 (脚手架)")
    p_add.add_argument("name", help="框架名 (如 openai-agents)")
    p_add.add_argument("--packages", nargs="+", required=True, help="pip 包列表")
    p_add.add_argument("--venv", default="myagent",
                       help="myagent (主环境) | new (新建 myagent_<名>) | 自定义目录名")
    p_add.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "status":
        return cmd_status(args.as_json)
    if args.cmd == "check":
        return cmd_check()
    if args.cmd == "update":
        return cmd_update(args.names, args.dry_run)
    if args.cmd == "verify":
        return cmd_verify(args.names, args.real)
    if args.cmd == "add":
        return cmd_add(args.name, args.packages, args.venv, args.dry_run)
    return 1


if __name__ == "__main__":
    sys.exit(main())
