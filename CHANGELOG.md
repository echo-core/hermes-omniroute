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
