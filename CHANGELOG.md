# Changelog

User-facing changes to the Hermes OmniRoute integration are recorded here.
Repository releases can include different versions of the two native plugins.
Changes under **Unreleased** are pending. Versioned entries describe release
contents; publication is recorded in GitHub Releases.

## Unreleased

### Added

- Backend 0.3.2 can route each user message with a selectable OmniRoute
  classifier. Brief follow-ups carry earlier task context, and the classifier
  can change the answer combo as tool results arrive. Routing is opt-in per
  profile. A reference to a numbered finding can use that item alone.
  A local classifier can check whether the new request needs fresh evidence.
  Tool-step decisions include the active tier and the user's objective.

### Fixed

- Backend 0.3.6 binds short skill-edit requests to the preceding assistant answer.

- Backend 0.3.5 preserves the typed instruction and preceding conversation when
  routing expanded skills and clarifies the source of edits for the answer model.

- Backend 0.3.4 can use a brief local follow-up check to route courtesies and
  requests to wait to the routine combo while preserving agreed start signals.

- Backend 0.3.3 keeps a pending offer in the routing context when a user
  acknowledges it, the assistant waits, and the user later approves the work.

### Development

- Give GitHub up to one minute to expose a newly published release before the
  final check fails. Retry only missing-release reads; never repeat publication.

## v0.1.0

First public release.

| Plugin | Version |
| --- | --- |
| `hermes-omniroute-provider` | 0.2.1 |
| `hermes-omniroute-backend` | 0.2.3 |

### Added

- Native OmniRoute model identity, live model catalogs and automatic context-limit
  discovery using the current profile's gateway and key.
- Regular and Complex image generation through the existing Hermes image tools.
- Web search and Markdown page extraction through OmniRoute. The gateway selects
  from the providers available to the profile's key unless explicitly overridden.
- Profile-scoped image-tool guidance, including discovery of deferred image tools.
  Explicit requests for another service still take precedence.
- Shared GitHub homepage metadata in both plugin manifests. Hermes 0.23.1 accepts
  the metadata but does not display it on user-plugin cards.

### Fixed

- Supply the current profile's gateway endpoint to native auxiliary clients,
  including vision and title generation.
- Discover context limits with the configured endpoint and current profile's key;
  keep cached metadata isolated between profiles sharing a gateway.
- Preserve extraction's final URL and requested source URL so Hermes can enforce
  redirect safety and validate cached results. Reject non-web final URLs.
- Identify PNG, JPEG and WebP image references from their bytes and send the
  matching upload MIME type, even when the filename has the wrong extension.
  Preserve reference bytes, order and stream cleanup.

### Development

- Add a Crow workflow for locked checks and tag-triggered GitHub releases through
  the Git Release plugin, using reviewed notes and a scoped publishing secret.
- Use feature branches into bleeding-edge `dev`, then reviewed promotions to
  stable `main`. Main merges automatically create the prepared release tag;
  the native tag pipeline publishes and verifies the release. A manual Release
  workflow remains available for recovery.
- Manage development tooling with uv and a committed lockfile.
- Use frozen prek hooks for formatting, linting, secret checks, Conventional
  Commits, branch protection and offline contract tests.
- Include native lifecycle, profile-isolation and SDK multipart probes alongside
  the offline unit suite. Provider calls are excluded from the default checks.

See the [v0.1.0 release notes](docs/releases/v0.1.0.md) for installation,
compatibility and known limitations.

## Development milestones

These are pre-release component milestones, not previously published GitHub releases.

| Milestone | Main change |
| --- | --- |
| Backend 0.2.3 | Correct image-reference upload types from file contents. |
| Backend 0.2.2 | Add native guidance for selecting and discovering image tools. |
| Model provider 0.2.1 | Resolve auxiliary gateway endpoints per profile. |
| Backend 0.2.1 | Preserve extraction URL provenance and redirect safety. |
| 0.2.0 | Package the integration as a model provider and a combined image/web backend. |
| 0.1.0 pilot | Establish native model, image and standalone web adapters with profile-scoped credentials. The standalone web draft was superseded by the combined backend. |
