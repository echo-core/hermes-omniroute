# Hermes OmniRoute integrations

Two native Hermes plugins route models, Regular/Complex image generation, web
search and Markdown extraction through a profile's OmniRoute gateway.

| Plugin | Source | Capabilities |
| --- | --- | --- |
| hermes-omniroute-provider | model-provider/ | Named provider, native chat transport/catalog, dynamic context lookup, auxiliary auto/fast |
| hermes-omniroute-backend | backend/ | Regular/Complex images, web_search and web_extract |

Video and additional media adapters can be added to the backend after implementation
and verification. Microsoft/local voice selections are independent of installation.

## Install

```sh
hermes plugins install echo-core/hermes-omniroute#model-provider
hermes plugins install echo-core/hermes-omniroute#backend
```

Use native enablement for both installed plugins in the selected profile. Keep
plugin selections local per profile. See [configuration and rollout](docs/design.md).
The repository is private; native Git access is required for installation/updates.

Credentials belong in the selected profile's Hermes secret store. Configure an
explicit gateway URL and preserve existing gateway key restrictions. No gateway
URL, provider credential or deployment identity is bundled.

## Tests

Run `python -m pytest -q tests` for all offline model/image/web contract tests
with pytest and requests installed. The unittest subset can also run with
`python -m unittest discover -s tests -v`. Native integration checks require Hermes's managed runtime.
See [the unified-plugin compatibility reproduction](docs/native-model-backend-proposal.md)
for the current upstream limitation and proposed future consolidation.

Compatibility baseline is official Hermes v0.23.1 and OmniRoute 3.8.54 next.
