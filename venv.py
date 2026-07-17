"""Repository-local compatibility wrapper for ``python -m venv``.

Some minimal Python builds omit ensurepip. The release verification contract still
needs a disposable environment with a ``bin/pip`` command so it can install the
locally built ReplyLoop wheel. This module shadows the stdlib ``venv`` module
when run from the repository root and creates an environment without ensurepip,
then writes a tiny pip shim that delegates to the invoking interpreter's pip with
``--python`` pointed at the disposable environment.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
import sysconfig
from pathlib import Path


def _stdlib_venv_module():
    stdlib = Path(sysconfig.get_path("stdlib")) / "venv" / "__init__.py"
    spec = importlib.util.spec_from_file_location("_replyloop_stdlib_venv", stdlib)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load stdlib venv module from {stdlib}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_pip_shim(env_dir: Path) -> None:
    bindir = "Scripts" if os.name == "nt" else "bin"
    scripts = env_dir / bindir
    scripts.mkdir(parents=True, exist_ok=True)
    python_exe = scripts / ("python.exe" if os.name == "nt" else "python")
    pip_exe = scripts / ("pip.exe" if os.name == "nt" else "pip")
    if os.name == "nt":
        pip_exe.with_suffix(".cmd").write_text(
            f'@echo off\r\n"{sys.executable}" -m pip --python "{python_exe}" %*\r\n',
            encoding="utf-8",
        )
        return
    pip_exe.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" -m pip --python "{python_exe}" "$@"\n',
        encoding="utf-8",
    )
    pip_exe.chmod(pip_exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: python -m venv ENV_DIR [ENV_DIR ...]", file=sys.stderr)
        return 2

    module = _stdlib_venv_module()
    parser = module.EnvBuilder(with_pip=False)
    for arg in args:
        if arg.startswith("-"):
            print(f"unsupported venv option in release shim: {arg}", file=sys.stderr)
            return 2
        env_dir = Path(arg)
        parser.create(env_dir)
        _write_pip_shim(env_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
