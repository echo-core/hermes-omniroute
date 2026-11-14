# Hermes OmniRoute images

Native user plugin for Hermes0.23.0. Regular requests use the built-in
`image_generate` tool through this backend; `image_generate_complex` explicitly
selects Sunburst. No built-in tools or vendor files are overridden.

```yaml
image_gen:
  provider: omniroute-images
  model: codex/gpt-image-2.5-flare
  omniroute-images:
    base_url: https://gateway.example/v1
    key_env: OMNIROUTE_API_KEY
    regular_model: codex/gpt-image-2.5-flare
    complex_model: codex/gpt-image-2.5-sunburst
    timeout: 180
    regular_fallback_to_complex: true
```

Uses the existing scoped gateway key, native image file loaders, SDK multipart
edits, cache/delivery helpers and usage recorder. The two role IDs are explicit
user choices, not a copied upstream model catalog. No paid API or direct OAuth
fallback exists. Complex never downgrades. Regular may escalate once after a
definite HTTP429 rejection, within the shared deadline. Timeouts/other ambiguous
errors never retry. Requested geometry remains advisory at the upstream backend.

Install as an profile-owner-owned directory under each intended profile's
`plugins/hermes-omniroute-images`, then use native `hermes plugins enable`.
The `openai` SDK is already part of Hermes's image runtime; do not pip-install
dependencies into its managed environment. Test native helper contracts on
upgrade before changing the pinned deployed copy. No automatic update process.
