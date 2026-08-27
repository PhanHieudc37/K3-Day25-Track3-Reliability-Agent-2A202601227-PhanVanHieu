from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    """Run the grader suite and persist its exact console output as evidence."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--junitxml=reports/test-results.xml"],
        check=False,
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    print(output, end="")
    report_dir = Path("reports")
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "test-output.txt").write_text(output, encoding="utf-8")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
