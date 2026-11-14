# Contributing

Use a feature branch and open a pull request. Do not commit or push directly to
`main` or `master`. Keep changes generic and preserve independent Hermes profile
credentials and native plugin lifecycle behavior.

## Local setup

Use Python 3.11 or newer and an isolated development environment:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.txt
prek install --force
prek run --all-files --stage manual
```

The install command replaces inherited pre-commit shims with prek and installs
pre-commit, commit-msg and pre-push hooks. They require this repository's config;
a missing configuration does not silently skip checks. Keep the environment
active while committing or pushing so the offline-test hook uses the frozen test
dependencies. These dependencies are development-only: do not install them into
Hermes's managed runtime.

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
they are not Git commit messages. Existing historical commits are not rewritten.

The branch guard runs locally on commit/push. Run the manual full check before
submitting a pull request.

## Updating frozen tools

All remote hook revisions are pinned to full commit SHAs with
release comments. Refresh hooks deliberately on a feature branch:

```sh
prek update --freeze
prek run --all-files --stage manual
```

Review upstream changes, the frozen revisions and the full diff before committing.
There is no commit-time hook that silently advances these pins. To update the
Python test/tool dependencies, edit `requirements-dev.in`, then regenerate the
hash-locked file with uv:

```sh
uv pip compile requirements-dev.in --python-version 3.11 --universal --generate-hashes --index-url https://pypi.org/simple --output-file requirements-dev.txt
python -m pip install --require-hashes -r requirements-dev.txt
```

Keep paid/live provider calls out of the default hooks. Live acceptance
requires a separate explicit budget and selected-profile authorization.
