"""Shared test fixtures: a pretend Companies House running on localhost.

WHY A FAKE SERVER (plain English)
---------------------------------
The unit tests must never:
  * spend the user's API request quota,
  * need the network to pass, or
  * depend on real company data that Companies House can change.

So the tests start a tiny real HTTP server on localhost that answers
exactly like Companies House does — same URL paths, same JSON shapes,
same status codes — and point CH_BASE_URL at it. The client code under
test is 100% the production code; only the endpoint is pretend.

The fixture payloads mirror the field names documented by Companies
House for each endpoint (company_name, company_status, sic_codes,
officer_role, appointed_on, ...), verified against the official
reference pages at developer-specs.company-information.service.gov.uk.

Company numbers deliberately start with zeros (00123456): most real
registry numbers do, and it lets the tests prove the "typed without
leading zeros" normalisation works end to end.
"""

import base64
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# ---------------------------------------------------------------------------
# Fixture data — realistic Companies House response shapes
# ---------------------------------------------------------------------------

# A normal active company, typical B2B prospect record.
COMPANY_PROFILE_ACTIVE = {
    "company_name": "DRIVER PLUMBING & HEATING LTD",
    "company_number": "00123456",
    "company_status": "active",
    "type": "ltd",
    "date_of_creation": "2015-03-10",
    "date_of_cessation": None,
    "registered_office_address": {
        "address_line_1": "28 London Road",
        "locality": "Great Glen",
        "region": "Leicestershire",
        "postal_code": "LE8 9FL",
        "country": "United Kingdom",
    },
    "sic_codes": ["43220", "99999"],
    "has_charges": False,
    "has_insolvency_history": False,
    "etag": '"abc123"',
}

# A dissolved company — the "don't waste outreach on this one" case.
COMPANY_PROFILE_DISSOLVED = {
    "company_name": "OLD ROOFS LTD",
    "company_number": "08765432",
    "company_status": "dissolved",
    "type": "ltd",
    "date_of_creation": "2001-01-01",
    "date_of_cessation": "2019-12-31",
    "registered_office_address": {
        "address_line_1": "1 Old Street",
        "locality": "Leicester",
        "postal_code": "LE1 1AA",
    },
    "sic_codes": ["43910"],
    "has_charges": False,
    "has_insolvency_history": False,
}

OFFICERS_PAYLOAD = {
    "items_per_page": 10,
    "total_results": 3,
    "items": [
        {
            "name": "John Driver",
            "officer_role": "director",
            "appointed_on": "2015-03-10",
            "resigned_on": None,
            "occupation": "Plumber",
            "nationality": "British",
            "date_of_birth": {"year": 1980, "month": 7},
        },
        {
            "name": "Sarah Smith",
            "officer_role": "secretary",
            "appointed_on": "2015-03-10",
            "resigned_on": "2018-05-01",  # historical — must be filtered out
            "occupation": "Administrator",
            "nationality": "British",
        },
        {
            "name": "Acme Corporate Services Limited",
            "officer_role": "corporate-secretary",
            "appointed_on": "2020-01-01",
            "resigned_on": None,
            "occupation": None,
            "nationality": None,
        },
    ],
}

FILING_HISTORY_PAYLOAD = {
    "items_per_page": 10,
    "total_count": 2,
    "items": [
        {
            "date": "2025-06-01",
            "type": "CS01",
            "category": "confirmation-statement",
            "description": "confirmation-statement",
            "description_values": {"made_up_to": "2025-05-10"},
        },
        {
            "date": "2024-12-15",
            "type": "AA",
            "category": "accounts",
            "description": "legacy",
            "description_values": {
                "description": "group of companies' accounts made up to 31 March 2024"
            },
        },
    ],
}

SEARCH_PAYLOAD = {
    "items_per_page": 10,
    "total_results": 2,
    "items": [
        {
            "company_number": "00123456",
            "company_type": "ltd",
            "company_status": "active",
            "title": "DRIVER PLUMBING & HEATING LTD",
            "date_of_creation": "2015-03-10",
            "address_snippet": "28 London Road, Great Glen, Leicestershire, LE8 9FL",
            "kind": "searchcompanies#company",
            "matches": {"sic_code": ["43220"]},
        },
        {
            "company_number": "08765432",
            "company_type": "lldp",
            "company_status": "dissolved",
            "title": "OLD ROOFS LTD",
            "date_of_creation": "2001-01-01",
            "address_snippet": "1 Old Street, Leicester, LE1 1AA",
            "kind": "searchcompanies#company",
            "matches": {},
        },
    ],
}

# Registry facts per company number — the fake registry's "database".
PROFILES = {
    "00123456": COMPANY_PROFILE_ACTIVE,
    "08765432": COMPANY_PROFILE_DISSOLVED,
}
OFFICERS = {
    "00123456": OFFICERS_PAYLOAD,
    "08765432": {"items": [], "total_results": 0},  # dissolved: none current
}
FILING_HISTORY = {
    "00123456": FILING_HISTORY_PAYLOAD,
    "08765432": {
        "items_per_page": 10,
        "total_count": 1,
        "items": [
            {
                "date": "2019-12-31",
                "type": "GAZ2",
                "category": "gazette",
                "description": "gaz2-dissolution",
                "description_values": {},
            }
        ],
    },
}


# ---------------------------------------------------------------------------
# The fake registry server
# ---------------------------------------------------------------------------


class FakeCompaniesHouseHandler(BaseHTTPRequestHandler):
    """Answers requests exactly like the real Companies House API:
    same paths, same auth header expectation, same JSON, same status
    codes (401 bad key, 404 unknown company)."""

    def log_message(self, *args):  # silence test-run noise
        pass

    def do_GET(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            self._send(401, {"errors": [{"error": "Invalid API key"}]})
            return

        # Decode the Basic auth exactly as the real API contract expects:
        # "key:" base64-encoded (key as username, empty password).
        try:
            decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
            username, _, password = decoded.partition(":")
        except Exception:
            self._send(401, {"errors": [{"error": "Invalid API key"}]})
            return
        if username != "test-key" or password:
            self._send(401, {"errors": [{"error": "Invalid API key"}]})
            return

        path = self.path.split("?")[0]

        if path == "/search/companies":
            self._send(200, SEARCH_PAYLOAD)
            return
        match = re.fullmatch(r"/company/([0-9A-Z]+)", path)
        if match:
            number = match.group(1)
            if number in PROFILES:
                self._send(200, PROFILES[number])
            else:
                self._send(404, {"errors": [{"error": "Company not found"}]})
            return
        match = re.fullmatch(r"/company/([0-9A-Z]+)/officers", path)
        if match:
            number = match.group(1)
            if number in OFFICERS:
                self._send(200, OFFICERS[number])
            else:
                self._send(404, {"errors": [{"error": "Company not found"}]})
            return
        match = re.fullmatch(r"/company/([0-9A-Z]+)/filing-history", path)
        if match:
            number = match.group(1)
            if number in FILING_HISTORY:
                self._send(200, FILING_HISTORY[number])
            else:
                self._send(404, {"errors": [{"error": "Company not found"}]})
            return
        self._send(404, {"errors": [{"error": "Not found"}]})

    def _send(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_fake_companies_house() -> tuple[HTTPServer, threading.Thread]:
    """Start the pretend registry on an OS-chosen free localhost port.

    Returns (server, thread). Call server.shutdown() when finished.
    """
    server = HTTPServer(("127.0.0.1", 0), FakeCompaniesHouseHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread