# 多用户支持: 本地账号存储 + 会话管理 + 审计日志 (全部零依赖, 标准库实现)。
# 设计约束:
#   - 默认关闭 (不传 users_file 即单用户/令牌模式, 行为完全不变)
#   - 密码 PBKDF2-HMAC-SHA256 (12 万次迭代 + 随机盐), 不存明文
#   - 会话 token 仅存内存 (重启即失效, 重新登录)
#   - 校验 / 容量 fail-safe: 非法输入 ValueError, 存储损坏退回空库
"""多用户核心: 账号存储 / 会话管理 / 审计日志。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from typing import Dict, List, Optional

DEFAULT_USERS_FILE = os.path.join("memory", "users.json")
DEFAULT_AUDIT_FILE = os.path.join("memory", "audit.jsonl")

PBKDF2_ITERATIONS = 120_000
MAX_USERS = 64
NAME_RE = re.compile(r"^[\w\u4e00-\u9fa5-]{1,32}$")
ROLES = ("admin", "user")
DEFAULT_ROLE = "user"
SESSION_TTL_SECONDS = 24 * 3600


def hash_password(password: str, salt: Optional[str] = None, iterations: int = PBKDF2_ITERATIONS) -> str:
    """返回可存储的哈希串 pbkdf2$<iters>$<salt_hex>$<hash_hex>。"""
    salt_hex = secrets.token_hex(16) if salt is None else salt
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), iterations)
    return f"pbkdf2${iterations}${salt_hex}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码; 哈希串格式不合法返回 False (不抛出)。"""
    try:
        algo, iters, salt_hex, _ = stored.split("$")
        if algo != "pbkdf2":
            return False
        want = hash_password(password, salt_hex, int(iters))
        return secrets.compare_digest(want.split("$")[3], stored.split("$")[3])
    except (ValueError, TypeError):
        return False


class UserStore:
    """账号存储: {name: {pw, role, created}} 原子落盘 JSON。"""

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.getenv("AGENT_USERS_FILE") or DEFAULT_USERS_FILE
        self._lock = threading.Lock()
        self._users: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._users = {k: v for k, v in data.items()
                               if isinstance(v, dict) and isinstance(v.get("pw"), str)}
        except (OSError, ValueError):
            self._users = {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._users, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def __len__(self) -> int:
        return len(self._users)

    def list(self) -> List[dict]:
        with self._lock:
            return [{"name": n, "role": u.get("role", DEFAULT_ROLE),
                     "created": u.get("created", "")}
                    for n, u in sorted(self._users.items())]

    def exists(self, name: str) -> bool:
        return name in self._users

    def add(self, name: str, password: str, role: str = DEFAULT_ROLE) -> dict:
        if not NAME_RE.match(name or ""):
            raise ValueError("用户名需为 1-32 位字母/数字/汉字/-/_")
        if len(password or "") < 6:
            raise ValueError("密码至少 6 位")
        if role not in ROLES:
            raise ValueError("角色仅支持 admin / user")
        with self._lock:
            if name in self._users:
                raise ValueError(f"用户 {name} 已存在")
            if len(self._users) >= MAX_USERS:
                raise ValueError(f"用户数已达上限 ({MAX_USERS})")
            self._users[name] = {"pw": hash_password(password), "role": role,
                                 "created": time.strftime("%Y-%m-%d %H:%M:%S")}
            self._save()
            return {"name": name, "role": role}

    def remove(self, name: str) -> None:
        with self._lock:
            if name not in self._users:
                raise ValueError(f"用户 {name} 不存在")
            del self._users[name]
            self._save()

    def set_role(self, name: str, role: str) -> None:
        if role not in ROLES:
            raise ValueError("角色仅支持 admin / user")
        with self._lock:
            if name not in self._users:
                raise ValueError(f"用户 {name} 不存在")
            self._users[name]["role"] = role
            self._save()

    def set_password(self, name: str, password: str) -> None:
        if name not in self._users:
            raise ValueError(f"用户 {name} 不存在")
        if len(password or "") < 6:
            raise ValueError("密码至少 6 位")
        with self._lock:
            self._users[name]["pw"] = hash_password(password)
            self._save()

    def verify(self, name: str, password: str) -> Optional[str]:
        """校验成功返回角色, 失败返回 None。"""
        user = self._users.get(name)
        if not user or not verify_password(password or "", user["pw"]):
            return None
        return user.get("role", DEFAULT_ROLE)

    def role(self, name: str) -> Optional[str]:
        user = self._users.get(name)
        return user.get("role", DEFAULT_ROLE) if user else None

    def seed_admin(self, password: Optional[str] = None) -> str:
        """users 文件为空时创建初始管理员, 返回明文密码 (随机生成时由调用方打印)。"""
        if "admin" in self._users:
            raise ValueError("admin 已存在")
        password = password or secrets.token_urlsafe(12)
        self.add("admin", password, "admin")
        return password


class SessionManager:
    """内存会话: token → {user, role, exp}; 重启即失效。"""

    def __init__(self, ttl_seconds: int = SESSION_TTL_SECONDS):
        self.ttl = ttl_seconds
        self._lock = threading.Lock()
        self._sessions: Dict[str, dict] = {}

    def issue(self, user: str, role: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sweep()
            self._sessions[token] = {"user": user, "role": role,
                                     "exp": time.time() + self.ttl}
        return token

    def resolve(self, token: Optional[str]) -> Optional[dict]:
        if not token:
            return None
        with self._lock:
            sess = self._sessions.get(token)
            if not sess or sess["exp"] < time.time():
                self._sessions.pop(token, None)
                return None
            return dict(sess)

    def revoke(self, token: Optional[str]) -> None:
        with self._lock:
            self._sessions.pop(token or "", None)

    def revoke_user(self, user: str) -> None:
        with self._lock:
            for t in [t for t, s in self._sessions.items() if s["user"] == user]:
                del self._sessions[t]

    def _sweep(self) -> None:
        now = time.time()
        for t in [t for t, s in self._sessions.items() if s["exp"] < now]:
            del self._sessions[t]


class AuditLog:
    """审计日志: JSON Lines 追加写; 超过上限时截断保留最近一半 (fail-safe 不抛出)。"""

    MAX_LINES = 10_000

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.getenv("AGENT_AUDIT_FILE") or DEFAULT_AUDIT_FILE
        self._lock = threading.Lock()

    def append(self, user: str, action: str, target: str = "",
               detail: str = "", ok: bool = True) -> None:
        entry = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "user": user or "-",
                 "action": action, "target": target[:200],
                 "detail": str(detail)[:300], "ok": bool(ok)}
        try:
            with self._lock:
                os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                self._rotate()
        except OSError:
            pass   # 审计失败不阻塞业务

    def _rotate(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > self.MAX_LINES:
                with open(self.path, "w", encoding="utf-8") as f:
                    f.writelines(lines[-self.MAX_LINES // 2:])
        except OSError:
            pass

    def tail(self, limit: int = 200) -> List[dict]:
        limit = max(1, min(int(limit), 1000))
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                lines = f.readlines()[-limit:]
            out = []
            for line in lines:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
            return out
        except OSError:
            return []
