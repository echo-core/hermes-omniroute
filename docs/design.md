# Design and rollout

## Approved architecture

One repository contains two native plugin packages. The model-provider package
uses Hermes's model loader. The backend package registers image generation,
the explicit Complex image tool, and web search/extraction through PluginContext.
Each package has one manifest and one native update/uninstall lifecycle. Internal
backend modules share its installed identity and profile scope.

A single combined identity remains a future target tracked in
[Hermes155799](https://github.com/NousResearch/hermes-agent/issues/155799).
[Native compatibility evidence](native-model-backend-proposal.md) explains the
separate loaders on v0.23.1. The approved two-plugin deployment uses those loaders.

## Profile configuration

```yaml
model:
  provider: omniroute
  default: auto
  base_url: https://YOUR-GATEWAY/v1
  api_mode: chat_completions
image_gen:
  provider: omniroute-images
  omniroute-images:
    base_url: https://YOUR-GATEWAY/v1
    regular_model: codex/gpt-image-2.5-flare
    complex_model: codex/gpt-image-2.5-sunburst
web:
  search_backend: omniroute
  extract_backend: omniroute
  keyless_rescue: false
  omniroute:
    base_url: https://YOUR-GATEWAY/v1
    search_provider: keenable
    fetch_provider: keenable-search
```

Set OMNIROUTE_API_KEY through the current profile's Hermes secret management.
OMNIROUTE_BASE_URL can supply the model endpoint when it is not explicitly
configured. The model adapter resolves context using the profile's endpoint and
key on each lookup. Keep automatic context discovery and existing auto/* routes.
The gateway owns Auto routing.

The gateway key must allow the selected models/endpoints/connections. Search
requires search capability and /v1/search endpoint access. Extraction additionally
requires web-fetch capability and /v1/web/fetch endpoint access. Change only the
selected profile's key permissions and preserve privacy/routing policies.

Web configuration is read per operation. The adapter refuses redirects, requires
an explicit trusted HTTPS gateway ending in /v1 and does not retry POSTs. Core
owns URL safety and extraction postprocessing. Preserve the gateway’s returned
page URL and requested metadata.sourceURL so core can check redirect targets
and cache content under the requested URL. Deep crawl and screenshot support
are outside the implemented web contract.

## Migration and rollback

Validate/install both packages using native Hermes management. During replacement,
disable the legacy image plugin before enabling the backend, so the Complex tool
and image backend have one owner. Keep the existing image provider name and role
settings. Disable the old standalone web draft if present. Replace the old model
plugin through native lifecycle operations. Inspect both inventory entries and
real native tool availability after reload.

Retain owner-private scoped rollback state until acceptance. Restore only changed
selections/settings and the original plugin revision on failure. Revert only
newly appended key permissions after checking for concurrent edits. Existing
sessions, provider credentials and unrelated plugin selections stay owned by their
profile. Managed plugin list leaves replace local lists, so use local selections.

## Native lifecycle verification

The two-package test in tests/native_pair_probe.py passes installation, scoped
registrations, backend disable/re-enable, native update and removal under both
in-process and host isolation on v0.23.1. Native search/extract fixtures also verify
scoped keys, denied gateway responses and private-URL rejection in-process.

The running model registry can retain a disabled/removed provider on this build.
The test records that observation and verifies model removal again in a fresh
native process. Restart the owning runtime after model-provider replacement or
removal. This is a native core cache limitation; the backend's owned registrations
unload in-process. The native context-cache probe also verifies same-gateway/different-key metadata
isolation across warm scope switches and fresh-process disk-cache reuse. Its HTTP
catalog responses are synthetic, independent of live provider dispatch.

When selecting the gateway route, set native web.keyless_rescue to false to prevent
core's default free-provider rescue from sending a failed gateway request directly
to another search/extract provider. Managed web route pins require administrator
configuration; a local selection cannot override those leaves.

## Extraction URL repair in backend 0.2.1

Preserve the gateway's final URL and bind metadata.sourceURL to the requested page.
The native regression in tests/native_extract_redirect_probe.py proves a private
redirect target is refused and a public redirect's cached final URL is checked
again on reuse. The old adapter fails that assertion. Provider HTTP is mocked.

Before reusing extraction cache created by backend 0.2.0, expire entries for this
provider in the selected profile so responses missing their final-URL provenance
are fetched again. Other providers' caches and user artifacts are separate from
this migration. The pilot had only one attributable public acceptance entry.

## Acceptance

- Both native package installs validate and appear in management inventory.
- Two disposable profiles use separate keys and scoped registrations. Exercise
  native reload, disable/re-enable, update and remove with coherent cleanup.
- Model identity, Auto catalog and dynamic context preserve existing behavior.
- Regular/Complex generation, multipart references, deadline and error/fallback
  handling pass fixture tests. Check native attachment display/download separately.
- Native web_search routes to /v1/search and web_extract to /v1/web/fetch. Public
  result content and provider provenance must match. Explicit backend selection
  must return errors without silently using a direct provider on gateway failure.
- Native URL policy rejects private targets before extract dispatch. Verify batch
  order, partial failures and truncation through normal Hermes tool entry points.
- Fresh-chat acceptance is distinct from endpoint/fixture tests. Paid provider
  tests require an explicit budget. Preserve existing voice selections.
