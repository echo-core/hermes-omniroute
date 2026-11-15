"""Verify actual SDK multipart uploads with synthetic files and zero upstream calls.

Run through Hermes's managed runtime. Files, profile and HTTP responses are local
fixtures; the HTTP transport accepts only the synthetic gateway edit URL.
"""

import hashlib
import importlib.util
import io
import json
import sys
import tempfile
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch


def main():
    import httpx
    import openai
    from agent.secret_scope import reset_secret_scope, set_secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from PIL import Image

    with tempfile.TemporaryDirectory(prefix="hermes-image-upload-") as directory:
        home = Path(directory)
        token = set_hermes_home_override(home)
        secret = set_secret_scope({"OMNIROUTE_API_KEY": "fixture-only"}, profile_home=str(home))
        try:
            fixtures = []
            for format, suffix, mime in (
                ("JPEG", "png", "image/jpeg"),
                ("WEBP", "png", "image/webp"),
                ("PNG", "jpg", "image/png"),
            ):
                buffer = io.BytesIO()
                Image.new("RGB", (8, 8), "blue").save(buffer, format=format)
                data = buffer.getvalue()
                path = home / (format.lower() + "." + suffix)
                path.write_bytes(data)
                fixtures.append((path, mime, data))
            from hermes_cli.plugin_validate import validate_plugin_dir

            validation = validate_plugin_dir(Path(__file__).resolve().parents[1] / "backend")
            assert validation.ok, validation.failures
            spec = importlib.util.spec_from_file_location(
                "native_image_upload_candidate",
                Path(__file__).resolve().parents[1] / "backend/images.py",
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            module.settings = lambda: {"base_url": "https://gateway.example/v1", "timeout": 180}
            module.materialize_image = lambda *a, **kw: (str(home / "synthetic-result.png"), None)
            module.record_token_usage = lambda *a, **kw: None
            real_openai = openai.OpenAI
            native_file = module._named_bytes_io
            opened = []
            seen = []
            expected = []

            def capture_file(ref):
                file = native_file(ref)
                opened.append(file)
                return file

            def respond(request):
                assert str(request.url) == "https://gateway.example/v1/images/edits"
                assert request.method == "POST"
                header = request.headers["content-type"]
                message = BytesParser(policy=policy.default).parsebytes(
                    ("Content-Type: " + header + "\r\nMIME-Version: 1.0\r\n\r\n").encode()
                    + request.read()
                )
                parts = [
                    part
                    for part in message.iter_parts()
                    if part.get_param("name", header="content-disposition") in ("image", "image[]")
                ]
                seen.append({"mimeTypes": [part.get_content_type() for part in parts]})
                assert len(parts) == len(expected)
                for part, (_, mime, data) in zip(parts, expected):
                    assert part.get_content_type() == mime, seen[-1]
                    assert part.get_payload(decode=True) == data
                return httpx.Response(200, json={"created": 1, "data": [{"b64_json": "fixture"}]})

            def client(**kwargs):
                assert kwargs["api_key"] == "fixture-only"  # pragma: allowlist secret
                assert kwargs["max_retries"] == 0
                return real_openai(
                    **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(respond))
                )

            with (
                patch.object(openai, "OpenAI", client),
                patch.object(module, "_named_bytes_io", capture_file),
            ):
                for mode in ("regular", "complex"):
                    for indexes in ((0,), (1,), (0, 2)):
                        expected = [fixtures[index] for index in indexes]
                        result = module.OmniRouteImages().generate(
                            "Synthetic multipart fixture",
                            reference_image_urls=[str(path) for path, _, _ in expected],
                            mode=mode,
                        )
                        assert result["success"], {"mode": mode, "observed": seen}
                        assert all(file.closed for file in opened)
            assert len(seen) == 6
            assert all(path.read_bytes() == data for path, _, data in fixtures)
            print(
                "NATIVE_IMAGE_UPLOAD "
                + json.dumps(
                    {
                        "sdkVersion": openai.__version__,
                        "nativeValidation": True,
                        "validationWarnings": len(validation.warnings),
                        "mockRequests": len(seen),
                        "mimeTypes": [item["mimeTypes"] for item in seen],
                        "bothModes": True,
                        "singleAndMultipleReferences": True,
                        "originalFilesPreserved": True,
                        "streamsClosed": True,
                        "payloadHashes": [
                            hashlib.sha256(data).hexdigest() for _, _, data in fixtures
                        ],
                        "upstreamCalls": 0,
                    }
                )
            )
        finally:
            reset_secret_scope(secret)
            reset_hermes_home_override(token)


if __name__ == "__main__":
    main()
