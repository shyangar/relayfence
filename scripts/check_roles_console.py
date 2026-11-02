"""Optional browser acceptance check; set RELAYFENCE_PLAYWRIGHT to a Node module path.

RELAYFENCE_BROWSER optionally selects an installed Chromium/Edge executable.
Run from the repository root; screenshots and a private test ledger are retained.
"""

import os
import subprocess
import uuid
from pathlib import Path

from relayfence.cli import init_directory
from relayfence.runner import LocalServer


def main():
    if not os.environ.get("RELAYFENCE_PLAYWRIGHT"):
        raise SystemExit("Set RELAYFENCE_PLAYWRIGHT to your installed Playwright Node module path")
    directory = init_directory(Path(".relayfence") / ("roles-ui-" + uuid.uuid4().hex[:8]))
    token = (directory / "access.token").read_text().strip()
    with LocalServer(directory, token) as server:
        env = {**os.environ, "RELAYFENCE_URL": server.url, "RELAYFENCE_TOKEN": token}
        subprocess.run(
            ["node", str(Path(__file__).with_suffix(".cjs").resolve())],
            env=env,
            check=True,
            timeout=180,
        )


if __name__ == "__main__":
    main()
