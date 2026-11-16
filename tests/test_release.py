"""Offline release gates; Git fixtures use only temporary local repositories."""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

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
            "docs/releases/v0.1.0.md",
            "CHANGELOG.md",
            "README.md",
            "model-provider/plugin.yaml",
            "backend/plugin.yaml",
        ]:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)

    def notes(self, tag="v0.1.0", repo="example/integration"):
        return release.release_notes(self.root, tag, repo)

    def test_curated_notes_keep_features_and_resolve_repository_links(self):
        text = self.notes()
        self.assertIn("Regular and Complex", text)
        self.assertIn(
            "https://github.com/example/integration/blob/v0.1.0/README.md#configure", text
        )
        self.assertIn("https://github.com/example/integration/blob/v0.1.0/CHANGELOG.md", text)
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
        p.write_text(p.read_text().replace("## v0.1.0", "## Unreleased candidate"))
        with self.assertRaisesRegex(ValueError, "changelog"):
            self.notes()

    def test_draft_notes_and_missing_relative_links_are_rejected(self):
        p = self.root / "docs/releases/v0.1.0.md"
        original = p.read_text()
        p.write_text(original + "\nPublication is pending.\n")
        with self.assertRaisesRegex(ValueError, "Finalize"):
            self.notes()
        p.write_text(original + "\n[missing](missing.md)\n")
        with self.assertRaisesRegex(ValueError, "missing file"):
            self.notes()

    def test_cli_prepares_artifacts_only_after_source_and_existing_release_checks(self):
        env = {"CI_COMMIT_TAG": "v0.1.0", "CI_REPO": "example/integration"}
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
        self.assertEqual((self.root / ".release/title.txt").read_text(), "Hermes OmniRoute v0.1.0")
        self.assertEqual((self.root / ".release/notes.md").read_text(), self.notes())

    def test_public_api_failure_does_not_prepare_publication(self):
        forbidden = urllib.error.HTTPError("https://api.github.com/", 403, "denied", {}, None)
        with (
            patch.dict(os.environ, {"CI_COMMIT_TAG": "v0.1.0", "CI_REPO": "example/integration"}),
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
            "tag_name": "v0.1.0",
            "name": "Hermes OmniRoute v0.1.0",
            "body": self.notes(),
            "draft": False,
            "prerelease": False,
        }
        release.verify_release(valid, "v0.1.0", valid["name"], self.notes())
        for key, value in [
            ("tag_name", "v0.2.2"),
            ("name", "other"),
            ("body", "other"),
            ("draft", True),
            ("prerelease", True),
        ]:
            with self.subTest(field=key), self.assertRaises(ValueError):
                release.verify_release({**valid, key: value}, "v0.1.0", valid["name"], self.notes())

    def test_output_directory_symlink_is_rejected(self):
        (self.root / ".release").symlink_to(self.root / "docs", target_is_directory=True)
        missing = urllib.error.HTTPError("https://api.github.com/", 404, "absent", {}, None)
        with (
            patch.dict(os.environ, {"CI_COMMIT_TAG": "v0.1.0", "CI_REPO": "example/integration"}),
            patch.object(sys, "argv", ["release.py", "prepare"]),
            patch.object(Path, "cwd", return_value=self.root),
            patch.object(release, "validate_source"),
            patch.object(release, "fetch_release", side_effect=missing),
            self.assertRaisesRegex(ValueError, "symlink"),
        ):
            release.main()

    def test_manual_prepare_requires_manual_event_and_never_creates_a_tag(self):
        env = {"RELEASE_VERSION": "v0.1.0", "CI_REPO": "example/integration"}
        with (
            patch.dict(os.environ, {**env, "CI_PIPELINE_EVENT": "push"}),
            patch.object(sys, "argv", ["release.py", "create-tag"]),
            patch.object(Path, "cwd", return_value=self.root),
            patch.object(release, "request_tag") as write,
            self.assertRaisesRegex(ValueError, "explicit manual"),
        ):
            release.main()
        write.assert_not_called()
        with (
            patch.dict(os.environ, {**env, "CI_PIPELINE_EVENT": "manual"}),
            patch.object(sys, "argv", ["release.py", "prepare-manual"]),
            patch.object(Path, "cwd", return_value=self.root),
            patch.object(release, "validate_manual_source"),
            patch.object(release, "check_existing_release"),
            patch.object(release, "request_tag") as write,
        ):
            release.main()
        write.assert_not_called()

    def test_manual_create_rechecks_source_and_release_conflicts_before_writing(self):
        env = {
            "RELEASE_VERSION": "v0.1.0",
            "CI_REPO": "example/integration",
            "CI_PIPELINE_EVENT": "manual",
        }
        for gate in ["validate_manual_source", "check_existing_release"]:
            with (
                self.subTest(gate=gate),
                patch.dict(os.environ, env),
                patch.object(sys, "argv", ["release.py", "create-tag"]),
                patch.object(Path, "cwd", return_value=self.root),
                patch.object(release, "validate_manual_source"),
                patch.object(release, "check_existing_release"),
                patch.object(release, gate, side_effect=ValueError("gate refused")),
                patch.object(release, "request_tag") as write,
                self.assertRaisesRegex(ValueError, "gate refused"),
            ):
                release.main()
            write.assert_not_called()

    def test_bundle_version_is_selected_from_changelog_not_environment(self):
        self.assertEqual(release.planned_release(self.root), "v0.1.0")
        p = self.root / "CHANGELOG.md"
        p.write_text(p.read_text().replace("## v0.1.0", "## v0.1.0-rc1"))
        with self.assertRaisesRegex(ValueError, "stable versioned"):
            release.planned_release(self.root)

    def test_promotion_requires_dev_to_main_and_never_writes(self):
        env = {
            "CI_REPO": "example/integration",
            "CI_PIPELINE_EVENT": "pull_request",
            "CI_COMMIT_SOURCE_BRANCH": "dev",
            "CI_COMMIT_TARGET_BRANCH": "main",
            "CI_REPO_DEFAULT_BRANCH": "main",
        }
        for change in [
            {"CI_PIPELINE_EVENT": "push"},
            {"CI_COMMIT_SOURCE_BRANCH": "feature"},
            {"CI_COMMIT_TARGET_BRANCH": "dev"},
        ]:
            with (
                self.subTest(change=change),
                patch.dict(os.environ, {**env, **change}, clear=True),
                patch.object(sys, "argv", ["release.py", "check-promotion"]),
                patch.object(Path, "cwd", return_value=self.root),
                patch.object(release, "request_tag") as write,
                self.assertRaisesRegex(ValueError, "from dev"),
            ):
                release.main()
            write.assert_not_called()
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(sys, "argv", ["release.py", "check-promotion"]),
            patch.object(Path, "cwd", return_value=self.root),
            patch.object(release, "validate_new_release") as version,
            patch.object(release, "check_existing_release"),
            patch.object(release, "request_tag") as write,
        ):
            release.main()
        version.assert_called_once_with(self.root, "v0.1.0")
        write.assert_not_called()

    def test_auto_release_rejects_other_events_before_write(self):
        for event in ["manual", "pull_request", "tag"]:
            with (
                self.subTest(event=event),
                patch.dict(
                    os.environ,
                    {"CI_PIPELINE_EVENT": event, "CI_REPO": "example/integration"},
                    clear=True,
                ),
                patch.object(sys, "argv", ["release.py", "create-auto-tag"]),
                patch.object(Path, "cwd", return_value=self.root),
                patch.object(release, "request_tag") as write,
                self.assertRaisesRegex(ValueError, "default-branch push"),
            ):
                release.main()
            write.assert_not_called()

    def test_auto_prepare_and_create_use_current_main_and_planned_version(self):
        sha = "a" * 40
        env = {
            "CI_REPO": "example/integration",
            "CI_PIPELINE_EVENT": "push",
            "CI_COMMIT_SHA": sha,
            "CI_COMMIT_REF": "refs/heads/main",
            "CI_REPO_DEFAULT_BRANCH": "main",
            "RELEASE_VERSION": "v9.9.9",
        }
        for action in ["prepare-auto", "create-auto-tag"]:
            with (
                self.subTest(action=action),
                patch.dict(os.environ, env, clear=True),
                patch.object(sys, "argv", ["release.py", action]),
                patch.object(Path, "cwd", return_value=self.root),
                patch.object(release, "validate_manual_source") as source,
                patch.object(release, "validate_new_release") as version,
                patch.object(release, "check_existing_release"),
                patch.object(release, "request_tag") as write,
            ):
                release.main()
            source.assert_called_once_with(self.root, "v0.1.0", sha, "refs/heads/main", "main")
            version.assert_called_once_with(self.root, "v0.1.0", sha)
            if action == "create-auto-tag":
                write.assert_called_once_with(self.root, "example/integration", "v0.1.0", sha)
            else:
                write.assert_not_called()

    def test_auto_source_version_and_existing_release_failures_never_write(self):
        env = {"CI_REPO": "example/integration", "CI_PIPELINE_EVENT": "push"}
        for gate in ["validate_manual_source", "validate_new_release", "check_existing_release"]:
            with (
                self.subTest(gate=gate),
                patch.dict(os.environ, env, clear=True),
                patch.object(sys, "argv", ["release.py", "create-auto-tag"]),
                patch.object(Path, "cwd", return_value=self.root),
                patch.object(release, "validate_manual_source"),
                patch.object(release, "validate_new_release"),
                patch.object(release, "check_existing_release"),
                patch.object(release, gate, side_effect=ValueError("gate refused")),
                patch.object(release, "request_tag") as write,
                self.assertRaisesRegex(ValueError, "gate refused"),
            ):
                release.main()
            write.assert_not_called()

    def test_new_bundle_versions_must_advance_but_same_commit_retries_are_safe(self):
        tags = f"{'a' * 40}\trefs/tags/v0.1.0\n{'b' * 40}\trefs/tags/v0.2.0"
        with patch.object(release, "git", return_value=tags):
            for tag in ["v0.0.9", "v0.1.0", "v0.2.0"]:
                with self.subTest(tag=tag), self.assertRaisesRegex(ValueError, "new version"):
                    release.validate_new_release(self.root, tag)
            release.validate_new_release(self.root, "v0.2.1")
            with patch.object(release, "remote_tag", return_value="b" * 40):
                release.validate_new_release(self.root, "v0.2.0", "b" * 40)
                with self.assertRaisesRegex(ValueError, "new version"):
                    release.validate_new_release(self.root, "v0.1.0", "b" * 40)


class ManualTagRequest(unittest.TestCase):
    def test_tag_creation_is_one_verified_post_with_no_token_output(self):
        sha = "a" * 40
        reply = {"ref": "refs/tags/v1.0.0", "object": {"sha": sha}}
        response = Mock()
        response.__enter__ = Mock(return_value=io.StringIO(json.dumps(reply)))
        response.__exit__ = Mock(return_value=False)
        with (
            patch.dict(os.environ, {"GITHUB_RELEASE_TOKEN": "synthetic-fixture-only"}),
            patch.object(release, "remote_tag", side_effect=[None, sha]),
            patch.object(release.urllib.request, "urlopen", return_value=response) as api,
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            release.request_tag(Path("."), "example/integration", "v1.0.0", sha)
        api.assert_called_once()
        request = api.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(
            request.full_url, "https://api.github.com/repos/example/integration/git/refs"
        )
        self.assertEqual(json.loads(request.data), {"ref": "refs/tags/v1.0.0", "sha": sha})
        self.assertNotIn("synthetic-fixture-only", output.getvalue())

    def test_existing_same_commit_tag_and_missing_secret_never_write(self):
        sha = "a" * 40
        with (
            patch.dict(os.environ, {"GITHUB_RELEASE_TOKEN": "synthetic-fixture-only"}),
            patch.object(release, "remote_tag", return_value=sha),
            patch.object(release.urllib.request, "urlopen") as api,
        ):
            release.request_tag(Path("."), "example/integration", "v1.0.0", sha)
        api.assert_not_called()
        with (
            patch.dict(os.environ, {"GITHUB_RELEASE_TOKEN": ""}),
            patch.object(release.urllib.request, "urlopen") as api,
            self.assertRaisesRegex(ValueError, "Missing Crow"),
        ):
            release.request_tag(Path("."), "example/integration", "v1.0.0", sha)
        api.assert_not_called()

    def test_tag_post_failure_is_not_retried(self):
        with (
            patch.dict(os.environ, {"GITHUB_RELEASE_TOKEN": "synthetic-fixture-only"}),
            patch.object(release, "remote_tag", return_value=None),
            patch.object(release.urllib.request, "urlopen", side_effect=TimeoutError) as api,
            self.assertRaises(TimeoutError),
        ):
            release.request_tag(Path("."), "example/integration", "v1.0.0", "a" * 40)
        api.assert_called_once()


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

    def test_manual_release_requires_latest_default_branch_and_preserves_tags(self):
        release.validate_manual_source(self.root, "v1.0.0", self.sha, "refs/heads/main", "main")
        for ref in ["refs/heads/feature", "refs/tags/v1.0.0"]:
            with self.subTest(ref=ref), self.assertRaisesRegex(ValueError, "default branch"):
                release.validate_manual_source(self.root, "v1.0.0", self.sha, ref, "main")
        self.git("tag", "v1.0.0")
        self.git("push", "origin", "v1.0.0")
        release.validate_manual_source(self.root, "v1.0.0", self.sha, "refs/heads/main", "main")
        (self.root / "example").write_text("next reviewed commit")
        self.git("commit", "-am", "Next fixture")
        new = self.git("rev-parse", "HEAD")
        with self.assertRaisesRegex(ValueError, "advanced"):
            release.validate_manual_source(self.root, "v1.0.1", new, "refs/heads/main", "main")
        self.git("push", "origin", "main")
        with self.assertRaisesRegex(ValueError, "never move"):
            release.validate_manual_source(self.root, "v1.0.0", new, "refs/heads/main", "main")
        release.validate_manual_source(self.root, "v1.0.1", new, "refs/heads/main", "main")
