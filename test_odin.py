#!/usr/bin/env python3
import subprocess
import sys
from pathlib import Path


if __name__ == "__main__":
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"],
        cwd=Path(__file__).parent,
    )
    raise SystemExit(result.returncode)