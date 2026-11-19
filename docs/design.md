# Design and rollout

## Approved architecture

One repository contains two native plugin packages. The model-provider package
uses Hermes's model loader. The backend package registers image generation,
the explicit Complex image tool, and web search/extraction through PluginContext.
Each package has one manifest and one native update/uninstall lifecycle. Internal
backend modules share its installed identity and profile scope.

## Unified plugin roadmap

The goal is one native install, one OmniRoute entry in the Hermes plugin UI,
and one enable, update, disable and uninstall lifecycle per profile. The plugin
will include model discovery and dynamic context, Regular and Complex image
generation, web search and page extraction. The two packages remain the
supported installation while Hermes adds a shared lifecycle.
[Hermes issue #155799](https://github.com/NousResearch/hermes-agent/issues/155799)
tracks that support. The [native compatibility notes](native-model-backend-proposal.md)
document the separate loaders and the original reproduction.

1. **Confirm support in a Hermes release.** Identify the released version and
   supported registration API that let one plugin own a model provider and
   backend tools under the same profile and lifecycle. An issue closure or a
   merged change is only a signal to check the release.
2. **Combine the adapters.** Give the existing model, image and web adapters a
   single manifest and installed identity. Preserve provider and tool names,
   profile configuration and keys, the Auto catalog, dynamic context and
   auxiliary routing. Update the release checks and notes for the new package
   layout. Keep the existing subdirectory sources available during migration so
   installed clients can continue to update.
3. **Verify the lifecycle and isolation.** On the supported Hermes version,
   exercise install, update, disable, re-enable and uninstall, including cleanup
   on partial failure. Check early model discovery and one rendered plugin UI
   entry. In two test profiles sharing a gateway URL, use distinct dummy keys,
   model catalogs and context limits to check registration and cache isolation.
   Cover the supported execution modes and the feature checks in
   [Acceptance](#acceptance).
4. **Pilot the migration and rollback.** Record both existing plugin revisions
   and profile selections. In one opt-in profile, use native management to switch
   from the pair to the unified plugin with one active registration per capability.
   Check startup warnings and verify the model picker, images, search and
   extraction in a fresh chat. Exercise rollback by removing the unified plugin
   and restoring the recorded pair and selections. Paid tests require an explicit
   budget.
5. **Publish migration instructions with the first unified release.** State the
   minimum Hermes version, how existing users switch, how to roll back, and which
   releases support the two-package install. Make the unified install the default
   after pilot acceptance. Keep migration an explicit choice for existing profiles.

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
```

Set OMNIROUTE_API_KEY through the current profile's Hermes secret management.
OMNIROUTE_BASE_URL can supply the model endpoint when it is not explicitly
configured. The model adapter resolves context using the profile's endpoint and
key on each lookup. Its native `runtime_base_url` hook supplies that same validated
endpoint to auxiliary credential resolution, which does not otherwise inherit
`model.base_url`. Resolve it per call so a warm profile switch cannot retain
another profile's endpoint. Keep automatic context discovery and existing auto/* routes.
The gateway owns Auto routing. Leave `web.omniroute.search_provider` and
`fetch_provider` unset for normal operation. The adapter then omits `provider`
from both requests: OmniRoute selects within its configured, key-authorized
search/fetch pools and applies its own fallback policy. Explicit provider fields
are optional overrides for a deliberate provider-specific request, not defaults.

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

## Image routing context (backend 0.2.2)

The backend registers `omniroute.image-routing` with the native
`register_system_prompt_section` API. A bounded callable reads selection and
availability when rendered; a captured home guard suppresses cross-profile reuse.
It emits only for the selected, available OmniRoute image backend and contains
no keys, endpoints, user content or model IDs. Ordinary versus Complex selection
continues through the configured native tools, with exact-name deferred discovery.
The instruction honors explicitly requested alternative services and treats
unavailable/denied tools as blockers, without enabling them or bypassing approvals.

Core owns prompt persistence and registration cleanup. New prompt builds reflect
disable/remove; existing cached prompts are not silently rewritten. Test fresh
sessions and native refresh separately from registration/rendering. Plugin unload
is not a revocation of historical prompt bytes. Tool-search settings, connector
grants and per-user SOUL files remain outside this change.

Offline guards are covered by `tests/test_image_guidance.py`. Run
`tests/native_image_guidance_probe.py` through Hermes's runtime to test selection,
profile isolation, credential loss and native disable/re-enable/remove in both
in-process and host isolation. These tests make no provider inference requests;
model-driven route choice is a separate live acceptance check.


## Reference upload types (backend 0.2.3)

Native image loading may return JPEG bytes with a `.png` filename. Passing that
stream directly makes the SDK declare `image/png`, which Codex edit validation
rejects because the bytes do not match. Both image modes now inspect the first
12 bytes for PNG, JPEG and WebP signatures, then pass an explicit SDK file tuple
with the matching filename and MIME type. Source bytes, reference order and input
files remain unchanged. Unknown formats retain the existing native upload path
and remain subject to gateway validation.

The original streams stay owned by the operation's context stack and are rewound
before the existing definite-rate-limit fallback. There is no transcode, new
runtime dependency, provider switch or error retry policy change. Unit contracts
cover incorrect extensions, single and multiple references, both modes, cleanup
and rewind. The separate native SDK probe verifies actual multipart headers and
payloads through a mock transport, with no upstream dispatch.
