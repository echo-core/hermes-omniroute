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
keep it and the offline-test hook enabled for your local checks. No CI service
is required by this setup.

Keep paid/live provider calls out of the default hooks. Live acceptance
requires a separate explicit budget and selected-profile authorization.
