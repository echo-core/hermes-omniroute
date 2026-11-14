# Hermes OmniRoute integrations

Native Hermes plugins maintained together for chat/model discovery, Regular/Complex
images, and web search/extraction through a scoped OmniRoute gateway.

The initial commit preserves the deployed model and image source unchanged.
These are separate native plugin registrations from one source repository. Search
and extraction are implemented in `plugins/web/omniroute`; optional media modules
are future work.

## Development boundaries

- Credentials remain in each profile’s Hermes secret store; never commit them.
- Use native Hermes provider APIs, plugin lifecycle and configuration.
- Keep capability selection local per profile; managed plugin lists replace user lists.
- Start with one selected profile; additional profiles require separate rollout.
- Preserve existing model context discovery, image tools and Microsoft/local voice.
- Run free contract tests before rollout; paid provider tests require an explicit budget.

Compatibility baseline: official Hermes v0.23.1 and OmniRoute 3.8.54 next.

## Tests and rollout

Run `python -m unittest discover -s tests -v` with requests installed (already a
Hermes core dependency). Tests are offline and require no credentials.
See [design and rollout](docs/design.md) for configuration, isolation, acceptance
and rollback. Model and image source remain unchanged in this release.
