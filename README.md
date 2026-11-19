# Hermes OmniRoute

[![Build Status](https://ci.echols.co/api/v1/badges/5/status.svg)](https://ci.echols.co/repos/5)

Use your OmniRoute gateway for Hermes chat models, image generation, web search
and page extraction. Your gateway manages provider accounts and routing; Hermes
uses the gateway URL and API key you configure for each profile.

This repository installs **two plugins**:

| Plugin | What it does |
| --- | --- |
| `hermes-omniroute-provider` | Adds OmniRoute to the model picker, loads the gateway's model list and discovers context limits automatically. |
| `hermes-omniroute-backend` | Adds Regular and Complex image generation, web search and page extraction. |

Hermes currently loads model providers separately from plugins that provide
tools. The [unified plugin roadmap](docs/design.md#unified-plugin-roadmap)
describes how the two packages will become one once Hermes ships support.

Tested with **Hermes v0.23.1** and **OmniRoute 3.8.54**. Voice and video are outside
this integration's current capabilities.

See the [changelog](CHANGELOG.md) and [first-release notes](docs/releases/v0.1.0.md)
for the included plugin versions, fixes and compatibility limits.

## Install

Before starting, have these ready:

- A working Hermes installation with plugin support.
- Your OmniRoute HTTPS API URL, including `/v1`.
- An OmniRoute API key allowed to use the models and endpoints you want.

Run these commands on the machine running Hermes, as the user who owns the
profile:

```sh
hermes plugins install 'echo-core/hermes-omniroute#model-provider' --enable
hermes plugins install 'echo-core/hermes-omniroute#backend' --enable
```

Review any installation prompts. `--enable` requests enablement; it does not
bypass dependency or capability consent. If the repository is private, the
installing user also needs GitHub access.

The commands apply to the current Hermes profile. For a named profile, add
`--profile work` before `plugins`, replacing `work` with its name. For example:

```sh
hermes --profile work plugins install 'echo-core/hermes-omniroute#backend' --enable
```

Install both plugins in each profile that should use them. Then configure that
profile as described below.

## Configure

### 1. Set your gateway key

Set **`OMNIROUTE_API_KEY`** to your gateway key using Hermes's secret settings or
the selected profile's `.env` file. You can also enter it if Hermes prompts during
installation. Both plugins use this key, resolved for the current profile.

For the default profile, the file is `~/.hermes/.env`. A named profile uses its own
`~/.hermes/profiles/<name>/.env`. Keep each user's key in their own profile.

### 2. Configure the features you want

Merge the following settings into your profile's `config.yaml`. Replace all three
example URLs with your OmniRoute API URL. Include only the feature sections you
want to route through the gateway.

The default profile's config is `~/.hermes/config.yaml`; a named profile uses
`~/.hermes/profiles/<name>/config.yaml`.

```yaml
model:
  provider: omniroute
  default: auto
  base_url: https://omniroute.example.com/v1
  api_mode: chat_completions

image_gen:
  provider: omniroute-images
  omniroute-images:
    base_url: https://omniroute.example.com/v1
    regular_model: codex/gpt-image-2.5-flare
    complex_model: codex/gpt-image-2.5-sunburst
    timeout: 180
    regular_fallback_to_complex: true

web:
  backend: omniroute
  search_backend: omniroute
  extract_backend: omniroute
  keyless_rescue: false
  omniroute:
    base_url: https://omniroute.example.com/v1
```

The model provider also supplies this endpoint to Hermes's native auxiliary
clients, including vision and title generation. They use the current profile's
key and configured gateway URL; no separate endpoint or fixed upstream model is
needed. Keep your existing `auxiliary` model selections such as `auto/vision`.

The image models shown above are the current defaults. They require a working
Codex connection in OmniRoute and access through your gateway key. You can replace
them with image model IDs supported by your gateway's OpenAI-compatible image API.

For web access, your key must allow `search` and `web-fetch`, the corresponding
endpoints and the upstream connections OmniRoute will use. Leave
`search_provider` and `fetch_provider` unset so OmniRoute selects from the
providers available to your key. `keyless_rescue: false` keeps a failed gateway
request from falling back to a direct web provider in Hermes.

On managed installations, administrators can place common settings in
`/etc/hermes/config.yaml`. Managed values override local settings; keys should
remain separate for each user. Keep plugin enablement local to each profile.

### Image-tool guidance

When OmniRoute is selected and available, the backend adds a short native prompt
section directing image requests to Regular or Complex, including how to load
those tools when Hermes defers them behind `tool_search`. An explicitly requested
alternative service still takes precedence. The section does not grant tools,
bypass approvals or change provider/model settings.

The section follows the active profile and is removed from new prompt builds
when the backend is disabled or uninstalled. Existing conversations keep their
saved prompt; start a fresh chat or use Hermes's native session prompt refresh
when adopting an update. Disabling a plugin does not rewrite saved conversations.

### 3. Restart and check

Restart the gateway after the initial setup, once active work has finished:

```sh
hermes gateway restart
hermes plugins list
```

If your deployment manages Hermes through an existing service, use its normal
restart procedure. Check that both plugins are enabled in the intended profile,
then open a chat and select **OmniRoute → Auto** in the model picker.

## Use

### Chat models

Chat normally with Auto selected. OmniRoute chooses the underlying provider and
model according to your gateway's routing configuration. Other gateway models or
groups, such as `auto/fast` and `auto/gpt`, appear when your key can access them.
The plugin reads the gateway's catalog and context metadata; you do not need to
maintain a separate chat-model list or set a manual context limit.

### Images

Regular uses Hermes's `image_generate` tool. Complex uses the additional
`image_generate_complex` tool for detailed composition, text or demanding edits.
Both support reference images. PNG, JPEG and WebP uploads declare their actual
content type, even if an attachment's filename has the wrong extension. The
original image bytes are preserved; the plugin does not convert or rename source
files. You can ask Hermes explicitly:

> Use image_generate to create a simple illustration of a lighthouse at sunset.

> Use image_generate_complex to create a labeled diagram of a home network.

With the example settings, Regular can try the Complex model once after a definite
HTTP 429 rate-limit rejection, within the same 180-second deadline. Set
`regular_fallback_to_complex: false` to disable that escalation. Complex never
falls back to Regular. Timeouts and ambiguous failures are not automatically
resubmitted. Requested aspect ratios are advisory; the returned size depends on
the upstream model.

### Search and page extraction

Use the normal Hermes web tools. For example:

> Use web_search to find the official Python tutorial, then use web_extract to
> summarize its introduction. Include the source URL.

Search goes through OmniRoute's `/v1/search` endpoint. Page extraction uses
`/v1/web/fetch` and returns Markdown. Your gateway chooses the upstream provider;
there is no need to configure a separate search-provider key in Hermes.

## Updating

`main` is the stable channel. Development flows through feature branches into
`dev` (bleeding edge), then reaches `main` only when accepted for release. Standard
Git installs follow the repository's default branch, `main`. Release tags identify
those accepted snapshots; updates do not wait for GitHub release publication.

Bleeding-edge testing is an explicit choice: use the full commit SHA from `dev`
with Hermes's `--ref` option. That install stays pinned until deliberately changed;
`--ref dev` and release tag names are not supported in Hermes 0.23.1.
See [Test the development version](docs/testing-dev.md) for choosing a checked
commit, using a separate test profile, trying updates and returning to stable.

Update both plugins in the same profile where you installed them:

```sh
hermes plugins check-updates
hermes plugins update hermes-omniroute-provider
hermes plugins update hermes-omniroute-backend
```

You can also use each plugin's **Update** action in Hermes's plugin settings.
Hermes remembers the installation source and fetches updates from it. No custom
updater or `update_url` setting is needed for a normal GitHub install.

After a model-provider update, restart the owning gateway so its loaded model
registry uses the new code. Open a fresh chat to check the picker and tools.
Backend-only updates support Hermes's native reload. Review installation notes
when upgrading Hermes itself, because this integration uses some version-sensitive
Hermes helpers.

Updates are manual by default; unattended updates are a separate Hermes setting.
If you installed with `--ref`, that commit stays pinned until you deliberately
choose another revision. Private GitHub sources need continued repository access;
a local Git source updates from that local repository, not directly from GitHub.

## Troubleshooting

- **A plugin or tool is missing:** check the selected profile and enable both
  plugins. If you maintain a custom Hermes tool selection, include the image and
  web tools you want to use.
- **HTTP 401 or 403:** check the profile's key and its OmniRoute permissions for
  the requested models, endpoints and provider connections.
- **The model picker is stale:** restart the gateway after a model-provider
  change and reopen the picker.
- **A setting will not change:** check whether it is pinned by managed config.

For development, run `uv sync --locked`; dependencies are pinned in `uv.lock`.
For tests and frozen prek checks, see
[CONTRIBUTING.md](CONTRIBUTING.md). The [design notes](docs/design.md) cover the
native integration, profile isolation, migration and rollback behavior.
