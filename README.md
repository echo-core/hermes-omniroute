# Hermes OmniRoute integrations

Native Hermes plugins maintained together for chat/model discovery, Regular/Complex
images, and web search/extraction through a scoped OmniRoute gateway.

The initial commit preserves the deployed model and image source unchanged.
These are separate native plugin registrations from one source repository. Search
and extraction are the next module; optional media modules are future work.

## Development boundaries

- Credentials remain in each profile’s Hermes secret store; never commit them.
- Use native Hermes provider APIs, plugin lifecycle and configuration.
- Keep capability selection local per profile; managed plugin lists replace user lists.
- Start with one selected profile; additional profiles require separate rollout.
- Preserve existing model context discovery, image tools and Microsoft/local voice.
- Run free contract tests before rollout; paid provider tests require an explicit budget.

Compatibility baseline: official Hermes v0.23.1 and OmniRoute 3.8.54 next.
