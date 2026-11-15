# Plugin development

Read README.md and docs/design.md before implementation. Preserve the native
Hermes interfaces and independent profile credentials. Do not introduce a runtime
fork or supervisor. No secret-bearing config, tokens, environments or private user
content in source, tests or commits. Tests must be offline by default. Live paid
provider tests require an explicit budget. Stage capability rollouts to one explicitly selected
profile first and retain rollback until native tool acceptance passes.
Commit meaningful tested milestones to the authorized origin. Do not force-push.

The approved deployment uses two native plugins from this repository: one model
provider and one backend for images, web search and extraction. Future media
adapters belong in the backend. A single combined identity remains the long-term
target once supported upstream. Keep both plugins profile-scoped and prove native
lifecycle and isolation before rollout. Repository files must remain generic;
deployment identities and hosts belong in external deployment records.

## Repository checks

Work on a feature branch and submit a pull request; never commit or push directly
to main/master. Use Conventional Commit messages with a short explanatory body.
Read CONTRIBUTING.md and install prek before committing. Keep all remote hooks frozen to full commit SHAs;
update them deliberately with prek update --freeze and review the resulting diff.
Use `uv sync --locked` and `uv run --locked prek install --force` for setup.
Run `uv run --locked prek run --all-files --stage manual` and offline tests before pushing. Do not bypass failed
checks, rewrite published history or change repository visibility without explicit
user authorization. Checks are offline with respect to model/provider APIs. Development
tooling does not authorize deployment or changes to installed Hermes plugins.
