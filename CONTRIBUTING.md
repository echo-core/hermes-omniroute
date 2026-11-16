# Contributing

Use a feature branch and open a pull request. Do not commit or push directly to
`main` or `master`. Keep changes generic and preserve independent Hermes profile
credentials and native plugin lifecycle behavior.

## Local setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```sh
uv sync --locked
uv run --locked prek install --force
uv run --locked prek run --all-files --stage manual
```

uv selects Python 3.11 from `.python-version` and manages `.venv`. It can download
that interpreter if needed. Development dependencies are declared in the `dev`
group in `pyproject.toml`; commit `uv.lock` so everyone uses the same resolutions.
The root project is development-only and is not installed as a package. Each
plugin keeps its own version in `plugin.yaml`; the development project's version
is not a plugin release version. Hermes owns its runtime and dependencies.

The install command replaces inherited pre-commit shims with prek and installs
pre-commit, commit-msg and pre-push hooks. They require this repository's config;
a missing configuration does not silently skip checks. The test hook uses
`uv run --locked --offline`, so commits and pushes work without activating a
virtual environment and cannot silently change the lockfile or fetch test packages.
Run `uv sync --locked` first when setting up a checkout or after pulling a lockfile
change. Hook environments may need network access on their first setup.

## Run tests

```sh
uv run --locked python -m pytest -q tests
```

These tests use synthetic inputs and mocked provider responses. The separate
`tests/native_*_probe.py` scripts require Hermes's own runtime; uv's development
environment does not replace that runtime.

## Checks and commits

The hooks check Python lint/format, YAML/JSON/TOML, whitespace/line endings, merge
conflicts, file sizes, private keys and potential secrets. Secret scanning is local;
it does not verify candidate credentials over the network. Inline exceptions cover
only reviewed Git commit hashes and explicit synthetic test credentials. The offline contract
tests run before pushes and during a manual full check. Native integration probes
need a Hermes runtime and are separate from this offline suite.

Use Conventional Commits, for example `fix(web): preserve extraction redirects` or
`test(images): cover regular fallback`. The commit-msg hook rejects unstructured,
fixup and merge messages. Public issue titles remain plain declarative reports;
they are not Git commit messages. Include a short commit-message body explaining
what changed and why. Existing historical commits are not rewritten.

The branch guard runs locally on commit/push. Run the manual full check before
submitting a pull request.

## Updating frozen tools

All remote hook revisions are pinned to full commit SHAs with
release comments. Refresh hooks deliberately on a feature branch:

```sh
uv run --locked prek update --freeze
uv run --locked prek run --all-files --stage manual
```

Review upstream changes, the frozen revisions and the full diff before committing.
There is no commit-time hook that silently advances these pins. To update a
specific development dependency within its declared version range:

```sh
uv lock --upgrade-package prek
uv sync --locked
uv run --locked prek install --force
uv run --locked prek run --all-files --stage manual
```

Use `uv add --dev` to add dependencies or deliberately change their version ranges.
Review both `pyproject.toml` and `uv.lock`. The lockfile hook rejects stale locks;
keep it and the offline-test hook enabled for your local checks. Local checks work
independently of the Crow workflow.

Keep paid/live provider calls out of the default hooks. Live acceptance
requires a separate explicit budget and selected-profile authorization.

## Crow checks and releases

The workflow in `.crow/pipeline.yaml` follows the same checks as local development.
PRs, pushes and ordinary manual checks execute locked uv/prek checks without a
publishing credential. The separate manual Release workflow creates an approved
version tag; version tags prepare, publish and verify a GitHub release
through the Crow-compatible [Git Release plugin](https://woodpecker-ci.org/plugins/release).
The workflow images are pinned by version and digest.

For a stable release:

1. Add the versioned changelog entry and `docs/releases/vX.Y.Z.md` in a PR. Include
   the exact version of each native plugin; their versions need not be identical.
2. Merge the reviewed PR. In Crow, open **Run pipeline**, select the latest `main`,
   and select the **release** workflow (`.crow/release.yaml`).
3. Enter `RELEASE_VERSION`, for example `v0.1.0`, then click **Run pipeline**.
   Crow runs checks, validates the release notes and the current default-branch
   commit, then creates the tag. No local Git commands are needed.
   The manual run finishes after verifying tag creation; follow the separate
   **tag-triggered pipeline** for publication. Keep Tags enabled in Crow.
   You can still create/push an approved tag with Git instead.
4. Crow runs the existing checks, prepares the notes, publishes the GitHub Release
   with the plugin, then verifies its title, tag and notes. No manual GitHub release
   creation is needed. Source archives are provided by GitHub.

Only stable `vMAJOR.MINOR.PATCH` tags are supported. Missing or unfinished notes,
manifest/version-matrix differences, unmerged source and conflicting existing
release metadata stop publication. Retries preserve an identical existing release;
the plugin does not overwrite notes or upload extra artifacts. Release-note links
are rewritten to the tagged source so they work on the GitHub Releases page.

### Release and plugin versions

Repository tags version the complete integration bundle. The first repository
release is `v0.1.0`, containing provider `0.2.1` and backend `0.2.3`. The bundle
version need not equal either plugin version; record both in every release's
changelog and notes. Increment a plugin's manifest version only when that plugin
changes. A bundle release never downgrades a plugin to the bundle's version.

Use patch bundle releases for compatible fixes and minor releases for new
features. While the bundle is `0.x`, breaking changes also require a minor bump
and explicit migration notes; after `1.0.0`, they require a major bump. Increment
the changed plugin's version independently, documenting any compatibility impact.
Do not rename, move or reuse published tags.

Hermes updates each plugin from its configured Git source, not the newest GitHub
release. Ordinary unpinned installs continue following Git; exact-SHA installs
stay pinned. Publishing a bundle release does not trigger a client update or
change either manifest. Existing native update commands remain unchanged.

### Crow repository setup

Enable the repository in Crow and use the existing webhook with the default
`.crow/` configuration directory. Provide a repository-scoped secret named
`github_release_token`, using a GitHub fine-grained token with **Contents: read and
write** for this repository only. Allow the secret for **tag** and **manual**
events; leave PR/push events disabled. Leave the optional image restriction empty:
the manual helper is a command step, while image-restricted secrets are intended
for native plugin steps. Both workflow images remain digest-pinned.
Keep the token out of source files and logs. A webhook alone does
not configure the repository or provide its publishing credential.

Only `create-release-tag` (manual) and `publish-release` (tag) reference the
credential. The manual helper only creates a tag through GitHub's API, with no
credential in command arguments or saved artifacts; release publication stays in
the native plugin without custom commands. Preparation and verification use unauthenticated
public GitHub reads; an API outage or rate limit fails the job visibly. If publishing
succeeded before a later verification failure, retry the original tag pipeline;
do not move the tag or manually overwrite the release. Repeating a manual run for
an existing same-commit tag makes no write and does not trigger another tag event:
inspect/retry its original tag pipeline instead. A different-commit tag is refused.
If main advances during checks, start a fresh manual run from its latest commit.

Run `crow lint --strict .crow/` with a CLI matching the server before changing the
workflow. The release helper and its negative-path tests run in the normal offline
suite; no live publishing credential or provider calls are used by those tests.
Publishing a GitHub Release leaves Hermes's ordinary Git updates and optional
full-SHA pinning unchanged.
