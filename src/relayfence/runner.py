"""Managed local server process for executable examples and integration tests."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx


class LocalServer:
    def __init__(self, directory: Path, token: str):
        self.directory = directory.resolve()
        self.token = token
        self.process = None
        self.log = None
        self.url = ""

    def start(self):
        if self.process is not None:
            raise RuntimeError("Server is already started")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.log = (self.directory / "server.log").open("ab")
        env = {**os.environ, "RELAYFENCE_TOKEN": self.token}
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "relayfence",
                "serve",
                "--directory",
                str(self.directory),
                "--port",
                str(port),
            ],
            stdout=self.log,
            stderr=self.log,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        deadline = time.monotonic() + 30
        with httpx.Client(trust_env=False, timeout=0.5) as client:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    self.stop()
                    raise RuntimeError(f"Server exited; inspect {self.directory / 'server.log'}")
                try:
                    response = client.get(
                        self.url + "/v1/schema", headers={"Authorization": "Bearer " + self.token}
                    )
                    if response.status_code == 200 and "task_claim" in response.json():
                        return self
                except (httpx.HTTPError, ValueError):
                    pass
                time.sleep(0.05)
        self.stop()
        raise RuntimeError("Local server did not become ready within 30 seconds")

    def stop(self, kill=False):
        if self.process is not None:
            if self.process.poll() is None:
                if kill:
                    self.process.kill()
                else:
                    self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=10)
            self.process = None
        if self.log:
            self.log.close()
            self.log = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.stop()
