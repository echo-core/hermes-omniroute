# Model exports from a backend plugin

This is a local upstream proposal. The manifest field below is proposed and is
unsupported on the inspected Hermes v0.23.1 revision
`3b421578588243f920886e6fe79a6faa87ea026e`. No production runtime patch is included.

## Reproduced problem

A plugin that owns model identity/context hooks and image/web capabilities needs
one install and one UI entry. The documented model loader imports model-provider
manifests, while PluginManager records them as placeholders and never invokes
`register(ctx)`. A backend manifest follows the other lifecycle.

`tests/native_lifecycle_probe.py` creates a local Git fixture, installs it through
`dashboard_install_plugin`, resolves the model before backend startup, and then
runs normal `get_plugin_manager().discover_and_load()`. Two disposable profile
homes bind distinct dummy credentials. The fixture contains a model declaration
and a harmless web registration under one model-provider manifest.

Observed in both `in_process` and `host` isolation on the inspected build:

```json
{"native_install": true, "inventory_rows": 1, "model_a": true,
 "web_a": false, "model_b": false, "web_b": false, "supported": false}
```

These are the manager inventory rows used by native management, not a rendered UI
check. The first missing capability prevents acceptance of this installation.
Update, disable, re-enable and uninstall of a working combined plugin remain
untested. Temporary repositories/homes are removed by the fixture context manager.
The test makes no provider calls and requires the installed Hermes runtime.

Run the fixture using Hermes's managed Python/bootstrap. Set
`OMNIROUTE_PROBE_ISOLATION=host` for the second execution mode. Running it with a
plain Python environment without Hermes is unsupported. Exit zero means the known
failure was reproduced, not that the architecture passed.

Earlier isolated probes also rejected direct `register_provider` from a backend.
In-process, it escaped the per-home model layer and survived backend unload. In
host isolation the parent never received it. A source-inspected pip entry point
is classified into a single kind by core and does not establish a combined
lifecycle either.

## Proposed native extension

Add an optional model-export declaration to an ordinary backend manifest. Keep
existing model-only manifests and backend registration APIs working.

```yaml
name: hermes-omniroute
kind: backend
model_provider_entry: models:get_model_providers
provides_web_providers:
  - omniroute
provides_tools:
  - image_generate_complex
```

`model_provider_entry` is illustrative syntax pending upstream design review.
The referenced zero-argument function returns a list of `ProviderProfile`
instances. The plugin root's existing `register(ctx)` supplies backend capabilities.

1. The native model loader recognizes an enabled backend with this declaration
   during early provider lookup. It imports the export through its existing
   per-home module namespace and registers the returned profiles in that home's
   layer. Validate the module/symbol reference within the admitted plugin package.
   Do not import a backend entry point just to discover models.
2. Admission, recall/trust, safe mode, dependency readiness and enable/disable
   decisions apply to the same installed identity in both loaders. Reuse core's
   native model execution policy. If a policy forbids the model export, expose a
   clear unavailable state. Host execution requires proving the model hooks have
   the same allowed execution semantics as existing native model plugins.
3. Keep one inventory/UI entry. Show both model and backend failures on it. Track
   export ownership by home and installed plugin identity. A failed export or
   backend startup must expose partial failure and unwind newly owned registrations
   before reporting the combined plugin ready.
4. Reuse native update/remove and forced-sweep invalidation for the complete
   package, its submodules, model rows and owned backend ledger. Retain unrelated
   providers when disabling, uninstalling or rolling back. Concurrent model lookups
   must never see another home's export or a half-replaced registration set.

The implementation should live upstream in manifest parsing/validation, native
provider discovery and plugin activation/inventory. A public PluginContext model
registrar alone would miss early model lookups and isolated-host export delivery.
The plugin repository itself should contain only the supported exports/adapters.

## Required upstream acceptance

- Existing model-only and backend-only plugins still load under the same policies.
- One native install yields model, image, web and Complex tool availability with
  one actual UI row. Verify early model lookup and backend-first startup.
- Two homes sharing one gateway use independent keys, catalogs and context values,
  both cold and warm. Missing credentials do not borrow the launch profile's key.
- Native update, disable/re-enable and uninstall affect only the owning profile.
- Failed admission/export/backend initialization, interrupted update and concurrent
  discovery leave coherent state. Repeat under supported isolation modes.

## Sources

- [Official model provider lifecycle](https://hermes-agent.nousresearch.com/docs/developer-guide/model-provider-plugin#general-pluginmanager-integration)
- [Official web registration](https://hermes-agent.nousresearch.com/docs/developer-guide/web-search-provider-plugin#how-discovery-works)
- [Official public plugin API](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/public-api)
