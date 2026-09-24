"""Small shared HTTP helper: timeouts, safe errors, no secrets in messages."""

import httpx

from ..errors import ProviderError

TIMEOUT = httpx.Timeout(20.0, connect=10.0)


def request(client, method, url, *, provider, ok=(200, 201, 202, 204), **kwargs):
    """Send and return the response. Provider bodies are never copied into errors."""
    try:
        response = client.request(method, url, **kwargs)
    except httpx.ConnectError:
        # Never reached the provider: the action definitely did not happen.
        raise ProviderError(f"{provider} could not be reached.", retriable=True,
                            provider_code="connect") from None
    except httpx.TimeoutException:
        # The request may have reached the provider: callers must treat this as unknown.
        raise ProviderError(f"{provider} did not respond in time.", retriable=True,
                            provider_code="timeout") from None
    except httpx.HTTPError:
        raise ProviderError(f"{provider} could not be reached.", retriable=True,
                            provider_code="network") from None
    if response.status_code not in ok:
        code = ""
        try:
            body = response.json()
            code = str(body.get("code") or body.get("name") or "")[:60] if isinstance(body, dict) else ""
        except ValueError:
            pass
        raise ProviderError(
            f"{provider} returned HTTP {response.status_code}.",
            retriable=response.status_code in (408, 429) or response.status_code >= 500,
            provider_code=code or str(response.status_code),
        )
    return response
