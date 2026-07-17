from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "public_repo_audit.py"


def run_audit(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


class PublicRepoAuditTests(unittest.TestCase):
    def test_clean_non_git_repository_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text(
                "Synthetic examples may use example.com and 192.0.2.10.\n",
                encoding="utf-8",
            )

            result = run_audit(root)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("passed", result.stdout)

    def test_git_repository_scans_untracked_and_tracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(git(root, "init").returncode, 0)
            (root / "README.md").write_text("clean public baseline\n", encoding="utf-8")
            self.assertEqual(git(root, "add", "README.md").returncode, 0)
            (root / "notes.md").write_text(
                "path=" + "/".join(["", "home", "hermes", "project"]) + "\n",
                encoding="utf-8",
            )

            result = run_audit(root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("notes.md:1", result.stderr)
            self.assertIn("machine-specific absolute path", result.stderr)
            self.assertNotIn("project", result.stderr)

    def test_forbidden_content_is_reported_without_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret_name = "api" + "_" + "key"
            secret_value = "A" * 12 + "B" * 12
            phone = "+" + "1" + " " + "202" + " " + "555" + " " + "0188"
            private_ip = ".".join(["10", "1", "2", "3"])
            (root / "bad.md").write_text(
                f"{secret_name} = {secret_value}\ncall {phone}\nconnect {private_ip}\n",
                encoding="utf-8",
            )

            result = run_audit(root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("assigned secret or token value", result.stderr)
            self.assertIn("phone number pattern", result.stderr)
            self.assertIn("private or loopback IP address", result.stderr)
            self.assertNotIn(secret_value, result.stderr)
            self.assertNotIn(phone, result.stderr)
            self.assertNotIn(private_ip, result.stderr)

    def test_forbidden_artifact_path_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "state.db").write_bytes(b"sqlite bytes")

            result = run_audit(root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("state.db: path", result.stderr)
            self.assertIn("forbidden local artifact", result.stderr)

    def test_chat_and_sender_identifier_patterns_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chat = "chat" + "_" + "id"
            sender = "sender" + "_" + "id"
            (root / "ids.txt").write_text(
                f"{chat}=syntheticButForbidden\n{sender}: anotherSyntheticValue\n",
                encoding="utf-8",
            )

            result = run_audit(root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("ids.txt:1", result.stderr)
            self.assertIn("ids.txt:2", result.stderr)
            self.assertIn("chat or sender identifier pattern", result.stderr)

    def test_quoted_json_keys_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret_name = "api" + "_" + "key"
            chat = "chat" + "_" + "id"
            (root / "fixture.json").write_text(
                "{"
                + f'"{secret_name}": "'
                + ("S" * 24)
                + '", '
                + f'"{chat}": "syntheticTarget"'
                + "}\n",
                encoding="utf-8",
            )

            result = run_audit(root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("fixture.json:1", result.stderr)
            self.assertIn("assigned secret or token value", result.stderr)
            self.assertIn("chat or sender identifier pattern", result.stderr)

    def test_auth_filename_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config"
            config.mkdir()
            (root / ("auth" + ".json")).write_text("{}\n", encoding="utf-8")
            (config / ("auth" + ".json")).write_text("{}\n", encoding="utf-8")

            result = run_audit(root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("auth.json: path", result.stderr)
            self.assertIn("config/auth.json: path", result.stderr)
            self.assertIn("forbidden local artifact", result.stderr)

    def test_authorization_bearer_token_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = "Authorization" + ": " + "Bearer" + " " + "abcde12345fghij67890"
            (root / "headers.txt").write_text(header + "\n", encoding="utf-8")

            result = run_audit(root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("headers.txt:1", result.stderr)
            self.assertIn("authorization bearer token", result.stderr)
            self.assertNotIn("abcde12345fghij67890", result.stderr)

    def test_quoted_authorization_bearer_token_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = "Authorization"
            value = "Bearer" + " " + "quoted12345token67890"
            (root / "headers.json").write_text(
                "{" + f'"{key}": "{value}"' + "}\n",
                encoding="utf-8",
            )

            result = run_audit(root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("headers.json:1", result.stderr)
            self.assertIn("authorization bearer token", result.stderr)
            self.assertNotIn("quoted12345token67890", result.stderr)

    def test_common_token_assignment_names_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            names = [
                "bot" + "_" + "token",
                "auth" + "_" + "token",
                "client" + "_" + "secret",
                "private" + "_" + "token",
            ]
            (root / "assignments.txt").write_text(
                "\n".join(f"{name}=syntheticForbiddenValue" for name in names) + "\n",
                encoding="utf-8",
            )

            result = run_audit(root)

            self.assertNotEqual(result.returncode, 0)
            for line_no in range(1, len(names) + 1):
                self.assertIn(f"assignments.txt:{line_no}", result.stderr)
            self.assertIn("assigned secret or token value", result.stderr)

    def test_windows_path_and_domestic_phone_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            windows_path = "C:" + "\\" + "Users" + "\\" + "publicperson" + "\\" + "AppData"
            phone = "(" + "202" + ") " + "555" + "-" + "0188"
            (root / "private.txt").write_text(
                f"path={windows_path}\nphone={phone}\n",
                encoding="utf-8",
            )

            result = run_audit(root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("private.txt:1", result.stderr)
            self.assertIn("machine-specific absolute path", result.stderr)
            self.assertIn("private.txt:2", result.stderr)
            self.assertIn("phone number pattern", result.stderr)
            self.assertNotIn(windows_path, result.stderr)
            self.assertNotIn(phone, result.stderr)

    def test_lowercase_windows_root_path_and_unformatted_phone_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            windows_path = "c:" + "\\" + "users" + "\\" + "publicperson" + "\\" + "AppData"
            root_path = "/" + "root" + "/" + ".config" + "/" + "tool"
            phone = "202" + "555" + "0188"
            (root / "private.txt").write_text(
                f"path={windows_path}\npath={root_path}\nphone={phone}\n",
                encoding="utf-8",
            )

            result = run_audit(root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("private.txt:1", result.stderr)
            self.assertIn("private.txt:2", result.stderr)
            self.assertIn("machine-specific absolute path", result.stderr)
            self.assertIn("private.txt:3", result.stderr)
            self.assertIn("phone number pattern", result.stderr)
            self.assertNotIn(windows_path, result.stderr)
            self.assertNotIn(root_path, result.stderr)
            self.assertNotIn(phone, result.stderr)

    def test_raw_common_provider_tokens_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            openai_token = "sk-" + "A" * 24
            gitlab_token = "glpat-" + "B" * 24
            (root / "tokens.txt").write_text(
                f"{openai_token}\n{gitlab_token}\n",
                encoding="utf-8",
            )

            result = run_audit(root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("token[REDACTED].txt:1", result.stderr)
            self.assertIn("openai token marker", result.stderr)
            self.assertIn("token[REDACTED].txt:2", result.stderr)
            self.assertIn("gitlab token marker", result.stderr)
            self.assertNotIn(openai_token, result.stderr)
            self.assertNotIn(gitlab_token, result.stderr)

    def test_tracked_symlink_target_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(git(root, "init").returncode, 0)
            target = "/".join(["", "home", "privateuser", "replyloop"])
            (root / "private-link").symlink_to(target)
            self.assertEqual(git(root, "add", "private-link").returncode, 0)

            result = run_audit(root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("private-link:1", result.stderr)
            self.assertIn("machine-specific absolute path", result.stderr)
            self.assertNotIn(target, result.stderr)

    def test_tracked_symlink_with_binary_suffix_target_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(git(root, "init").returncode, 0)
            target = "/".join(["", "home", "privateuser", "replyloop"])
            (root / "private.bin").symlink_to(target)
            self.assertEqual(git(root, "add", "private.bin").returncode, 0)

            result = run_audit(root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("private.bin:1", result.stderr)
            self.assertIn("machine-specific absolute path", result.stderr)
            self.assertNotIn(target, result.stderr)

    def test_pgp_private_key_block_header_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key_header = "-----BEGIN PGP PRIVATE KEY" + " BLOCK-----"
            (root / "key.txt").write_text(key_header + "\n", encoding="utf-8")

            result = run_audit(root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("key.txt:1", result.stderr)
            self.assertIn("private key marker", result.stderr)
            self.assertNotIn(key_header, result.stderr)

    def test_sensitive_filename_is_reported_without_token_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = "ghp_" + "A" * 24
            (root / ("backup-" + token + ".log")).write_text("safe\n", encoding="utf-8")

            result = run_audit(root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("path", result.stderr)
            self.assertIn("forbidden local artifact", result.stderr)
            self.assertNotIn(token, result.stderr)

    def test_generic_credential_filename_is_reported_without_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = "candidate" + "Value" + "123456"
            (root / ("api" + "_" + "key=" + candidate + ".log")).write_text("safe\n", encoding="utf-8")

            result = run_audit(root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("path", result.stderr)
            self.assertIn("forbidden local artifact", result.stderr)
            self.assertNotIn(candidate, result.stderr)

    def test_backup_archive_path_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "backup.zip").write_bytes(b"archive bytes")

            result = run_audit(root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("backup.zip: path", result.stderr)
            self.assertIn("forbidden local artifact", result.stderr)

    def test_documentation_ipv6_example_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            example_ipv6 = "2001" + ":" + "db8" + ":" + ":" + "10"
            (root / "network.md").write_text(
                "Document with " + example_ipv6 + " as a reserved example.\n",
                encoding="utf-8",
            )

            result = run_audit(root)

            self.assertEqual(result.returncode, 0, result.stderr)

    def test_ipv6_loopback_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            loopback = ":" + ":" + "1"
            (root / "network.txt").write_text("bind " + loopback + "\n", encoding="utf-8")

            result = run_audit(root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("network.txt:1", result.stderr)
            self.assertIn("private or loopback IP address", result.stderr)
            self.assertNotIn(loopback, result.stderr)

    def test_credential_filename_without_separator_is_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = "candidate" + "Value" + "123456"
            parent = root / "config"
            parent.mkdir()
            (parent / ("secret" + candidate + ".txt")).write_text("safe\n", encoding="utf-8")

            result = run_audit(root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("config/" + "secret" + "[REDACTED].txt: path", result.stderr)
            self.assertIn("forbidden local artifact", result.stderr)
            self.assertNotIn(candidate, result.stderr)

    def test_dotted_credential_filename_without_safe_extension_is_fully_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = "candidate" + "." + "signaturePart123456"
            parent = root / "config"
            parent.mkdir()
            (parent / ("secret" + candidate)).write_text("safe\n", encoding="utf-8")

            result = run_audit(root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("config/" + "secret" + "[REDACTED]: path", result.stderr)
            self.assertIn("forbidden local artifact", result.stderr)
            self.assertNotIn(candidate, result.stderr)
            self.assertNotIn("signaturePart123456", result.stderr)

    def test_raw_common_cloud_package_and_ai_tokens_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tokens = [
                "AI" + "za" + "A" * 32,
                "sk" + "_" + "live" + "_" + "B" * 32,
                "sk" + "_" + "test" + "_" + "C" * 32,
                "npm" + "_" + "D" * 32,
                "pypi" + "-" + "E" * 32,
                "xai" + "-" + "F" * 32,
            ]
            (root / "samples.txt").write_text("\n".join(tokens) + "\n", encoding="utf-8")

            result = run_audit(root)

            self.assertNotEqual(result.returncode, 0)
            for line_no in range(1, len(tokens) + 1):
                self.assertIn(f"samples.txt:{line_no}", result.stderr)
            self.assertIn("google api key marker", result.stderr)
            self.assertIn("stripe secret key marker", result.stderr)
            self.assertIn("npm token marker", result.stderr)
            self.assertIn("pypi token marker", result.stderr)
            self.assertIn("xai token marker", result.stderr)
            for token in tokens:
                self.assertNotIn(token, result.stderr)

    def test_common_token_near_misses_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            near_misses = [
                "AI" + "zb" + "A" * 32,
                "sk" + "_" + "demo" + "_" + "B" * 32,
                "npx" + "_" + "D" * 32,
                "pypi" + ":" + "E" * 32,
                "xai" + ":" + "F" * 32,
            ]
            (root / "near-misses.txt").write_text("\n".join(near_misses) + "\n", encoding="utf-8")

            result = run_audit(root)

            self.assertEqual(result.returncode, 0, result.stderr)

    def test_windows_unc_absolute_path_is_reported_without_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unc_path = "\\" + "\\" + "private-host" + "\\" + "share" + "\\" + "config"
            (root / "paths.txt").write_text("path=" + unc_path + "\n", encoding="utf-8")

            result = run_audit(root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("paths.txt:1", result.stderr)
            self.assertIn("machine-specific absolute path", result.stderr)
            self.assertNotIn(unc_path, result.stderr)

    def test_age_private_key_marker_is_reported_without_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = "AGE" + "-" + "SECRET" + "-" + "KEY" + "-" + "1" + "G" * 48
            (root / "age.txt").write_text(key + "\n", encoding="utf-8")

            result = run_audit(root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("age.txt:1", result.stderr)
            self.assertIn("age private key marker", result.stderr)
            self.assertNotIn(key, result.stderr)


if __name__ == "__main__":
    unittest.main()
