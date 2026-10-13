"""Private Windows bootstrap: do not launch the adapter before job assignment.

Run as a script with the base interpreter, not the venv's redirector executable:
the redirector itself may create a child before the runner can assign its job.
Only standard-library imports belong here.
"""

import subprocess
import sys


def main():
    # EOF means the owner disappeared before it could establish process ownership.
    if sys.stdin.buffer.read(1) != b"\x01":
        return 125
    try:
        with open(sys.argv[1], "rb") as source:
            return subprocess.call(sys.argv[2:], stdin=source, shell=False, close_fds=True)
    except OSError:
        print("Unable to start the configured adapter executable", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main())
