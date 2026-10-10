"""Run task-management browser acceptance against a real temporary server."""

import os
import subprocess
import uuid
from pathlib import Path

from relayfence.cli import init_directory
from relayfence.runner import LocalServer


def main():
    if not os.environ.get("RELAYFENCE_PLAYWRIGHT"):
        raise SystemExit("Set RELAYFENCE_PLAYWRIGHT to the installed Playwright Node module path")
    directory = init_directory(Path(".relayfence") / ("tasks-ui-" + uuid.uuid4().hex[:8]))
    token = (directory / "access.token").read_text().strip()
    with LocalServer(directory, token) as server:
        subprocess.run(
            ["node", str(Path(__file__).with_suffix(".cjs").resolve())],
            env={**os.environ, "RELAYFENCE_URL": server.url, "RELAYFENCE_TOKEN": token},
            check=True,
            timeout=180,
        )


if __name__ == "__main__":
    main()
