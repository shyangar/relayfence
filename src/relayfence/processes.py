"""Own an adapter's process group/job for its entire lifetime, including cleanup."""

import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def stop_on_signal(stop):
    """Route service-manager and Ctrl+C termination through normal cleanup."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def request_stop(signum, frame):
        stop.set()

    signals = [signal.SIGTERM, signal.SIGINT]
    if os.name == "nt" and hasattr(signal, "SIGBREAK"):
        signals.append(signal.SIGBREAK)
    previous = {}
    try:
        for sig in signals:
            try:
                previous[sig] = signal.signal(sig, request_stop)
            except (OSError, ValueError):
                continue
        yield
    finally:
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (OSError, ValueError):
                pass


def _kill_process_group(process):
    # Darwin may report EPERM while a fast-exiting leader is still tearing down,
    # including after poll() briefly reports it alive. Reap on every retry, then
    # still signal the group so descendants cannot outlive an exited leader.
    deadline = time.monotonic() + 0.5
    while True:
        process.poll()
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except PermissionError:
            if sys.platform != "darwin" or time.monotonic() >= deadline:
                raise  # A persistent permission failure is not successful cleanup.
            time.sleep(0.01)


@contextmanager
def managed_process(command, *, input_path, cwd, env, stdout, stderr):
    job, process = None, None
    try:
        if os.name == "nt":
            from ._windows_job import WindowsJob

            # Use the real interpreter for the gate; a venv launcher can fork early.
            interpreter = Path(sys.base_prefix) / "python.exe"
            if not interpreter.is_file():
                raise RuntimeError(
                    "Cannot locate the base Python interpreter for process ownership"
                )
            job = WindowsJob()
            process = subprocess.Popen(
                [
                    str(interpreter),
                    str(Path(__file__).with_name("_process_gate.py")),
                    str(input_path),
                    *command,
                ],
                stdin=subprocess.PIPE,
                stdout=stdout,
                stderr=stderr,
                cwd=cwd,
                env=env,
                shell=False,
                close_fds=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            # The gate cannot start the adapter if this fails. Never silently fall
            # back to a PID-only kill, which loses children when a wrapper exits.
            job.assign(process.pid)
            process.stdin.write(b"\x01")
            process.stdin.flush()
        else:
            with input_path.open("rb") as source:
                process = subprocess.Popen(
                    command,
                    stdin=source,
                    stdout=stdout,
                    stderr=stderr,
                    cwd=cwd,
                    env=env,
                    shell=False,
                    close_fds=True,
                    start_new_session=True,
                )
        yield process
    finally:
        try:
            if job is not None:
                job.close()
            elif process is not None:
                _kill_process_group(process)
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()  # Also covers failed job assignment before gate release.
                process.wait(timeout=5)
                if process.stdin is not None:
                    process.stdin.close()
