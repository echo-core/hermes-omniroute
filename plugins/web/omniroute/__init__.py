"""Register only the web capability; model/image/voice registrations stay separate."""
from .provider import OmniRouteWebProvider


def register(ctx):
    ctx.register_web_search_provider(OmniRouteWebProvider())
