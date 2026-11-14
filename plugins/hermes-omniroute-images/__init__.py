"""Native Hermes image backend with explicit Regular and Complex roles."""

from __future__ import annotations

from contextlib import ExitStack
import json
import logging
import time
from urllib.parse import urlsplit

from agent.image_gen_provider import ImageGenProvider, error_response, resolve_aspect_ratio, success_response
from plugins.image_gen._common import collect_source_images, materialize_image, record_token_usage, size_for
from plugins.image_gen.openai import _named_bytes_io

log = logging.getLogger(__name__)
PROVIDER = "omniroute-images"
REGULAR = "codex/gpt-image-2.5-flare"
COMPLEX = "codex/gpt-image-2.5-sunburst"


def settings():
    from hermes_cli.config import load_config_readonly
    return load_config_readonly().get("image_gen", {}).get(PROVIDER, {})


class OmniRouteImages(ImageGenProvider):
    @property
    def name(self):
        return PROVIDER

    @property
    def display_name(self):
        return "OmniRoute Regular (Flare)"

    def default_model(self):
        return settings().get("regular_model", REGULAR)

    def list_models(self):
        cfg = settings()
        return [{"id": cfg.get("regular_model", REGULAR), "display": "Regular — Flare (Complex has its own tool)"}]

    def capabilities(self):
        return {"modalities": ["text", "image"], "max_reference_images": 16}

    def is_available(self):
        from agent.secret_scope import get_secret
        return bool(get_secret(settings().get("key_env", "OMNIROUTE_API_KEY")))

    def generate(self, prompt, aspect_ratio="landscape", *, image_url=None,
                 reference_image_urls=None, mode="regular", **kwargs):
        from agent.secret_scope import get_secret
        from openai import OpenAI, RateLimitError

        cfg = settings()
        aspect = resolve_aspect_ratio(aspect_ratio)
        prompt = (prompt or "").strip()
        model = cfg.get("complex_model", COMPLEX) if mode == "complex" else cfg.get("regular_model", REGULAR)

        def fail(message, kind):
            return error_response(error=message, error_type=kind, provider=PROVIDER,
                                  model=model, prompt=prompt, aspect_ratio=aspect)

        if not prompt:
            return fail("An image prompt is required.", "invalid_argument")
        if mode not in ("regular", "complex"):
            return fail("Image mode must be regular or complex.", "invalid_argument")
        base_url = str(cfg.get("base_url", "")).rstrip("/")
        url = urlsplit(base_url)
        if url.scheme != "https" or not url.hostname or not url.path.endswith("/v1") or url.username or url.query or url.fragment:
            return fail("The configured OmniRoute image endpoint is invalid.", "configuration_error")
        key = get_secret(cfg.get("key_env", "OMNIROUTE_API_KEY"))
        if not key:
            return fail("The scoped OmniRoute image credential is unavailable.", "auth_required")
        try:
            timeout = min(180.0, max(1.0, float(cfg.get("timeout", 180))))
        except (TypeError, ValueError):
            return fail("The image deadline must be numeric.", "configuration_error")
        deadline = time.monotonic() + timeout
        sources = collect_source_images(image_url, reference_image_urls, limit=16)
        attempted = []
        try:
            with OpenAI(api_key=key, base_url=base_url, timeout=timeout, max_retries=0,
                        default_headers={"OpenAI-Project": ""}) as client, ExitStack() as stack:
                files = [stack.enter_context(_named_bytes_io(ref)) for ref in sources]
                request = {"model": model, "prompt": prompt, "size": size_for(aspect), "n": 1}
                if files:
                    request["image"] = files if len(files) > 1 else files[0]
                while True:
                    attempted.append(model)
                    request["model"] = model
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return fail("Image deadline reached; no duplicate request was submitted.", "timeout")
                    call = client.images.edit if files else client.images.generate
                    try:
                        response = call(**request, timeout=remaining)
                        break
                    except RateLimitError:
                        fallback = cfg.get("regular_fallback_to_complex", True)
                        if mode != "regular" or not fallback or len(attempted) != 1:
                            raise
                        model = cfg.get("complex_model", COMPLEX)
                        for file in files:
                            file.seek(0)
            record_token_usage(getattr(response, "usage", None), model=model, provider="omniroute")
            data = getattr(response, "data", None) or []
            if not data:
                return fail("OmniRoute returned no image data.", "empty_response")
            first = data[0]
            image, error = materialize_image(
                getattr(first, "b64_json", None), getattr(first, "url", None),
                prefix="omniroute_" + mode, label="OmniRoute", provider=PROVIDER,
                model=model, prompt=prompt, aspect=aspect, log=log)
            if error:
                return error
            return success_response(
                image=image, model=model, prompt=prompt, aspect_ratio=aspect,
                provider=PROVIDER, modality="image" if sources else "text",
                extra={"mode": mode, "attempted_models": attempted, "requested_size": size_for(aspect)})
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            kind = "timeout" if "timeout" in type(exc).__name__.lower() else "api_error"
            message = "OmniRoute image request failed"
            if status is not None:
                message += f" (HTTP {status})"
            return fail(message + ". No automatic retry after ambiguous failures.", kind)


def register(ctx):
    provider = OmniRouteImages()
    ctx.register_image_gen_provider(provider)

    def complex_image(args, **context):
        from tools.image_generation_tool import _postprocess_image_generate_result
        result = provider.generate(
            args.get("prompt", ""), args.get("aspect_ratio", "landscape"),
            image_url=args.get("image_url"), reference_image_urls=args.get("reference_image_urls"),
            mode="complex")
        return _postprocess_image_generate_result(json.dumps(result), task_id=context.get("task_id"))

    ctx.register_tool(
        name="image_generate_complex", toolset="image_gen", handler=complex_image,
        check_fn=provider.is_available, requires_env=[],
        schema={"name": "image_generate_complex", "description": (
            "Generate or edit a complex image with OmniRoute Sunburst. Use for demanding composition, "
            "precise typography, final quality or difficult preservation edits. Respect an explicit complex "
            "request. For routine images use image_generate (Regular/Flare). Returns the same native image "
            "attachment as image_generate; never change config or call image APIs from the terminal."),
            "parameters": {"type": "object", "properties": {
                "prompt": {"type": "string"},
                "aspect_ratio": {"type": "string", "enum": ["landscape", "square", "portrait"]},
                "image_url": {"type": "string", "description": "Optional local path, HTTPS URL or data URI to edit."},
                "reference_image_urls": {"type": "array", "items": {"type": "string"}, "maxItems": 15},
            }, "required": ["prompt"]}})
