"""Validate releases, request an approved version tag, and verify publication."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlsplit

TAG = re.compile(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
PLUGINS = {
    "model-provider": "hermes-omniroute-provider",
    "backend": "hermes-omniroute-backend",
}


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def validate_source(root: Path, tag: str, sha: str, ref: str, branch: str) -> None:
    if ref != f"refs/tags/{tag}" or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Publication requires the original version-tag event and commit SHA")
    if git(root, "rev-parse", "HEAD") != sha:
        raise ValueError("Checked-out source differs from the tagged pipeline commit")
    git(root, "check-ref-format", f"refs/heads/{branch}")
    # Check the live tag, including annotated tags, without creating or moving it.
    refs = git(
        root,
        "ls-remote",
        "--exit-code",
        "--tags",
        "origin",
        f"refs/tags/{tag}",
        f"refs/tags/{tag}^{{}}",
    )
    targets = dict(line.split()[::-1] for line in refs.splitlines())
    target = targets.get(f"refs/tags/{tag}^{{}}", targets.get(f"refs/tags/{tag}"))
    if target != sha:
        raise ValueError("The remote version tag differs from this pipeline commit")
    fetch = ["fetch", "--no-tags"]
    if git(root, "rev-parse", "--is-shallow-repository") == "true":
        fetch.append("--unshallow")
    git(root, *fetch, "origin", f"refs/heads/{branch}")
    git(root, "merge-base", "--is-ancestor", sha, "FETCH_HEAD")


def remote_tag(root: Path, tag: str) -> str | None:
    refs = git(root, "ls-remote", "--tags", "origin", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}")
    targets = dict(line.split()[::-1] for line in refs.splitlines())
    return targets.get(f"refs/tags/{tag}^{{}}", targets.get(f"refs/tags/{tag}"))


def validate_manual_source(root: Path, tag: str, sha: str, ref: str, branch: str) -> None:
    if not TAG.fullmatch(tag) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Expected a stable version and exact commit SHA")
    if ref != f"refs/heads/{branch}" or git(root, "rev-parse", "HEAD") != sha:
        raise ValueError("Releases must use the checked-out default branch")
    git(root, "check-ref-format", f"refs/heads/{branch}")
    refs = git(root, "ls-remote", "--exit-code", "origin", f"refs/heads/{branch}")
    if refs.split()[0] != sha:
        raise ValueError("Default branch advanced; start a new run from its latest commit")
    target = remote_tag(root, tag)
    if target is not None and target != sha:
        raise ValueError("Existing version tag points to another commit; never move a release tag")


def planned_release(root: Path) -> str:
    headings = re.findall(r"^## (v[^\n]+)$", (root / "CHANGELOG.md").read_text(), re.M)
    if not headings or not TAG.fullmatch(headings[0]):
        raise ValueError("Prepare a stable versioned changelog entry before promoting to main")
    return headings[0]


def validate_new_release(root: Path, tag: str, sha: str | None = None) -> None:
    refs = git(root, "ls-remote", "--tags", "origin", "refs/tags/v*")
    version = tuple(map(int, tag[1:].split(".")))
    for line in refs.splitlines():
        _target, ref = line.split()
        existing = ref.removeprefix("refs/tags/")
        if not TAG.fullmatch(existing):
            continue
        if existing == tag and sha and remote_tag(root, tag) == sha:
            continue  # A retry of this exact main commit never creates another tag.
        if tuple(map(int, existing[1:].split("."))) >= version:
            raise ValueError("Prepare a new version greater than all existing stable tags")


def check_existing_release(repository: str, tag: str, notes: str) -> None:
    try:
        existing = fetch_release(repository, tag)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    else:
        verify_release(existing, tag, f"Hermes OmniRoute {tag}", notes)


def request_tag(root: Path, repository: str, tag: str, sha: str) -> None:
    # This is the only credentialed helper operation: create one immutable tag.
    token = os.environ.get("GITHUB_RELEASE_TOKEN", "")
    if not token:
        raise ValueError("Missing Crow publishing secret for tag creation")
    if remote_tag(root, tag) == sha:
        print(f"Tag {tag} already exists at this commit; inspect or retry its tag pipeline")
        return
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/git/refs",
        data=json.dumps({"ref": f"refs/tags/{tag}", "sha": sha}).encode(),
        headers={
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "hermes-omniroute-release-check",
        },
        method="POST",
    )
    # No overwrite, force, delete, or automatic POST retry. A lost response is safe
    # to investigate/rerun because a same-commit tag is accepted above.
    with urllib.request.urlopen(request, timeout=30) as response:
        created = json.load(response)
    if created.get("ref") != f"refs/tags/{tag}" or created.get("object", {}).get("sha") != sha:
        raise ValueError("Tag creation response did not match the requested version/commit")
    if remote_tag(root, tag) != sha:
        raise ValueError("Created tag could not be verified; inspect it before retrying")
    print(f"Created and verified {tag}; follow the tag-triggered pipeline for publication")


def release_notes(root: Path, tag: str, repository: str) -> str:
    if not TAG.fullmatch(tag) or not REPOSITORY.fullmatch(repository):
        raise ValueError("Expected a stable vMAJOR.MINOR.PATCH tag and owner/repository")
    source = root / "docs" / "releases" / f"{tag}.md"
    text = source.read_text()
    if text.splitlines()[0] != f"# Hermes OmniRoute {tag}":
        raise ValueError("Release-note title does not match the tag")
    if re.search(r"publication is pending|prepared notes|TODO|TBD", text, re.I):
        raise ValueError("Finalize release notes before tagging")
    changelog = (root / "CHANGELOG.md").read_text()
    sections = re.split(r"^## ", changelog, flags=re.M)
    section = next((s for s in sections if s.splitlines()[0] == tag), None)
    if section is None:
        raise ValueError("Missing versioned changelog section")
    for directory, name in PLUGINS.items():
        manifest = (root / directory / "plugin.yaml").read_text()
        version = re.search(r"^version: ([0-9]+\.[0-9]+\.[0-9]+)$", manifest, re.M)
        if version is None:
            raise ValueError(f"Missing plain semantic version in {directory}/plugin.yaml")
        row = rf"^\| `{re.escape(name)}` \| {re.escape(version[1])} \|"
        if not all(re.search(row, s, re.M) for s in (text, section)):
            raise ValueError(f"Plugin version matrix differs from {directory}/plugin.yaml")

    def link(match: re.Match[str]) -> str:
        label, target = match.groups()
        url = urlsplit(target)
        if url.scheme or url.netloc or target.startswith("#"):
            return match[0]
        path = (source.parent / url.path).resolve()
        relative = path.relative_to(root.resolve())
        if not path.is_file():
            raise ValueError(f"Release note links to a missing file: {relative}")
        href = f"https://github.com/{repository}/blob/{tag}/{quote(relative.as_posix())}"
        if url.fragment:
            href += "#" + url.fragment
        return f"[{label}]({href})"

    # GitHub release bodies resolve relative links differently from repository files.
    return re.sub(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)\)", link, text).strip() + "\n"


def fetch_release(repository: str, tag: str) -> dict:
    url = f"https://api.github.com/repos/{repository}/releases/tags/{tag}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "hermes-omniroute-release-check",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def verify_release(data: dict, tag: str, title: str, notes: str) -> None:
    if (
        data.get("tag_name") != tag
        or data.get("name") != title
        or (data.get("body") or "").strip() != notes.strip()
        or data.get("draft") is not False
        or data.get("prerelease") is not False
    ):
        raise ValueError("Existing GitHub release differs from the reviewed tag/title/notes")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "prepare",
            "verify",
            "prepare-manual",
            "create-tag",
            "check-promotion",
            "prepare-auto",
            "create-auto-tag",
        ],
    )
    args = parser.parse_args()
    root = Path.cwd()
    manual = args.action in {"prepare-manual", "create-tag"}
    automatic = args.action in {"prepare-auto", "create-auto-tag"}
    promotion = args.action == "check-promotion"
    tag = (
        planned_release(root)
        if automatic or promotion
        else os.environ.get("RELEASE_VERSION" if manual else "CI_COMMIT_TAG", "")
    )
    repository = os.environ.get("CI_REPO", "")
    notes = release_notes(root, tag, repository)
    title = f"Hermes OmniRoute {tag}"
    if promotion:
        if (
            os.environ.get("CI_PIPELINE_EVENT") != "pull_request"
            or os.environ.get("CI_COMMIT_SOURCE_BRANCH") != "dev"
            or os.environ.get("CI_COMMIT_TARGET_BRANCH")
            != os.environ.get("CI_REPO_DEFAULT_BRANCH", "main")
        ):
            raise ValueError(
                "Stable promotions must be pull requests from dev to the default branch"
            )
        validate_new_release(root, tag)
        check_existing_release(repository, tag, notes)
        print(f"Validated dev promotion for {tag}; no tag or release created")
    elif automatic:
        if os.environ.get("CI_PIPELINE_EVENT") != "push":
            raise ValueError("Automatic releases require a default-branch push")
        sha = os.environ.get("CI_COMMIT_SHA", "")
        validate_manual_source(
            root,
            tag,
            sha,
            os.environ.get("CI_COMMIT_REF", ""),
            os.environ.get("CI_REPO_DEFAULT_BRANCH", "main"),
        )
        validate_new_release(root, tag, sha)
        check_existing_release(repository, tag, notes)
        if args.action == "create-auto-tag":
            request_tag(root, repository, tag, sha)
        else:
            print(f"Validated automatic release {tag}; no tag or release created")
    elif manual:
        if os.environ.get("CI_PIPELINE_EVENT") != "manual":
            raise ValueError("Tag creation is only allowed from an explicit manual run")
        sha = os.environ.get("CI_COMMIT_SHA", "")
        validate_manual_source(
            root,
            tag,
            sha,
            os.environ.get("CI_COMMIT_REF", ""),
            os.environ.get("CI_REPO_DEFAULT_BRANCH", "main"),
        )
        check_existing_release(repository, tag, notes)
        if args.action == "create-tag":
            request_tag(root, repository, tag, sha)
        else:
            print(f"Validated manual release {tag}; no tag or release created")
    elif args.action == "prepare":
        validate_source(
            root,
            tag,
            os.environ.get("CI_COMMIT_SHA", ""),
            os.environ.get("CI_COMMIT_REF", ""),
            os.environ.get("CI_REPO_DEFAULT_BRANCH", "main"),
        )
        # Refuse a conflicting existing release before the credentialed plugin runs.
        check_existing_release(repository, tag, notes)
        output = root / ".release"
        if output.is_symlink():
            raise ValueError("Release output directory must not be a symlink")
        output.mkdir(exist_ok=True)
        (output / "title.txt").write_text(title)
        (output / "notes.md").write_text(notes)
        print(f"Prepared {tag} from reviewed source")
    else:
        verify_release(fetch_release(repository, tag), tag, title, notes)
        print(f"Verified published GitHub release {tag}")


if __name__ == "__main__":
    main()
