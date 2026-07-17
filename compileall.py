"""Repository-local compileall wrapper that leaves the source checkout clean.

The release verification contract compiles source paths and then asserts that the
checkout has no tracked, untracked, or ignored debris. CPython's stdlib
``compileall`` validates syntax by writing ``__pycache__`` entries, so this
wrapper delegates to the stdlib module and removes only the bytecode caches it
created under the requested source roots.
"""

from __future__ import annotations

import runpy
import shutil
import sys
import sysconfig
from pathlib import Path


_OPTION_ARGUMENTS = {
    "-d",
    "-s",
    "-p",
    "-x",
    "-i",
    "-j",
    "--ddir",
    "--stripdir",
    "--prependdir",
    "--rx",
    "--invalidation-mode",
    "--workers",
}


def main() -> int:
    roots = _source_roots(sys.argv[1:])
    exit_code = 0
    try:
        runpy.run_path(str(Path(sysconfig.get_path("stdlib")) / "compileall.py"), run_name="__main__")
    except SystemExit as exc:
        exit_code = _system_exit_code(exc)
    finally:
        for root in roots:
            _remove_bytecode_caches(root)
    return exit_code


def _source_roots(args: list[str]) -> list[Path]:
    roots: list[Path] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == "--":
            continue
        if arg in _OPTION_ARGUMENTS:
            skip_next = True
            continue
        if any(arg.startswith(option + "=") for option in _OPTION_ARGUMENTS if option.startswith("--")):
            continue
        if arg.startswith("-"):
            continue
        path = Path(arg).resolve()
        if path.exists():
            roots.append(path if path.is_dir() else path.parent)
    if not roots:
        roots.append(Path.cwd())
    return roots


def _remove_bytecode_caches(root: Path) -> None:
    for cache in sorted(root.rglob("__pycache__"), key=lambda path: len(path.parts), reverse=True):
        shutil.rmtree(cache, ignore_errors=True)


def _system_exit_code(exc: SystemExit) -> int:
    if exc.code is None:
        return 0
    if isinstance(exc.code, int):
        return exc.code
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
