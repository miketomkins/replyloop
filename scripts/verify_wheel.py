#!/usr/bin/env python3
"""Verify ReplyLoop wheel release contents without third-party packages."""

from __future__ import annotations

import argparse
import configparser
from email.parser import Parser
from pathlib import Path
import sys
from typing import Any, cast
import zipfile

EXPECTED_NAME = "replyloop"
EXPECTED_VERSION = "0.1.0"
EXPECTED_MIGRATIONS = {
    "replyloop/migrations/001_initial.sql",
    "replyloop/migrations/002_delivery_claim_ids.sql",
    "replyloop/migrations/003_logical_delivery_identity.sql",
}
FORBIDDEN_PREFIXES = ("tests/", "build/", "dist/")
FORBIDDEN_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
FORBIDDEN_SUFFIXES = (".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".log")


class VerificationError(Exception):
    pass


def verify_wheel(path: Path) -> None:
    if not path.is_file():
        raise VerificationError(f"wheel not found: {path}")
    if path.suffix != ".whl":
        raise VerificationError(f"not a wheel: {path}")

    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            name_set = set(names)
            _verify_forbidden_entries(names)
            metadata = _read_single_text(archive, names, ".dist-info/METADATA")
            entry_points = _read_single_text(archive, names, ".dist-info/entry_points.txt")
    except zipfile.BadZipFile as exc:
        raise VerificationError(f"invalid wheel zip: {exc}") from exc

    _verify_metadata(metadata)
    _verify_entry_points(entry_points)
    missing_migrations = sorted(EXPECTED_MIGRATIONS - name_set)
    if missing_migrations:
        raise VerificationError("missing migrations: " + ", ".join(missing_migrations))


def _read_single_text(archive: zipfile.ZipFile, names: list[str], suffix: str) -> str:
    matches = [name for name in names if name.endswith(suffix)]
    if len(matches) != 1:
        raise VerificationError(f"expected exactly one {suffix}, found {len(matches)}")
    return archive.read(matches[0]).decode("utf-8")


def _verify_metadata(text: str) -> None:
    metadata = Parser().parsestr(text)
    if metadata.get("Name") != EXPECTED_NAME:
        raise VerificationError(f"unexpected package name: {metadata.get('Name')!r}")
    if metadata.get("Version") != EXPECTED_VERSION:
        raise VerificationError(f"unexpected version: {metadata.get('Version')!r}")
    requires = metadata.get_all("Requires-Dist", [])
    if requires:
        raise VerificationError("runtime dependencies are not allowed: " + ", ".join(requires))


def _verify_entry_points(text: str) -> None:
    parser = configparser.ConfigParser()
    parser.optionxform = cast(Any, str)
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        raise VerificationError(f"invalid entry_points.txt: {exc}") from exc
    expected = {
        ("console_scripts", "replyloop"): "replyloop.cli:main",
        ("hermes_agent.plugins", "replyloop"): "replyloop.hermes_plugin",
    }
    for (section, key), value in expected.items():
        if not parser.has_section(section):
            raise VerificationError(f"missing entry point section: {section}")
        if parser.get(section, key, fallback=None) != value:
            raise VerificationError(f"missing entry point {section}.{key} = {value}")


def _verify_forbidden_entries(names: list[str]) -> None:
    bad: list[str] = []
    for name in names:
        path = Path(name)
        if name.startswith(FORBIDDEN_PREFIXES):
            bad.append(name)
        elif any(part in FORBIDDEN_PARTS for part in path.parts):
            bad.append(name)
        elif name.endswith(FORBIDDEN_SUFFIXES):
            bad.append(name)
        elif name.endswith(".egg-info") or ".egg-info/" in name:
            bad.append(name)
    if bad:
        raise VerificationError("forbidden wheel entries: " + ", ".join(sorted(bad)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify ReplyLoop wheel release contents")
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args(argv)
    try:
        verify_wheel(args.wheel)
    except VerificationError as exc:
        print(f"wheel verification failed: {exc}", file=sys.stderr)
        return 1
    print(f"wheel verified: {args.wheel.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
