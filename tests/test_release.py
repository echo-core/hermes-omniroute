"""Offline release gates; Git fixtures use only temporary local repositories."""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_checks", ROOT / "scripts/release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseNotes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in [
            "docs/releases/v0.2.3.md",
            "CHANGELOG.md",
            "README.md",
            "model-provider/plugin.yaml",
            "backend/plugin.yaml",
        ]:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)

    def notes(self, tag="v0.2.3", repo="example/integration"):
        return release.release_notes(self.root, tag, repo)

    def test_curated_notes_keep_features_and_resolve_repository_links(self):
        text = self.notes()
        self.assertIn("Regular and Complex", text)
        self.assertIn(
            "https://github.com/example/integration/blob/v0.2.3/README.md#configure", text
        )
        self.assertIn("https://github.com/example/integration/blob/v0.2.3/CHANGELOG.md", text)
        self.assertNotIn("../../README.md", text)

    def test_unsafe_or_unstable_tags_and_repository_names_are_rejected(self):
        for tag in ["v1.2.3-rc1", "v01.2.3", "../README", "v1.2.3;echo unsafe"]:
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                self.notes(tag=tag)
        with self.assertRaises(ValueError):
            self.notes(repo="../other/repo")

    def test_manifest_change_requires_matching_changelog_and_release_notes(self):
        p = self.root / "backend/plugin.yaml"
        p.write_text(p.read_text().replace("version: 0.2.3", "version: 0.2.4"))
        with self.assertRaisesRegex(ValueError, "version matrix"):
            self.notes()

    def test_versioned_changelog_is_required(self):
        p = self.root / "CHANGELOG.md"
        p.write_text(p.read_text().replace("## v0.2.3", "## Unreleased candidate"))
        with self.assertRaisesRegex(ValueError, "changelog"):
            self.notes()

    def test_draft_notes_and_missing_relative_links_are_rejected(self):
        p = self.root / "docs/releases/v0.2.3.md"
        original = p.read_text()
        p.write_text(original + "\nPublication is pending.\n")
        with self.assertRaisesRegex(ValueError, "Finalize"):
            self.notes()
        p.write_text(original + "\n[missing](missing.md)\n")
        with self.assertRaisesRegex(ValueError, "missing file"):
            self.notes()

    def test_cli_prepares_artifacts_only_after_source_and_existing_release_checks(self):
        env = {"CI_COMMIT_TAG": "v0.2.3", "CI_REPO": "example/integration"}
        missing = urllib.error.HTTPError("https://api.github.com/", 404, "absent", {}, None)
        with (
            patch.dict(os.environ, env),
            patch.object(sys, "argv", ["release.py", "prepare"]),
            patch.object(Path, "cwd", return_value=self.root),
            patch.object(release, "validate_source") as source,
            patch.object(release, "fetch_release", side_effect=missing),
        ):
            release.main()
        source.assert_called_once()
        self.assertEqual((self.root / ".release/title.txt").read_text(), "Hermes OmniRoute v0.2.3")
        self.assertEqual((self.root / ".release/notes.md").read_text(), self.notes())

    def test_public_api_failure_does_not_prepare_publication(self):
        forbidden = urllib.error.HTTPError("https://api.github.com/", 403, "denied", {}, None)
        with (
            patch.dict(os.environ, {"CI_COMMIT_TAG": "v0.2.3", "CI_REPO": "example/integration"}),
            patch.object(sys, "argv", ["release.py", "prepare"]),
            patch.object(Path, "cwd", return_value=self.root),
            patch.object(release, "validate_source"),
            patch.object(release, "fetch_release", side_effect=forbidden),
            self.assertRaises(urllib.error.HTTPError),
        ):
            release.main()
        self.assertFalse((self.root / ".release").exists())

    def test_existing_release_conflicts_fail_without_overwriting(self):
        valid = {
            "tag_name": "v0.2.3",
            "name": "Hermes OmniRoute v0.2.3",
            "body": self.notes(),
            "draft": False,
            "prerelease": False,
        }
        release.verify_release(valid, "v0.2.3", valid["name"], self.notes())
        for key, value in [
            ("tag_name", "v0.2.2"),
            ("name", "other"),
            ("body", "other"),
            ("draft", True),
            ("prerelease", True),
        ]:
            with self.subTest(field=key), self.assertRaises(ValueError):
                release.verify_release({**valid, key: value}, "v0.2.3", valid["name"], self.notes())

    def test_output_directory_symlink_is_rejected(self):
        (self.root / ".release").symlink_to(self.root / "docs", target_is_directory=True)
        missing = urllib.error.HTTPError("https://api.github.com/", 404, "absent", {}, None)
        with (
            patch.dict(os.environ, {"CI_COMMIT_TAG": "v0.2.3", "CI_REPO": "example/integration"}),
            patch.object(sys, "argv", ["release.py", "prepare"]),
            patch.object(Path, "cwd", return_value=self.root),
            patch.object(release, "validate_source"),
            patch.object(release, "fetch_release", side_effect=missing),
            self.assertRaisesRegex(ValueError, "symlink"),
        ):
            release.main()


class ReleaseSource(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "work"
        self.remote = Path(self.temp.name) / "origin.git"
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        subprocess.run(
            ["git", "init", "--initial-branch=main", str(self.root)],
            check=True,
            capture_output=True,
        )
        self.git("config", "user.name", "Release test")
        self.git("config", "user.email", "release-test@example.invalid")
        self.git("remote", "add", "origin", str(self.remote))
        (self.root / "example").write_text("reviewed")
        self.git("add", "example")
        self.git("commit", "-m", "Reviewed fixture")
        self.git("push", "origin", "main")
        self.sha = self.git("rev-parse", "HEAD")

    def git(self, *args):
        return subprocess.check_output(
            ["git", *args], cwd=self.root, stderr=subprocess.DEVNULL, text=True
        ).strip()

    def test_lightweight_and_annotated_tags_on_main_are_accepted(self):
        for tag, annotated in [("v1.0.0", False), ("v1.0.1", True)]:
            if annotated:
                self.git("tag", "-a", tag, "-m", "Reviewed tag")
            else:
                self.git("tag", tag)
            self.git("push", "origin", tag)
            release.validate_source(self.root, tag, self.sha, f"refs/tags/{tag}", "main")

    def test_unmerged_tag_is_rejected(self):
        self.git("checkout", "-b", "unmerged")
        (self.root / "example").write_text("unreviewed")
        self.git("commit", "-am", "Unmerged fixture")
        sha = self.git("rev-parse", "HEAD")
        self.git("tag", "v2.0.0")
        self.git("push", "origin", "v2.0.0")
        with self.assertRaises(subprocess.CalledProcessError):
            release.validate_source(self.root, "v2.0.0", sha, "refs/tags/v2.0.0", "main")

    def test_changed_pipeline_sha_or_remote_tag_is_rejected(self):
        self.git("tag", "v1.0.0")
        self.git("push", "origin", "v1.0.0")
        with self.assertRaisesRegex(ValueError, "Checked-out"):
            release.validate_source(self.root, "v1.0.0", "0" * 40, "refs/tags/v1.0.0", "main")
        (self.root / "example").write_text("new")
        self.git("commit", "-am", "New fixture")
        with self.assertRaisesRegex(ValueError, "remote version tag"):
            release.validate_source(
                self.root, "v1.0.0", self.git("rev-parse", "HEAD"), "refs/tags/v1.0.0", "main"
            )

    def test_non_tag_event_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "version-tag event"):
            release.validate_source(self.root, "v1.0.0", self.sha, "refs/heads/main", "main")
