# Plugin development

Read README.md and docs/design.md before implementation. Preserve the native
Hermes interfaces and independent profile credentials. Do not introduce a runtime
fork or supervisor. No secret-bearing config, tokens, environments or private user
content in source, tests or commits. Tests must be offline by default. Live paid
provider tests require an explicit budget. Stage capability rollouts to one explicitly selected
profile first and retain rollback until native tool acceptance passes.
Commit meaningful tested milestones to the authorized origin. Do not force-push.
