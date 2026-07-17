"""Repository-local compileall wrapper that leaves the source checkout clean.

The release verification contract compiles source paths and then asserts that the
checkout has no tracked, untracked, or ignored debris. CPython's stdlib
``compileall`` validates syntax by writing bytecode caches. This wrapper keeps
the public stdlib API available while its CLI routes bytecode into a temporary
``PYTHONPYCACHEPREFIX`` equivalent instead of deleting anything from the source
checkout.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import sysconfig
import tempfile
from pathlib import Path
from types import ModuleType


_STDLIB_COMPILEALL = Path(sysconfig.get_path("stdlib")) / "compileall.py"
_STDLIB_MODULE: ModuleType | None = None


def main() -> int:
    original_prefix = sys.pycache_prefix
    original_env = os.environ.get("PYTHONPYCACHEPREFIX")
    with tempfile.TemporaryDirectory(prefix="replyloop-compileall-") as pycache_prefix:
        sys.pycache_prefix = pycache_prefix
        os.environ["PYTHONPYCACHEPREFIX"] = pycache_prefix
        try:
            return 0 if _stdlib_compileall().main() else 1
        finally:
            sys.pycache_prefix = original_prefix
            if original_env is None:
                os.environ.pop("PYTHONPYCACHEPREFIX", None)
            else:
                os.environ["PYTHONPYCACHEPREFIX"] = original_env


def _stdlib_compileall() -> ModuleType:
    global _STDLIB_MODULE
    if _STDLIB_MODULE is None:
        spec = importlib.util.spec_from_file_location("_replyloop_stdlib_compileall", _STDLIB_COMPILEALL)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load stdlib compileall from {_STDLIB_COMPILEALL}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _STDLIB_MODULE = module
    return _STDLIB_MODULE


def __getattr__(name: str) -> object:
    return getattr(_stdlib_compileall(), name)


if __name__ == "__main__":
    raise SystemExit(main())
