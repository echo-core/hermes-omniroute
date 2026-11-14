# Design and rollout

One maintained repository, separate native registrations. Model and image source
are initially preserved exactly from the accepted deployment. The web module uses
the native WebSearchProvider contract and Hermes's profile-aware provider_env.

The web module registers no media/model providers. It requires an explicit trusted
HTTPS gateway URL, refuses redirects and does not retry POSTs. No public gateway
or provider credential is bundled. Provider choices are read per call from scoped
config, never process-global import-time defaults. Hermes core owns URL safety,
extraction truncation, tool policy and result processing. Extraction is Markdown;
deep crawl, browser automation and screenshots are not implemented.

## Example profile configuration

```yaml
web:
  search_backend: omniroute
  extract_backend: omniroute
  omniroute:
    base_url: https://YOUR-GATEWAY/v1
    search_provider: keenable
    fetch_provider: keenable-search
```

Use the profile's existing OMNIROUTE_API_KEY through Hermes secret management.
The gateway key must permit models `search` and `web-fetch`, endpoint categories
`search` and `web-fetch`, and the selected provider connection. Preserve unrelated
key restrictions and privacy settings. Plugin enablement stays in local profile
config; managed list leaves replace the user's entire list.

Install web source under ~/.hermes/plugins/web/omniroute, then native
`hermes plugins enable web/omniroute`. Model-provider and image plugin locations
remain unchanged. New modules do not replace existing registrations.

Before activation, retain owner-private original config and permission fields.
Rollback restores only changed web settings and native plugin selection, and
removes only appended key permissions after checking concurrent edits. Do not
restore whole configs over unrelated user changes. No direct credential deletion
until acceptance and explicit credential-retirement approval.

## Acceptance

Offline tests exercise wire contracts, failures, redirects and credential scope.
Live pilot uses native Hermes web_search/web_extract with public content and
checks selected backend and provider provenance. Multiple URLs, partial failure,
private URL rejection and successful fresh-chat behavior are separate gates.
No paid image/voice/model test is implied. Existing image behavior is preserved
source, not newly live-tested merely because it is in this repository.
