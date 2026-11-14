# Hermes OmniRoute integrations

Native Hermes plugins maintained together for chat/model discovery, Regular/Complex
images, and web search/extraction through a scoped OmniRoute gateway.

The model and image modules are based on working native Hermes integrations.
Target: one installable plugin and one UI entry covering all implemented capabilities.
The current component directories are experimental; unified lifecycle is not yet qualified. Search
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
and rollback. Gateway URLs and credentials are configured per profile.

## Gateway configuration

Set `OMNIROUTE_API_KEY` and `OMNIROUTE_BASE_URL` through the selected profile’s
Hermes secret/configuration interface. The model module has no bundled gateway
URL. Image and web modules require their own explicit HTTPS base_url settings;
see the module configuration examples. No private deployment identity belongs
in this repository.

## Status

Architecture gate: prove single-entry native model/backend discovery, enablement,
update and uninstall with profile isolation before deployment. See docs/design.md.
Native install reproduces the model/backend lifecycle gap on Hermes v0.23.1 in
both execution modes. See [the compatibility reproduction and local upstream
proposal](docs/native-model-backend-proposal.md). No supported single-entry pilot
has been deployed. The offline suite now contains 23 model/web contract tests.
