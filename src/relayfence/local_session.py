"""One-time local browser pairing; long-lived admin credentials never enter HTML."""

import secrets
import threading
import time
from http.cookies import CookieError, SimpleCookie


class BrowserSessions:
    cookie_name = "relayfence_local"

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.code = secrets.token_urlsafe(32)
        self.code_expires = clock() + 600
        self.sessions = {}

    def pair(self, code):
        with self.lock:
            if (
                not isinstance(code, str)
                or not code.isascii()
                or not self.code
                or self.clock() >= self.code_expires
                or not secrets.compare_digest(code, self.code)
            ):
                return None
            self.code = None
            session = secrets.token_urlsafe(32)
            self.sessions[session] = self.clock() + 8 * 3600
            return session

    def valid(self, header):
        try:
            cookie = SimpleCookie()
            cookie.load(header)
            value = cookie.get(self.cookie_name)
            if value is None:
                return False
            with self.lock:
                now = self.clock()
                self.sessions = {key: end for key, end in self.sessions.items() if end > now}
                return value.value in self.sessions
        except (CookieError, ValueError):
            return False


def browse_directories(path=None):
    """List directories only for an authenticated local project selector."""
    import os
    from pathlib import Path

    if path is None or path == "":
        roots = (
            [
                Path(f"{letter}:/")
                for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                if Path(f"{letter}:/").is_dir()
            ]
            if os.name == "nt"
            else [Path("/")]
        )
        return {
            "path": "",
            "parent": None,
            "directories": [{"name": str(p), "path": str(p)} for p in roots],
            "truncated": False,
        }
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise ValueError("请选择绝对路径的文件夹")
    folder = Path(path).resolve(strict=True)
    if not folder.is_dir():
        raise ValueError("所选路径不是文件夹")
    entries = []
    with os.scandir(folder) as scan:
        for item in scan:
            try:
                if item.is_dir(follow_symlinks=False):
                    entries.append({"name": item.name, "path": str(folder / item.name)})
            except OSError:
                continue
            if len(entries) > 500:
                break
    entries.sort(key=lambda item: item["name"].casefold())
    return {
        "path": str(folder),
        "parent": str(folder.parent) if folder.parent != folder else "",
        "directories": entries[:500],
        "truncated": len(entries) > 500,
    }


_PICK_LOCK = threading.Lock()


def pick_directory():
    """Run the native folder picker outside the web server's UI/event threads."""
    if not _PICK_LOCK.acquire(blocking=False):
        raise ValueError("文件夹选择器已在运行，请先完成当前选择。")
    try:
        return _pick_directory()
    finally:
        _PICK_LOCK.release()


def _pick_directory():
    import subprocess
    import sys

    program = (
        "import tkinter as tk; from tkinter import filedialog; "
        "r=tk.Tk(); r.withdraw(); r.attributes('-topmost', True); "
        "p=filedialog.askdirectory(title='RelayFence - Select local project', mustexist=True); "
        "r.destroy(); print(p)"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            timeout=120,
            encoding="utf-8",
            env=__import__("os").environ | {"PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("文件夹选择已超时，请重试或粘贴本地项目的绝对路径。") from exc
    if result.returncode:
        raise ValueError("无法打开系统文件夹选择器，请粘贴本地项目的绝对路径。")
    return result.stdout.strip()
