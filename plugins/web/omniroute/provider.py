"""Native Hermes web provider for OmniRoute's search and fetch endpoints.

No provider credentials live here. The existing per-profile OmniRoute key is
resolved through Hermes's scope-aware provider_env helper for every operation.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from plugins.web._common import BaseWebSearchProvider, provider_env

MAX_RESULTS = 50
MAX_URLS = 20
SNIPPET_CHARS = 500


def _settings() -> dict:
    from hermes_cli.config import load_config_readonly
    web = load_config_readonly().get("web", {})
    settings = web.get("omniroute", {}) if isinstance(web, dict) else {}
    return settings if isinstance(settings, dict) else {}


def _base_url(settings: dict) -> str:
    value = str(settings.get("base_url", "")).strip().rstrip("/")
    url = urlsplit(value)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or not url.path.endswith("/v1")):
        raise ValueError("Configure web.omniroute.base_url as a trusted HTTPS URL ending in /v1")
    return value


class GatewayError(Exception):
    """Credential-free error safe to return to the model."""


class OmniRouteWebProvider(BaseWebSearchProvider):
    NAME = "omniroute"
    DISPLAY_NAME = "OmniRoute"
    KEY_ENV = "OMNIROUTE_API_KEY"
    EXTRACT = True

    def is_available(self) -> bool:
        try:
            return bool(provider_env(self.KEY_ENV)) and bool(_base_url(_settings()))
        except Exception:
            # A missing profile scope must never select the launch profile's key.
            return False

    def _post(self, path: str, payload: dict) -> dict:
        import requests
        from agent.ssl_verify import requests_verify

        settings = _settings()
        try:
            base = _base_url(settings)
            key = provider_env(self.KEY_ENV)
        except Exception:
            raise GatewayError("OmniRoute configuration or profile credentials unavailable") from None
        if not key:
            raise GatewayError("OMNIROUTE_API_KEY is not configured for this profile")
        try:
            response = requests.post(
                base + path, json=payload,
                headers={"Authorization": "Bearer " + key,
                         "Content-Type": "application/json",
                         "User-Agent": "echo-core-hermes-omniroute/0.1.0"},
                timeout=(10, 40), allow_redirects=False, verify=requests_verify(),
            )
        except requests.Timeout:
            raise GatewayError("OmniRoute request timed out; no retry was submitted") from None
        except requests.RequestException:
            raise GatewayError("Could not reach OmniRoute") from None
        try:
            if response.status_code != 200:
                raise GatewayError("OmniRoute returned HTTP " + str(response.status_code))
            try:
                body = response.json()
            except ValueError:
                raise GatewayError("OmniRoute returned invalid JSON") from None
            if not isinstance(body, dict) or body.get("error"):
                raise GatewayError("OmniRoute returned an invalid response")
            return body
        finally:
            response.close()

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip():
            return {"success": False, "error": "A nonempty search query is required"}
        try:
            count = min(MAX_RESULTS, max(1, int(limit)))
            settings = _settings()
            payload = {"query": query.strip(), "max_results": count}
            if provider := settings.get("search_provider"):
                payload["provider"] = provider
            body = self._post("/search", payload)
            results = body.get("results")
            if not isinstance(results, list):
                raise GatewayError("OmniRoute returned no search result list")
            web = []
            for row in results:
                if not isinstance(row, dict) or not isinstance(row.get("url"), str):
                    continue
                if urlsplit(row["url"]).scheme not in ("http", "https"):
                    continue
                web.append({"title": str(row.get("title") or ""), "url": row["url"],
                            "description": str(row.get("snippet") or row.get("description") or "")[:SNIPPET_CHARS],
                            "position": len(web) + 1})
                if len(web) == count:
                    break
            return {"success": True, "data": {"web": web}, "provider": body.get("provider")}
        except GatewayError as exc:
            return {"success": False, "error": str(exc)}
        except (ValueError, TypeError):
            return {"success": False, "error": "Invalid search input or response"}

    def extract(self, urls: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        if isinstance(urls, str):
            urls = [urls]
        if len(urls) > MAX_URLS:
            return [self._error(url, "Extract at most 20 URLs per call") for url in urls]
        try:
            settings = _settings()
        except Exception:
            return [self._error(url, "OmniRoute configuration unavailable") for url in urls]
        results = []
        for url in urls:
            try:
                if not isinstance(url, str) or urlsplit(url).scheme not in ("http", "https"):
                    raise GatewayError("An HTTP(S) page URL is required")
                # Hermes core validates private/internal URLs before provider dispatch.
                # Keenable's native output is Markdown; other formats are not forwarded.
                payload = {"url": url, "format": "markdown", "include_metadata": True}
                if provider := settings.get("fetch_provider"):
                    payload["provider"] = provider
                body = self._post("/web/fetch", payload)
                content = body.get("content")
                if not isinstance(content, str) or not content.strip():
                    raise GatewayError("OmniRoute returned no extractable content")
                metadata = dict(body["metadata"]) if isinstance(body.get("metadata"), dict) else {}
                metadata["provider"] = body.get("provider")
                results.append({"url": url, "title": str(metadata.get("title") or ""),
                                "content": content, "raw_content": content, "metadata": metadata})
            except GatewayError as exc:
                results.append(self._error(url, str(exc)))
            except (TypeError, ValueError):
                results.append(self._error(url, "Invalid extraction input or response"))
        return results

    @staticmethod
    def _error(url: str, message: str) -> dict:
        return {"url": url, "title": "", "content": "", "raw_content": "", "error": message}
