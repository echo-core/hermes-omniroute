"""Bounded image-routing context owned by this backend's native lifecycle."""

from pathlib import Path

from .images import PROVIDER, OmniRouteImages

SECTION_ID = "omniroute.image-routing"
IMAGE_GUIDANCE = """OmniRoute is this profile's configured image-generation backend.
For routine image creation or editing, prefer image_generate (Regular). For an
explicit Complex request or demanding composition, typography or preservation,
prefer image_generate_complex. These tools use the configured models and this
profile's existing gateway access. Unless the user explicitly requests a different
image service, use these native tools rather than discovering another image
connector or starting another service sign-in.
If deferred, load the exact name with tool_describe, then invoke it with tool_call;
do not treat absence from the initial tool list as unavailable. If it is actually
unavailable or denied in this session, report that blocker; do not bypass tool
restrictions, approvals, change configuration or make direct API calls.
Use the native returned image attachment and preserve the user's reference images."""


def register(ctx):
    from hermes_constants import get_hermes_home

    registered_home = Path(get_hermes_home()).resolve()

    def render(session_info):
        from hermes_cli.config import load_config_readonly

        # Resolve at render time; a warm worker must not reuse another profile's state.
        if Path(get_hermes_home()).resolve() != registered_home:
            return ""
        try:
            config = load_config_readonly()
            if config.get("image_gen", {}).get("provider") != PROVIDER:
                return ""
            if not OmniRouteImages().is_available():
                return ""
        except Exception:
            return ""
        return IMAGE_GUIDANCE

    ctx.register_system_prompt_section(SECTION_ID, render, position="after_memory", max_chars=1200)
