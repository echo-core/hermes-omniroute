"""Declarative Hermes provider; transport and catalog fetching stay upstream-native."""

from urllib.parse import urlsplit

from providers import register_provider
from providers.base import ProviderProfile


class OmniRouteProfile(ProviderProfile):
    def _configured_base_url(self):
        from agent.web_search_provider import get_provider_env
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
        provider_config = config.get("providers", {}).get(self.name, {})
        model_config = config.get("model", {})
        model_url = (
            model_config.get("base_url", "") if model_config.get("provider") == self.name else ""
        )
        base_url = (
            str(provider_config.get("api") or model_url or get_provider_env("OMNIROUTE_BASE_URL"))
            .strip()
            .rstrip("/")
        )
        url = urlsplit(base_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or not url.path.endswith("/v1")
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Configure a trusted OmniRoute HTTPS endpoint ending in /v1")
        return base_url

    def runtime_base_url(self, *, api_key, default, env_url):
        # Hermes resolves auxiliary credentials separately from model.base_url.
        return self._configured_base_url()

    def get_model_context_length(self, model):
        from agent.model_metadata import _resolve_custom_endpoint_context_length
        from agent.web_search_provider import get_provider_env

        base_url = self._configured_base_url()
        key = get_provider_env("OMNIROUTE_API_KEY")
        if not key:
            raise ValueError("The scoped OmniRoute credential is unavailable")
        return _resolve_custom_endpoint_context_length(model, base_url, key, self.name)


register_provider(
    OmniRouteProfile(
        name="omniroute",
        display_name="OmniRoute",
        description="Profile-scoped OmniRoute gateway",
        env_vars=("OMNIROUTE_API_KEY",),
        base_url="",
        base_url_env_var="OMNIROUTE_BASE_URL",
        auth_type="api_key",
        api_mode="chat_completions",
        aggregator=True,
        fallback_models=(),
        default_aux_model="auto/fast",
    )
)
