# Changes

## 0.1.0 — development pilot

- Include native model and Regular/Complex image modules.
- Add native web-provider0.1.0 for search and Markdown extraction.
- Profile-scoped credentials/config, explicit gateway URL, no redirects/retries.
- Add16offline wire-contract/security/error tests.
- Initial profile acceptance pending; optional voice/video modules not implemented.

## Unreleased

- Added a native-install reproduction of the model/backend lifecycle gap.
- Documented a proposed upstream model export under one backend identity.
- Fixed generic context lookup to resolve the configured endpoint and current
  profile's key. Declared the endpoint variable separately from API key aliases.
- Added seven offline model regressions. All 23 offline model/web tests pass.

## 0.2.0

- Package a model provider and a combined image/web backend as two native plugins.
- Preserve Regular/Complex image roles and native tool names while adding gateway
  search and extraction in the backend.
- Keep a shared identity as a future upstream integration target.

- Native two-profile lifecycle passes in both execution modes. Model cache cleanup
  is verified after a fresh process. In-process web fixtures cover errors and SSRF.

## 0.2.1 backend

- Preserve the gateway's final page URL for native redirect safety and cache
  tracking, while binding sourceURL to the requested page.
- Reject non-web final URLs and include the explicit no-rescue setting in the
  example configuration.

- Verify distinct context metadata for two keys at the same gateway, including
  warm scope switches and fresh-process disk-cache reuse, with native Hermes.
