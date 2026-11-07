#!/usr/bin/env python3
"""PostToolUse-хук: после правки .py в backend/ запускает ruff format и ruff check --fix.

Если остались ошибки, которые ruff не исправил сам, — exit 2: stderr уходит агенту,
и он обязан их исправить до продолжения работы.
"""

import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    file_path = (payload.get("tool_input") or {}).get("file_path", "")
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR", Path.cwd()))
    path = Path(file_path)
    if path.suffix != ".py" or not path.is_file():
        return 0
    backend = project / "backend"
    if backend not in path.resolve().parents:
        return 0
    ruff = project / ".venv" / "bin" / "ruff"
    if not ruff.exists():
        print("ruff_on_edit: .venv/bin/ruff не найден — пропускаю", file=sys.stderr)
        return 0

    subprocess.run([ruff, "format", "--quiet", str(path)], cwd=backend, check=False)
    result = subprocess.run(
        [ruff, "check", "--fix", "--quiet", str(path)],
        cwd=backend,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"ruff нашёл ошибки в {file_path}:\n{result.stdout}{result.stderr}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
