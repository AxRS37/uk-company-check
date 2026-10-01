"""Thin client for the free Companies House Public Data API (UK registry).

PLAIN ENGLISH
-------------
This is the only file that talks to Companies House. Everything else in
the package asks this client for facts.

The API is free and official. You register once (about two minutes, no
credit card) at:
    https://developer.company-information.service.gov.uk/signin
then create an "application" and copy the API key it shows you.
Companies House documents the limit as 600 requests per 5-minute window
per key — far more than normal prospect research needs. We also cache
answers for an hour so repeat questions cost nothing.

HOW THE KEY IS SENT
-------------------
Companies House expects the key as an HTTP "Basic" auth *username* with
an EMPTY password. That is their documented scheme; we follow it exactly
(base64 of "your-key:").

DESIGN NOTE
-----------
`api_key` and `base_url` are injected (not read from the environment
inside the class) so the unit tests can point this client at a pretend
Companies House running on localhost: tests need no network, no key, and
can never spend your request quota.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

DEFAULT_BASE_URL = "https://api.company-information.service.gov.uk"
REQUEST_TIMEOUT_SECONDS = 25
CACHE_TTL_SECONDS = 3600  # registry facts change rarely; be polite

SIGNUP_URL = "https://developer.company-information.service.gov.uk/signin"

NO_KEY_MESSAGE = (
    "No Companies House API key found. The key is free (2-minute signup, "
    f"no card): {SIGNUP_URL} — register, create an application, copy the key. "
    "Then either export CH_API_KEY=... in your environment or add it to the "
    "'env' block of your MCP client config. See README.md for the exact steps."
)


class CompaniesHouseError(Exception):
    """Raised when the API cannot answer. The message is written for a
    non-technical reader: what went wrong + what to do about it.
    `status` carries the HTTP status code when there was one."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def normalize_company_number(company_number: str) -> str:
    """Clean up a company number so the registry accepts it.

    Companies House numbers are 8 characters, e.g. "00123456". People
    usually type them without the leading zeros ("123456"). Scottish
    companies start with "SC", Northern Irish ones with "NI" — those
    keep their letters. Rules applied here:
      * keep only letters and digits (drops spaces/dashes)
      * UPPERCASE letters
      * pad purely-numeric numbers to 8 digits with leading zeros
    """
    num = "".join(ch for ch in (company_number or "") if ch.isalnum()).upper()
    if num.isdigit():
        num = num.zfill(8)
    return num


def api_key_from_env() -> Optional[str]:
    """Read the API key from the environment. Two names accepted:
    CH_API_KEY (short) or COMPANIES_HOUSE_API_KEY (spelled out)."""
    for name in ("CH_API_KEY", "COMPANIES_HOUSE_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value and not value.lower().startswith("your_"):
            return value
    return None


def base_url_from_env() -> str:
    """Where the API lives. Override with CH_BASE_URL (the repo's own
    tests use this to point at a pretend registry on localhost)."""
    override = os.environ.get("CH_BASE_URL", "").strip()
    return override or DEFAULT_BASE_URL


class CompaniesHouseClient:
    """A very small JSON client with a built-in 1-hour cache."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        cache_ttl_seconds: int = CACHE_TTL_SECONDS,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cache: dict[tuple[str, str], tuple[float, Any]] = {}
        # How many real HTTP requests we made — the tests assert on this
        # to prove the cache actually prevents duplicate calls.
        self.request_count = 0

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _auth_header(self) -> dict[str, str]:
        """Companies House's documented auth: key as username, no password."""
        token = base64.b64encode(f"{self.api_key}:".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    def _friendly_http_error(self, exc: urllib.error.HTTPError) -> CompaniesHouseError:
        """Turn raw HTTP errors into messages a non-technical person can act on."""
        status = exc.code
        if status == 401:
            return CompaniesHouseError(
                "Companies House rejected the API key (HTTP 401). Check you copied "
                "the whole key with no spaces, and that it comes from the application "
                f"you created at {SIGNUP_URL}.",
                status=status,
            )
        if status == 404:
            return CompaniesHouseError(
                "Companies House has no company with that number (HTTP 404). "
                "Use search_companies to find the right number first.",
                status=status,
            )
        if status == 429:
            return CompaniesHouseError(
                "Companies House rate limit hit (HTTP 429): 600 requests per "
                "5-minute window per key. Wait a few minutes and try again — "
                "this server also caches answers for an hour, so repeats are free.",
                status=status,
            )
        return CompaniesHouseError(
            f"Companies House answered with HTTP {status}. "
            "Try again shortly; if it keeps happening, check "
            "https://status.company-information.service.gov.uk/",
            status=status,
        )

    def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> dict:
        """GET `path` and return the parsed JSON, with caching."""
        cache_key = (path, json.dumps(params or {}, sort_keys=True))
        now = time.time()
        hit = self._cache.get(cache_key)
        if hit is not None and now - hit[0] < self.cache_ttl_seconds:
            return hit[1]  # cached — costs no request quota

        if not self.api_key:
            raise CompaniesHouseError(NO_KEY_MESSAGE, status=None)

        query = urllib.parse.urlencode(params or {})
        url = self.base_url + path + (f"?{query}" if query else "")
        request = urllib.request.Request(url, headers=self._auth_header())
        self.request_count += 1
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise self._friendly_http_error(exc) from None
        except urllib.error.URLError as exc:
            raise CompaniesHouseError(
                f"Could not reach Companies House ({exc.reason}). "
                "Check your internet connection and try again.",
                status=None,
            ) from None
        except (TimeoutError, OSError) as exc:
            raise CompaniesHouseError(
                f"Companies House did not answer in time ({exc}). "
                "Check your connection and try again.",
                status=None,
            ) from None
        except json.JSONDecodeError:
            raise CompaniesHouseError(
                "Companies House returned something that was not valid JSON. "
                "Try again shortly.",
                status=None,
            ) from None

        self._cache[cache_key] = (now, data)
        return data

    # ------------------------------------------------------------------
    # endpoints (each maps 1:1 to a Companies House API endpoint)
    # ------------------------------------------------------------------

    def search_companies(self, query: str, items_per_page: int = 10) -> dict:
        """GET /search/companies — find companies by name fragment."""
        return self._get(
            "/search/companies",
            {"q": query, "items_per_page": items_per_page},
        )

    def get_company_profile(self, company_number: str) -> dict:
        """GET /company/{number} — status, dates, SIC codes, registered office."""
        return self._get(f"/company/{normalize_company_number(company_number)}")

    def get_officers(self, company_number: str, items_per_page: int = 10) -> dict:
        """GET /company/{number}/officers — directors, secretaries, etc."""
        return self._get(
            f"/company/{normalize_company_number(company_number)}/officers",
            {"items_per_page": items_per_page},
        )

    def get_filing_history(self, company_number: str, items_per_page: int = 10) -> dict:
        """GET /company/{number}/filing-history — what the company has filed and when."""
        return self._get(
            f"/company/{normalize_company_number(company_number)}/filing-history",
            {"items_per_page": items_per_page},
        )