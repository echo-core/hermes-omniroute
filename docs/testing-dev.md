# Test the development version

`main` is stable. `dev` contains work being tested before the next stable release.
Testing `dev` is optional and does not require changing your everyday profile.

Hermes 0.23.1 installs a specific commit, not a branch name. You choose a checked
commit from `dev`; the installation stays on that commit until you change it.

## 1. Create a separate test profile

Run as the user who owns Hermes:

```sh
hermes profile create omniroute-test --no-alias
```

Use another name if that profile already exists, or reuse your existing test
profile. This creates a fresh profile; configure its own gateway URL and key
using the [configuration guide](../README.md#configure). Do not copy another
person's credentials. All commands below explicitly select the test profile.

## 2. Choose a checked development commit

Open the [dev commit history](https://github.com/echo-core/hermes-omniroute/commits/dev/).
Choose a commit whose Crow checks passed, open it and copy its full 40-character
commit SHA. Use the same SHA for both plugins so they come from one snapshot.

On the Hermes machine, replace the placeholder below with that SHA:

```sh
DEV_COMMIT_SHA='<paste the full 40-character dev commit SHA>'
hermes --profile omniroute-test plugins install 'echo-core/hermes-omniroute#model-provider' --ref "$DEV_COMMIT_SHA" --enable
hermes --profile omniroute-test plugins install 'echo-core/hermes-omniroute#backend' --ref "$DEV_COMMIT_SHA" --enable
```

Review Hermes's dependency and permission prompts. `--enable` does not bypass
those decisions. `--ref dev`, release tag names and shortened SHAs are not
accepted. This is a fixed test snapshot, not an automatically moving dev channel.

## 3. Check the installation and try the change

```sh
hermes --profile omniroute-test plugins list
hermes --profile omniroute-test chat
```

Confirm both plugins appear, with the pin identifying your chosen commit. Start
with a basic chat using OmniRoute Auto, then test the behavior the change affects:
search and reading a page, image generation, reference-image editing, or another
specific fix. Compare the result with what the change says it should do. Live
requests use your gateway's providers and quota; a basic code install makes no
model-generation request by itself.

If a gateway serves the test profile, restart its owning runtime after changing
the model provider, once active work is finished. Start a fresh chat afterward.
Backend-only updates can use Hermes's normal reload behavior.

Report the commit SHA, Hermes and OmniRoute versions, what you tried, what you
expected, and the actual result or error. Do not include keys, tokens or private
chat content in a public report.

## 4. Try a newer development commit

Copy the newer full SHA, set `DEV_COMMIT_SHA` again, and reinstall the same sources:

```sh
hermes --profile omniroute-test plugins install 'echo-core/hermes-omniroute#model-provider' --force --ref "$DEV_COMMIT_SHA" --enable
hermes --profile omniroute-test plugins install 'echo-core/hermes-omniroute#backend' --force --ref "$DEV_COMMIT_SHA" --enable
```

Review the commit before using `--force`: it replaces the existing plugin and can
accept caution-level scan findings; dangerous findings still block installation.
Hermes's dependency and capability checks still apply. Ordinary **Update** does
not move these custom pinned installs. Restart/reopen as described above.

## 5. Return to stable

If you used the separate test profile, return to your everyday profile. Its
plugins and ordinary stable updates were never changed by these commands.

To put stable code in the test profile too, open the desired stable release,
follow its commit link and copy the full SHA. Replace the placeholder and run:

```sh
STABLE_COMMIT_SHA='<paste the full 40-character stable release commit SHA>'
hermes --profile omniroute-test plugins install 'echo-core/hermes-omniroute#model-provider' --force --ref "$STABLE_COMMIT_SHA" --enable
hermes --profile omniroute-test plugins install 'echo-core/hermes-omniroute#backend' --force --ref "$STABLE_COMMIT_SHA" --enable
```

Restart/reopen as above. This returns the code to that stable snapshot but leaves
it pinned. Reinstalling without `--ref` does not clear an existing pin in Hermes
0.23.1. To resume ordinary moving updates in that profile, use native plugin
removal and reinstall without `--ref`; removal clears plugin configuration
references, so follow the configuration guide again. Prefer the separate test
profile so your everyday setup does not need that reset.

See the official [plugin](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins)
and [profile](https://hermes-agent.nousresearch.com/docs/reference/profile-commands)
guides for Hermes's pinning, installation and profile behavior.
