#!/usr/bin/env python3
"""Privacy and secret audit for public ReplyLoop repository content."""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

SAFE_EXAMPLE_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32")
)

TEXT_SUFFIXES = {
    "",
    ".cfg",
    ".css",
    ".csv",
    ".gitignore",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".rst",
    ".sh",
    ".sql",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

ARTIFACT_PATH_RE = re.compile(
    r"(^|/)(?:"
    r"\.env(?:\..*)?"
    r"|(?:build|dist|logs?|backups?)(?:/|$)"
    r"|.*\.egg-info(?:/|$)"
    r"|.*\.(?:db|sqlite|sqlite3|db-wal|db-shm|sqlite-wal|sqlite-shm|bak|backup|zip|tar|tgz|tar\.gz|whl|log|pem|key|p12|pfx|tmp|swp|orig|rej)$"
    r"|[^/]+\.tar\.gz$"
    r"|(?:auth(?:[._-].*)?|credentials?)(?:/|$)"
    r"|.*(?:[._-]auth(?:[._-].*)?|secret|token|credentials?|private[_-]?key).*)",
    re.IGNORECASE,
)

PATH_PRIVACY_CHECKS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private host name in path", re.compile(r"(?i)\b(?:" + "local" + "host" + r"|[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.(?:internal|local)\b(?![.-]))")),
    (
        "phone number pattern in path",
        re.compile(r"(?<![\w.])(?:\+\d[\d .()\-]{7,}\d|\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4})(?![\w.])"),
    ),
    (
        "chat or sender identifier pattern in path",
        re.compile(r"(?i)(?:\b|['\"])(?:chat|sender)[_-]?id(?:\b|['\"]|[_-])\s*[:=._-]?\s*['\"]?[A-Za-z0-9_-]{6,}"),
    ),
    ("private key name in path", re.compile(r"(?i)(?:^|[._/-])(?:id_rsa|id_dsa|id_ecdsa|id_ed25519|private[_-]?key)(?:[._/-]|$)")),
)

CHECKS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private key marker",
        re.compile(r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY(?: [A-Z0-9 ]{1,20})?-----"),
    ),
    (
        "assigned secret or token value",
        re.compile(
            r"(?i)(?:\b|['\"])[a-z0-9_-]*(?:api[_-]?key|token|secret|password|passwd|credential)[a-z0-9_-]*(?:\b|['\"])"
            r"\s*[:=]\s*['\"]?[^'\"\s]{8,}"
        ),
    ),
    (
        "authorization bearer token",
        re.compile(r"(?i)(?:\b|['\"])authorization(?:\b|['\"])\s*[:=]\s*['\"]?bearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    ),
    ("cloud access key marker", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github token marker", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")),
    ("openai token marker", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("gitlab token marker", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("slack token marker", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("google api key marker", re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b")),
    ("stripe secret key marker", re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{20,}\b")),
    ("npm token marker", re.compile(r"\bnpm_[A-Za-z0-9_-]{20,}\b")),
    ("pypi token marker", re.compile(r"\bpypi-[A-Za-z0-9_-]{20,}\b")),
    ("xai token marker", re.compile(r"\bxai-[A-Za-z0-9_-]{20,}\b")),
    ("age private key marker", re.compile(r"\bAGE-SECRET-KEY-1[A-Z0-9]+\b")),
    (
        "machine-specific absolute path",
        re.compile(
            r"(?i)(?:/(?:Users|home|root)(?:/[A-Za-z0-9._-]+)?\b|[A-Za-z]:\\users\\[A-Za-z0-9._-]+\b|\\\\[A-Za-z0-9._-]+\\[A-Za-z0-9.$_-]+(?:\\|\b))"
        ),
    ),
    ("vault or private config path", re.compile(r"(?i)/(?:private|vault|secrets?)(?:/|\b)")),
    ("private host name", re.compile(r"(?i)\b(?:" + "local" + "host" + r"|[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.(?:internal|local)\b(?![.-]))")),
    (
        "phone number pattern",
        re.compile(r"(?<![\w.])(?:\+\d[\d .()\-]{7,}\d|\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4})(?![\w.])"),
    ),
    (
        "chat or sender identifier pattern",
        re.compile(
            r"(?i)(?:"
            r"(?:\b|['\"])(?:chat|sender)[_-]?id(?:\b|['\"])\s*[:=]\s*['\"]?[A-Za-z0-9_-]{6,}"
            r"|(?:^|\s)--(?:chat|sender)\s+['\"]?[0-9]{6,}['\"]?(?:\s|$)"
            r")"
        ),
    ),
)

PATH_REDACTIONS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bnpm_[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bpypi-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxai-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAGE-SECRET-KEY-1[A-Z0-9]+\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|credential)(?:[=:._-])[^/]+"),
)

SAFE_REDACTION_SUFFIXES = {".cfg", ".csv", ".ini", ".json", ".md", ".py", ".txt", ".yaml", ".yml"}

SENSITIVE_NAME_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|credential)")

IP_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
IPV6_RE = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])")


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int | None
    rule: str

    def format(self, root: Path) -> str:
        rel = self.path.relative_to(root) if self.path.is_relative_to(root) else self.path
        display_path = redact_path(rel.as_posix())
        if self.line is None:
            return f"{display_path}: path: {self.rule}"
        return f"{display_path}:{self.line}: {self.rule}"


def redact_path(value: str) -> str:
    redacted = value
    for pattern in PATH_REDACTIONS:
        redacted = pattern.sub("[REDACTED]", redacted)
    redacted = re.sub(
        r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)",
        lambda match: "[REDACTED]" if is_forbidden_ip(match.group(0)) else match.group(0),
        redacted,
    )
    redacted = IPV6_RE.sub(lambda match: "[REDACTED]" if is_forbidden_ip(match.group(0)) else match.group(0), redacted)
    return "/".join(redact_path_segment(segment) for segment in redacted.split("/"))


def redact_path_segment(segment: str) -> str:
    for _, pattern in PATH_PRIVACY_CHECKS:
        stem = segment.rsplit(".", 1)[0]
        if pattern.search(segment) or pattern.search(stem):
            return "[REDACTED]"
    marker = SENSITIVE_NAME_RE.search(segment)
    if marker is None:
        return segment
    suffix = ""
    dot = segment.rfind(".")
    if dot > marker.end() and segment[dot:].lower() in SAFE_REDACTION_SUFFIXES:
        suffix = segment[dot:]
    return segment[: marker.end()] + "[REDACTED]" + suffix


def run_git(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


def run_git_bytes(root: Path, args: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


def git_files(root: Path) -> list[Path] | None:
    probe = run_git(root, ["rev-parse", "--is-inside-work-tree"])
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return None
    listed = run_git(root, ["ls-files", "--cached", "--others", "--exclude-standard", "-z"])
    if listed.returncode != 0:
        return None
    return [root / item for item in listed.stdout.split("\0") if item]


def walk_files(root: Path) -> list[Path]:
    ignored_dirs = {".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache"}
    files: list[Path] = []
    for current, dirs, names in os.walk(root):
        dirs[:] = [name for name in dirs if name not in ignored_dirs]
        base = Path(current)
        for name in names:
            files.append(base / name)
    return files


def candidate_files(root: Path) -> list[Path]:
    files = git_files(root)
    if files is None:
        files = walk_files(root)
    return sorted(path for path in files if path.is_file() or path.is_symlink())


def looks_text(path: Path) -> bool:
    if path.suffix in TEXT_SUFFIXES or path.name in {"LICENSE", "README", "SECURITY", "CONTRIBUTING"}:
        return True
    try:
        with path.open("rb") as handle:
            chunk = handle.read(4096)
    except OSError:
        return False
    return b"\0" not in chunk


def looks_text_bytes(content: bytes) -> bool:
    return b"\0" not in content[:4096]


def is_safe_example_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return True
    return any(address in network for network in SAFE_EXAMPLE_NETWORKS)


def is_forbidden_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
    ) and not is_safe_example_ip(value)


def scan_path(root: Path, path: Path) -> Iterable[Finding]:
    rel = path.relative_to(root).as_posix()
    if ARTIFACT_PATH_RE.search(rel):
        yield Finding(path, None, "forbidden local artifact or credential-like path")
    yield from scan_path_value(path, rel, "")


def scan_path_value(path: Path, rel: str, suffix: str) -> Iterable[Finding]:
    values = [rel]
    values.extend(segment.rsplit(".", 1)[0] for segment in rel.split("/") if "." in segment)
    for rule, pattern in PATH_PRIVACY_CHECKS:
        if any(pattern.search(value) for value in values):
            yield Finding(path, None, rule + suffix)
    for value in values:
        for match in re.finditer(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)", value):
            if is_forbidden_ip(match.group(0)):
                yield Finding(path, None, "private or loopback IP address in path" + suffix)
                break
        for match in IPV6_RE.finditer(value):
            if is_forbidden_ip(match.group(0)):
                yield Finding(path, None, "private or loopback IP address in path" + suffix)
                break


def scan_text(root: Path, path: Path) -> Iterable[Finding]:
    if path.is_symlink():
        try:
            text = os.readlink(path)
        except OSError:
            return
        lines = [text]
    else:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return
        except OSError:
            return
        lines = text.splitlines()
    yield from scan_lines(path, lines)


def scan_lines(path: Path, lines: Iterable[str]) -> Iterable[Finding]:
    for line_no, line in enumerate(lines, start=1):
        for rule, pattern in CHECKS:
            if pattern.search(line):
                yield Finding(path, line_no, rule)
        for match in IP_RE.finditer(line):
            if is_forbidden_ip(match.group(0)):
                yield Finding(path, line_no, "private or loopback IP address")
        for match in IPV6_RE.finditer(line):
            if is_forbidden_ip(match.group(0)):
                yield Finding(path, line_no, "private or loopback IP address")


def scan_commit_message(root: Path, path: Path, lines: Iterable[str]) -> Iterable[Finding]:
    repo_prefix = root.as_posix().rstrip("/") + "/"
    redacted_lines = (line.replace(repo_prefix, "REPO/") for line in lines)
    yield from scan_lines(path, redacted_lines)


def audit(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in candidate_files(root):
        findings.extend(scan_path(root, path))
        if path.is_symlink() or looks_text(path):
            findings.extend(scan_text(root, path))
    findings.extend(scan_git_history(root))
    return findings


def scan_git_history(root: Path) -> list[Finding]:
    probe = run_git(root, ["rev-parse", "--is-inside-work-tree"])
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return []
    commits = run_git(root, ["rev-list", "--all"])
    if commits.returncode != 0:
        return []
    findings: list[Finding] = []
    for commit in [line.strip() for line in commits.stdout.splitlines() if line.strip()]:
        message = run_git(root, ["show", "-s", "--format=%B", commit])
        if message.returncode == 0:
            message_path = root / ".git-history" / commit[:12] / "COMMIT_MESSAGE"
            findings.extend(scan_commit_message(root, message_path, message.stdout.splitlines()))
        tree = run_git(root, ["ls-tree", "-r", "--name-only", "-z", commit])
        if tree.returncode != 0:
            continue
        for rel in [item for item in tree.stdout.split("\0") if item]:
            history_path = root / ".git-history" / commit[:12] / rel
            findings.extend(scan_history_path(root, history_path, rel))
            blob = run_git_bytes(root, ["cat-file", "-p", f"{commit}:{rel}"])
            if blob.returncode == 0 and looks_text_bytes(blob.stdout):
                text = blob.stdout.decode("utf-8", errors="replace")
                findings.extend(scan_lines(history_path, text.splitlines()))
    return findings


def scan_history_path(root: Path, history_path: Path, rel: str) -> Iterable[Finding]:
    if ARTIFACT_PATH_RE.search(rel):
        yield Finding(history_path, None, "forbidden local artifact or credential-like path in git history")
    yield from scan_path_value(history_path, rel, " in git history")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".", help="repository root to audit")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.exists() or not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2

    findings = audit(root)
    if findings:
        print("public repository audit failed:", file=sys.stderr)
        for finding in findings:
            print(f"  {finding.format(root)}", file=sys.stderr)
        return 1

    print("public repository audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
