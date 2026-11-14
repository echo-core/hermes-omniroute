"""Native OmniRoute image and web backends under one plugin identity."""


def register(ctx):
    from .images import register as register_images
    from .web import OmniRouteWebProvider

    register_images(ctx)
    ctx.register_web_search_provider(OmniRouteWebProvider())
