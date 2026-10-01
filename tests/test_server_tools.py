"""Unit tests for the MCP tool layer (server.py), no network needed.

The tools are plain Python functions (the @server.tool() decorator
returns the original function in both MCP SDK generations — verified in
this repo), so the tests call them directly against the fake registry.
"""

import asyncio
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from fake_ch import start_fake_companies_house  # noqa: E402
import uk_company_check.server as server_mod  # noqa: E402
from uk_company_check.ch_api import CompaniesHouseClient  # noqa: E402


@pytest.fixture()
def fake_registry(monkeypatch):
    server, thread = start_fake_companies_house()
    base = f"http://127.0.0.1:{server.server_port}"
    # Point the server module's client at the fake registry, as the
    # CH_BASE_URL env var would in real use.
    monkeypatch.setattr(
        server_mod,
        "_client",
        CompaniesHouseClient(api_key="test-key", base_url=base),
    )
    yield base
    server.shutdown()
    thread.join(timeout=5)


def tools():
    """The four tool functions (decorator returns them unchanged)."""
    return {
        "search_companies": server_mod.search_companies,
        "get_company": server_mod.get_company,
        "get_filing_history": server_mod.get_filing_history,
        "explain_sic_code": server_mod.explain_sic_code,
    }


# ---------------------------------------------------------------------------
# tool: search_companies
# ---------------------------------------------------------------------------


class TestSearchCompaniesTool:
    def test_returns_matches_with_numbers(self, fake_registry):
        result = server_mod.search_companies("driver plumbing")
        assert result["total_results"] == 2
        assert result["companies"][0]["company_number"] == "00123456"
        assert result["companies"][0]["company_name"] == "DRIVER PLUMBING & HEATING LTD"
        assert result["companies"][0]["company_status"] == "active"
        assert result["companies"][0]["incorporated"] == "2015-03-10"
        assert "LE8 9FL" in result["companies"][0]["address"]

    def test_empty_query_gives_error_not_crash(self, fake_registry):
        result = server_mod.search_companies("   ")
        assert "error" in result
        assert "company name" in result["error"]

    def test_no_key_returns_friendly_message(self, monkeypatch):
        monkeypatch.setattr(server_mod, "_client", CompaniesHouseClient(api_key=None))
        result = server_mod.search_companies("anything")
        assert "error" in result
        assert "CH_API_KEY" in result["error"]

    def test_items_per_page_clamped(self, fake_registry):
        # Clamping happens before the request; the fake registry answers
        # regardless, so just assert no crash and a sane result shape.
        result = server_mod.search_companies("driver", items_per_page=99999)
        assert result["total_results"] == 2


# ---------------------------------------------------------------------------
# tool: get_company
# ---------------------------------------------------------------------------


class TestGetCompanyTool:
    def test_full_profile_with_officers(self, fake_registry):
        result = server_mod.get_company("00123456")
        assert result["company_name"] == "DRIVER PLUMBING & HEATING LTD"
        assert result["status"] == "active"
        assert result["incorporated"] == "2015-03-10"
        assert result["registered_office"] == (
            "28 London Road, Great Glen, Leicestershire, LE8 9FL, United Kingdom"
        )

        # SIC codes arrive WITH their plain-English descriptions.
        sic = result["sic_codes"]
        assert {"code": "43220", "description": "Plumbing, heat and air-conditioning installation"} in sic

        # Officers: resigned ones filtered out, current ones present.
        names = [o["name"] for o in result["officers"]]
        assert "John Driver" in names
        assert "Sarah Smith" not in names  # resigned 2018
        director = next(o for o in result["officers"] if o["role"] == "director")
        assert director["appointed_on"] == "2015-03-10"
        assert director["occupation"] == "Plumber"
        assert director["year_of_birth"] == 1980

    def test_dissolved_company(self, fake_registry):
        result = server_mod.get_company("8765432")
        assert result["status"] == "dissolved"
        assert result["ceased"] == "2019-12-31"
        assert result["officers"] == []  # fake registry has none for this co
        assert result["note"] == "No current officers listed."

    def test_number_padding_via_tool(self, fake_registry):
        result = server_mod.get_company("123456")
        assert result["company_number"] == "00123456"

    def test_missing_company_is_an_error_result_not_a_crash(self, fake_registry):
        result = server_mod.get_company("99999999")
        assert "error" in result
        assert "404" in result["error"]


# ---------------------------------------------------------------------------
# tool: get_filing_history
# ---------------------------------------------------------------------------


class TestFilingHistoryTool:
    def test_filings_summarized(self, fake_registry):
        result = server_mod.get_filing_history("00123456")
        assert result["total_count"] == 2
        first = result["filings"][0]
        assert first["date"] == "2025-06-01"
        assert first["type"] == "CS01"
        assert first["category"] == "confirmation-statement"
        assert "confirmation statement" in first["description"]

    def test_missing_company_error(self, fake_registry):
        result = server_mod.get_filing_history("99999999")
        assert "error" in result


# ---------------------------------------------------------------------------
# tool: explain_sic_code (offline — no registry needed)
# ---------------------------------------------------------------------------


class TestExplainSicTool:
    def test_known_code(self):
        result = server_mod.explain_sic_code("43220")
        assert result["description"] == "Plumbing, heat and air-conditioning installation"
        assert result["code"] == "43220"

    def test_unknown_code_notes_offline(self):
        result = server_mod.explain_sic_code("12345")  # genuinely not on the list
        assert result["description"] is None
        assert "does not call the API" in result["note"]

    def test_zero_pad(self):
        # 01120 "Growing of rice" — only resolvable with the leading zero.
        assert server_mod.explain_sic_code("1120")["description"] == "Growing of rice"


# ---------------------------------------------------------------------------
# MCP server surface: all four tools registered, dual-SDK compatible
# ---------------------------------------------------------------------------


class TestServerSurface:
    def test_all_four_tools_registered(self):
        registered = {t.name for t in asyncio.run(server_mod.server.list_tools())}
        assert registered == {
            "search_companies",
            "get_company",
            "get_filing_history",
            "explain_sic_code",
        }

    def test_tool_functions_are_directly_callable(self):
        # Both SDK generations return the undecorated function from
        # @server.tool() — asserted here so a future SDK change that
        # breaks direct-call tests fails loudly with a clear cause.
        for tool in tools().values():
            assert callable(tool)

    def test_every_tool_has_a_docstring(self):
        # Tool descriptions are what an AI agent reads to decide what to
        # call — a missing docstring makes a tool invisible to agents.
        for name, tool in tools().items():
            assert tool.__doc__ and tool.__doc__.strip(), f"{name} lacks a docstring"