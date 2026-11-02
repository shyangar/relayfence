"""Private project/runtime bindings. A saved identity is never replaced implicitly."""

import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

RUNTIMES = {"codex", "zcode", "dsh", "opencode"}
_IDENTITY = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:/@-]{0,199}$")


def read_binding(path):
    path = Path(path)
    if not path.is_file():
        return None
    if path.stat().st_size > 32768:
        raise ValueError("项目会话绑定文件过大，请检查后重建绑定")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValueError
        if any(not isinstance(value.get(key), str) for key in ("project", "runtime", "workspace")):
            raise ValueError
        if not isinstance(value.get("turns"), int) or value["turns"] < 0:
            raise ValueError
        ident = value.get("session_id")
        if ident is not None and (not isinstance(ident, str) or not _IDENTITY.fullmatch(ident)):
            raise ValueError
        return value
    except (ValueError, UnicodeError) as exc:
        raise ValueError("项目会话绑定损坏，请检查后重建绑定；不会自动丢弃历史") from exc


class ProjectSession:
    def __init__(self, path, project, runtime, workspace):
        self.path = Path(path).resolve()
        self.project, self.runtime = project, runtime
        self.workspace = str(Path(workspace).resolve())
        self.value = read_binding(self.path)
        if self.value and (
            self.value.get("project") != project
            or self.value.get("runtime") != runtime
            or os.path.normcase(self.value.get("workspace", "")) != os.path.normcase(self.workspace)
        ):
            raise ValueError("项目会话与当前项目、软件或目录不匹配，拒绝跨项目续接")
        if self.value and not self.value.get("session_id"):
            raise ValueError("上次创建会话未取得身份，请检查运行日志后重建绑定，避免重复创建")

    @property
    def session_id(self):
        return self.value.get("session_id") if self.value else None

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        pending = self.path.with_name("." + self.path.name + "-" + uuid.uuid4().hex)
        try:
            fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.value, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pending, self.path)
        finally:
            pending.unlink(missing_ok=True)

    def begin(self, task):
        now = time.time()
        if self.value is None:
            self.value = {
                "version": 1,
                "project": self.project,
                "runtime": self.runtime,
                "workspace": self.workspace,
                "session_id": None,
                "created_at": now,
                "turns": 0,
            }
        self.value.update(
            status="running" if self.session_id else "creating",
            updated_at=now,
            last_task=task.get("task_id"),
        )
        self.save()

    def bind(self, ident):
        if not isinstance(ident, str) or not _IDENTITY.fullmatch(ident):
            raise ValueError("运行时没有返回有效的会话身份")
        if self.session_id and self.session_id != ident:
            raise ValueError("运行时返回了其他会话，拒绝替换项目固定会话")
        self.value.update(session_id=ident, status="running", updated_at=time.time())
        self.save()

    def finish(self, success):
        self.value.update(status="ready" if success else "error", updated_at=time.time())
        if success:
            if not self.session_id:
                raise ValueError("执行没有报告持久会话身份，不能确认项目会话已建立")
            self.value["turns"] += 1
        self.save()


def current_session(runtime, envelope):
    path = os.environ.get("RELAYFENCE_SESSION_FILE")
    if not path:
        return None
    if runtime not in RUNTIMES or runtime != os.environ.get("RELAYFENCE_SESSION_RUNTIME"):
        raise ValueError("项目会话运行时不匹配")
    project = os.environ.get("RELAYFENCE_SESSION_PROJECT")
    if not project:
        raise ValueError("项目会话缺少项目身份")
    binding = ProjectSession(path, project, runtime, Path.cwd())
    binding.begin(envelope["task"])
    selection = envelope.get("selection", {})
    binding.value.update(
        model=selection.get("model"), provider=selection.get("parameters", {}).get("provider")
    )
    binding.save()
    return binding


def stream_process(command, prompt, events, binding, dialect, env=None):
    """Capture identity before the first model turn, even if later work fails."""
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None, env=env
    )
    try:
        process.stdin.write(prompt)
        process.stdin.close()
        while line := process.stdout.readline(1_048_577):
            if len(line) > 1_048_576:
                raise ValueError("运行时会话事件过大")
            events.write(line)
            events.flush()
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(event, dict):
                continue
            ident = None
            if dialect == "codex" and event.get("type") == "thread.started":
                ident = event.get("thread_id")
            elif dialect == "dsh" and event.get("type") == "session":
                ident = event.get("sessionId")
            elif dialect == "opencode":
                ident = event.get("sessionID")
            if ident:
                binding.bind(ident)
        return subprocess.CompletedProcess(command, process.wait())
    except BaseException:
        binding.finish(False)
        raise
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        if not process.stdin.closed:
            process.stdin.close()
