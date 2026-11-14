"""Declarative Hermes provider; transport and catalog fetching stay upstream-native."""
from providers import register_provider
from providers.base import ProviderProfile

class OmniRouteProfile(ProviderProfile):
    def get_model_context_length(self, model):
        from agent.model_metadata import _resolve_custom_endpoint_context_length
        from agent.secret_scope import get_secret_str
        return _resolve_custom_endpoint_context_length(
            model, self.base_url, get_secret_str("OMNIROUTE_API_KEY", ""), self.name)


register_provider(OmniRouteProfile(
    name="omniroute",
    display_name="OmniRoute",
    description="Profile-scoped OmniRoute gateway",
    env_vars=("OMNIROUTE_API_KEY", "OMNIROUTE_BASE_URL"),
    base_url="",
    auth_type="api_key",
    api_mode="chat_completions",
    aggregator=True,
    fallback_models=(),
    default_aux_model="auto/fast",
))
