from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


class HAError(RuntimeError):
    pass


class HAActivationDeferred(HAError):
    """Native interlock rejected activation before dispatch; retry without recovery."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward a bearer token to another origin.
        raise HAError("Home Assistant redirected the request; configure its final base URL")


class HomeAssistant:
    def __init__(self, url: str, token: str, live: bool = False, timeout: float = 10):
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("HA_URL must be a base http(s) URL without embedded credentials/query")
        if not token:
            raise ValueError("Set HA_TOKEN in the process environment")
        self.url, self.token = url.rstrip("/"), token
        self.live, self.timeout = live, timeout
        self.opener = build_opener(NoRedirect())

    def request(self, path: str, body: dict | None = None):
        if body is not None and not self.live:
            raise HAError("Shadow mode forbids all POST requests")
        request = Request(self.url + "/api/" + path,
                          data=json.dumps(body).encode() if body is not None else None,
                          headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            raise HAError(f"HA API returned HTTP {exc.code} for {path}") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise HAError(f"HA API request failed for {path}") from None

    def states(self) -> dict:
        return {state["entity_id"]: state for state in self.request("states")}

    def service(self, domain: str, service: str, **data):
        return self.request(f"services/{domain}/{service}", data)
