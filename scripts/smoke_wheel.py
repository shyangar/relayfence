"""Install a wheel in an isolated environment and run the real protocol demo.

Run with: uv run python scripts/smoke_wheel.py dist/relayfence-0.1.0-py3-none-any.whl
Requires uv on PATH. Nothing is installed into the checkout or global Python.
"""

import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    wheel = Path(sys.argv[1]).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="relayfence-wheel-") as temporary:
        directory = Path(temporary).resolve()
        assert directory.is_relative_to(Path(tempfile.gettempdir()).resolve())
        environment = directory / "environment"
        subprocess.run(["uv", "venv", "--python", sys.executable, str(environment)], check=True)
        python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        subprocess.run(["uv", "pip", "install", "--python", str(python), str(wheel)], check=True)
        subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                "from importlib.resources import files; "
                "assert files('relayfence').joinpath('schema.sql').is_file(); "
                "assert files('relayfence').joinpath('dashboard.html').is_file(); "
                "from relayfence import __version__; print('Installed wheel:', __version__)",
            ],
            cwd=directory,
            check=True,
        )
        subprocess.run(
            [str(python), "-I", "-m", "relayfence", "demo", "--directory", str(directory / "demo")],
            cwd=directory,
            check=True,
        )
        print("Wheel installed and all protocol acceptance checks passed outside the source tree.")


if __name__ == "__main__":
    main()
