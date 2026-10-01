"""uk-company-check MCP server — Companies House as agent tools.

This file is the whole MCP layer. It defines four tools an AI agent (or
you, in any MCP client) can call, and wires them to the API client in
ch_api.py.

RUNS ON BOTH MCP SDK GENERATIONS
--------------------------------
The official Python SDK renamed its main class between versions:
  * SDK 1.x:  from mcp.server.fastmcp import FastMCP
  * SDK 2.x:  from mcp.server.mcpserver import MCPServer   (FastMCP was renamed)
We try 1.x first and fall back to 2.x, so `pip install mcp` gives you
whichever version is current and this server still runs. Everything else
(decorators, run(), stdio transport) is identical across both.

WHY stdout IS SACRED
--------------------
MCP servers talk to the client over stdout (JSON-RPC, one message per
line). A stray `print()` corrupts the protocol. All diagnostics here go
to stderr only, and the tools themselves RETURN values — never print.

ZERO-KEY MODE
-------------
If no API key is configured the server still starts and still lists
its tools; calling a tool returns a plain-English message explaining
how to get the free key. (An agent can read that message to the user —
far better than a crash.)
"""

from __future__ import annotations

import sys
from typing import Any, Optional

from uk_company_check.ch_api import (
    CompaniesHouseClient,
    CompaniesHouseError,
    api_key_from_env,
    base_url_from_env,
)
from uk_company_check.sic_data import describe_sic_code

# ---------------------------------------------------------------------------
# SDK compatibility shim (see module docstring)
# ---------------------------------------------------------------------------
try:  # SDK 1.x
    from mcp.server.fastmcp import FastMCP
except ImportError:  # SDK 2.x renamed FastMCP -> MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP  # type: ignore

server = FastMCP(
    "uk-company-check",
    instructions=(
        "UK Companies House registry tools for B2B prospect research: "
        "search_companies to find a company and its number, get_company for "
        "status/incorporation date/SIC codes/officers, get_filing_history for "
        "filings, explain_sic_code to decode SIC codes. Company numbers "
        "accepted with or without leading zeros."
    ),
)

# One shared client: one cache, and its request_count is reused by tests.
_client: Optional[CompaniesHouseClient] = None


def get_client() -> CompaniesHouseClient:
    """Lazily build the API client from environment variables."""
    global _client
    if _client is None:
        _client = CompaniesHouseClient(
            api_key=api_key_from_env(),
            base_url=base_url_from_env(),
        )
    return _client


def _friendly_error(exc: CompaniesHouseError) -> dict[str, Any]:
    """Represent a CompaniesHouseError as a tool result (not a crash)."""
    return {"error": str(exc)}


# ---------------------------------------------------------------------------
# THE FOUR TOOLS
# ---------------------------------------------------------------------------


@server.tool()
def search_companies(query: str, items_per_page: int = 10) -> dict[str, Any]:
    """Search the UK company register by name. Returns matches with company
    number, status, address snippet and incorporation date — use the
    company_number from the best match with get_company.

    Args:
        query: Company name or part of it, e.g. "Driver Plumbing" or "TESCO".
        items_per_page: How many matches to return (1-100, default 10).
    """
    query = (query or "").strip()
    if not query:
        return {"error": "Please give a company name to search for, e.g. search_companies('Driver Plumbing')."}
    items_per_page = max(1, min(int(items_per_page or 10), 100))
    try:
        data = get_client().search_companies(query, items_per_page=items_per_page)
    except CompaniesHouseError as exc:
        return _friendly_error(exc)

    results = []
    for item in data.get("items", []):
        results.append(
            {
                "company_number": item.get("company_number"),
                "company_name": item.get("title") or item.get("company_name"),
                "company_status": item.get("company_status"),
                "incorporated": item.get("date_of_creation"),
                "address": item.get("address_snippet"),
                "nature_of_business": item.get("matches", {})
                .get("sic_code", [None])[0],
            }
        )
    return {
        "query": query,
        "total_results": data.get("total_results"),
        "companies": results,
    }


@server.tool()
def get_company(company_number: str) -> dict[str, Any]:
    """Full profile of one UK company from its official registry record:
    status (active/dissolved/...), incorporation date, registered office,
    SIC codes with plain-English descriptions, and current officers
    (directors and secretary) with appointment dates and occupations.

    Args:
        company_number: The 8-character Companies House number, e.g.
            "00123456", "123456", "SC123456" or "NI123456" — leading zeros
            optional.
    """
    try:
        client = get_client()
        profile = client.get_company_profile(company_number)
    except CompaniesHouseError as exc:
        return _friendly_error(exc)

    office = profile.get("registered_office_address", {})
    sic_codes = profile.get("sic_codes", []) or []
    officers_payload: dict[str, Any] = {"officers": [], "note": None}
    try:
        officers_data = client.get_officers(company_number)
    except CompaniesHouseError as exc:  # officers are supplementary — never
        officers_payload["note"] = str(exc)  # fail the whole profile for them
    else:
        officers_payload = _summarize_officers(officers_data)

    return {
        "company_name": profile.get("company_name"),
        "company_number": profile.get("company_number"),
        "status": profile.get("company_status"),
        "type": profile.get("type"),
        "incorporated": profile.get("date_of_creation"),
        "ceased": profile.get("date_of_cessation"),
        "registered_office": _one_line_address(office),
        "sic_codes": [
            {
                "code": code,
                "description": describe_sic_code(code),
            }
            for code in sic_codes
        ],
        "has_charges": profile.get("has_charges"),
        "has_insolvency_history": profile.get("has_insolvency_history"),
        **officers_payload,
    }


def _summarize_officers(payload: dict[str, Any]) -> dict[str, Any]:
    """Condense a Companies House officers payload to the fields a
    prospect researcher actually needs. Current officers only, newest
    appointments first."""
    officers = []
    for item in payload.get("items", []):
        if item.get("resigned_on"):  # historical appointments are noise here
            continue
        dob = item.get("date_of_birth") or {}
        officers.append(
            {
                "name": item.get("name"),
                "role": item.get("officer_role"),
                "appointed_on": item.get("appointed_on"),
                "occupation": item.get("occupation"),
                "nationality": item.get("nationality"),
                "year_of_birth": dob.get("year"),
            }
        )
    officers.sort(key=lambda o: o.get("appointed_on") or "", reverse=True)
    return {
        "officers": officers,
        "note": None if officers else "No current officers listed.",
    }


def _one_line_address(office: dict[str, Any]) -> str:
    """Flatten the registry's address object into one readable line."""
    order = (
        "address_line_1",
        "address_line_2",
        "locality",
        "region",
        "postal_code",
        "country",
    )
    parts = [office.get(key) for key in order if office.get(key)]
    return ", ".join(str(p) for p in parts)


@server.tool()
def get_filing_history(company_number: str, items_per_page: int = 10) -> dict[str, Any]:
    """The most recent filings a company has made at Companies House
    (confirmation statements, accounts, officer changes...). Useful for
    spotting dormancy (nothing filed for years), late accounts, or a
    change of directors last week.

    Args:
        company_number: The Companies House number (leading zeros optional).
        items_per_page: How many filings to return (1-100, default 10).
    """
    try:
        client = get_client()
        data = client.get_filing_history(
            company_number, items_per_page=max(1, min(int(items_per_page or 10), 100))
        )
    except CompaniesHouseError as exc:
        return _friendly_error(exc)

    filings = []
    for item in data.get("items", []):
        filings.append(
            {
                "date": item.get("date"),
                "type": item.get("type"),
                "category": item.get("category"),
                "description": _describe_filing(item),
                "description_values": item.get("description_values"),
            }
        )
    return {
        "company_number": data.get("company_number") or "",
        "total_count": data.get("total_count"),
        "filings": filings,
    }


def _describe_filing(item: dict[str, Any]) -> str:
    """Companies House returns a description *key* like 'legacy' or
    'confirmation-statement' plus separate values. Make one human sentence."""
    description = item.get("description") or item.get("type") or "filing"
    values = item.get("description_values") or {}
    sentence = description.replace("-", " ")
    for value in values.values():
        if isinstance(value, str):
            sentence += f" ({value})"
            break
    return sentence


@server.tool()
def explain_sic_code(code: str) -> dict[str, Any]:
    """Decode a UK SIC code (the 5-digit 'nature of business' code on every
    company record) into its official plain-English description. Works
    offline from the official UK SIC 2007 condensed list — no API key
    needed. Example: explain_sic_code('43220') -> 'Plumbing, heat and
    air-conditioning installation'.

    Args:
        code: The SIC code, e.g. "43220". Leading zeros are kept.
    """
    description = describe_sic_code(code)
    if description is None:
        return {
            "code": (code or "").strip(),
            "description": None,
            "note": (
                "Not a code on the official UK SIC 2007 condensed list. "
                "Codes are 5 digits, e.g. 43220. (This tool works offline; "
                "it does not call the API.)"
            ),
        }
    return {"code": (code or "").strip(), "description": description}


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------


def main() -> None:
    """Start the MCP server on the stdio transport (what Claude Desktop,
    Cursor, Continue and other MCP clients launch by default)."""
    key = api_key_from_env()
    if key:
        print("uk-company-check: API key loaded — ready.", file=sys.stderr)
    else:
        print(
            "uk-company-check: no API key found (CH_API_KEY). The server "
            "still runs; calling a tool will explain how to get the free "
            "2-minute key. See README.md.",
            file=sys.stderr,
        )
    server.run()  # stdio by default in both SDK generations


if __name__ == "__main__":
    main()