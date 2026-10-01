"""Unit tests for the Companies House API client (no network needed).

Every test runs against a pretend Companies House on localhost:1234-avoid
(a free port chosen by the OS). See fake_ch.py for why and how.
"""

import sys
from pathlib import Path

import pytest

# Make `src/` importable when the package is not installed (running
# straight from a checkout). Installed use ignores this path entry.
SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from uk_company_check.ch_api import (  # noqa: E402
    CompaniesHouseClient,
    CompaniesHouseError,
    normalize_company_number,
)
from uk_company_check.sic_data import describe_sic_code  # noqa: E402

from fake_ch import start_fake_companies_house  # noqa: E402


@pytest.fixture()
def fake_registry():
    """Pretend Companies House on localhost; yields its base URL."""
    server, thread = start_fake_companies_house()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture()
def client(fake_registry):
    return CompaniesHouseClient(api_key="test-key", base_url=fake_registry)


# ---------------------------------------------------------------------------
# company number normalisation (pure logic)
# ---------------------------------------------------------------------------


class TestNormalizeCompanyNumber:
    def test_pads_numeric_numbers_to_8_digits(self):
        assert normalize_company_number("123456") == "00123456"

    def test_keeps_8_digit_numbers_as_is(self):
        assert normalize_company_number("00123456") == "00123456"

    def test_uppercases_scottish_prefixes(self):
        assert normalize_company_number("sc123456") == "SC123456"

    def test_northern_ireland_prefix(self):
        assert normalize_company_number("NI567890") == "NI567890"

    def test_strips_spaces_and_dashes(self):
        assert normalize_company_number(" 12-34 56 ") == "00123456"

    def test_empty_and_garbage(self):
        assert normalize_company_number("") == ""
        assert normalize_company_number("---") == ""


# ---------------------------------------------------------------------------
# SIC code descriptions (pure logic, offline)
# ---------------------------------------------------------------------------


class TestSicDescriptions:
    def test_known_code(self):
        assert describe_sic_code("43220") == (
            "Plumbing, heat and air-conditioning installation"
        )

    def test_zero_padding_needed_case(self):
        # 01120 "Growing of rice" — only resolvable with the leading zero.
        assert describe_sic_code("1120") == "Growing of rice"
        assert describe_sic_code("1120") == describe_sic_code("01120")

    def test_short_code_is_zero_padded(self):
        # 4322 -> 04322, which is NOT on the condensed list (division
        # 43.22 has no standalone entry) — but 43220 is.
        assert describe_sic_code("4322") is None
        assert describe_sic_code("43220") is not None

    def test_99999_is_a_real_code(self):
        # Fun fact that matters for tests: 99999 is CH's official
        # "Dormant Company" code — NOT an unknown-code example.
        assert describe_sic_code("99999") == "Dormant Company"

    def test_unknown_code_returns_none(self):
        # Genuinely absent from the official condensed list:
        assert describe_sic_code("99998") is None
        assert describe_sic_code("00000") is None
        assert describe_sic_code("12345") is None
        assert describe_sic_code("") is None
        assert describe_sic_code("abc") is None


# ---------------------------------------------------------------------------
# client behaviour against the fake registry
# ---------------------------------------------------------------------------


class TestClientBasics:
    def test_search_returns_real_shape(self, client):
        data = client.search_companies("driver plumbing")
        assert data["total_results"] == 2
        first = data["items"][0]
        assert first["company_number"] == "00123456"
        assert first["title"] == "DRIVER PLUMBING & HEATING LTD"
        assert first["company_status"] == "active"

    def test_get_profile_active_company(self, client):
        profile = client.get_company_profile("00123456")
        assert profile["company_name"] == "DRIVER PLUMBING & HEATING LTD"
        assert profile["company_status"] == "active"
        assert profile["date_of_creation"] == "2015-03-10"
        assert profile["sic_codes"] == ["43220", "99999"]
        assert profile["registered_office_address"]["postal_code"] == "LE8 9FL"

    def test_get_profile_pads_number(self, client):
        # "123456" must be sent as "00123456" — the registry 404s otherwise.
        assert client.get_company_profile("123456")["company_number"] == "00123456"

    def test_missing_company_raises_friendly_404(self, client):
        with pytest.raises(CompaniesHouseError) as excinfo:
            client.get_company_profile("99999999")
        assert excinfo.value.status == 404
        assert "no company with that number" in str(excinfo.value)

    def test_wrong_key_gives_friendly_401(self, fake_registry):
        bad = CompaniesHouseClient(api_key="wrong-key", base_url=fake_registry)
        with pytest.raises(CompaniesHouseError) as excinfo:
            bad.get_company_profile("00123456")
        assert excinfo.value.status == 401
        assert "API key" in str(excinfo.value)

    def test_no_key_raises_actionable_message(self, fake_registry):
        nokey = CompaniesHouseClient(api_key=None, base_url=fake_registry)
        with pytest.raises(CompaniesHouseError) as excinfo:
            nokey.get_company_profile("00123456")
        message = str(excinfo.value)
        assert "developer.company-information.service.gov.uk" in message
        assert "CH_API_KEY" in message

    def test_filing_history_shape(self, client):
        data = client.get_filing_history("00123456")
        assert data["total_count"] == 2
        assert data["items"][0]["type"] == "CS01"
        assert data["items"][0]["category"] == "confirmation-statement"

    def test_cache_prevents_second_http_request(self, client):
        client.get_company_profile("00123456")
        first = client.request_count
        client.get_company_profile("00123456")  # same company, same endpoint
        client.get_company_profile("123456")  # normalises to the same number
        assert client.request_count == first
        client.search_companies("driver")  # different endpoint → new request
        assert client.request_count == first + 1

    def test_officers_shape(self, client):
        data = client.get_officers("123456")  # pads to 00123456
        assert data["total_results"] == 3
        assert data["items"][0]["officer_role"] == "director"

    def test_cache_expires_after_ttl(self, fake_registry):
        short = CompaniesHouseClient(
            api_key="test-key", base_url=fake_registry, cache_ttl_seconds=0
        )
        short.get_company_profile("00123456")
        first = short.request_count
        short.get_company_profile("00123456")
        assert short.request_count == first + 1  # TTL 0 → every call is fresh


class TestEnvHelpers:
    def test_api_key_from_env(self, monkeypatch):
        import uk_company_check.ch_api as mod

        monkeypatch.setenv("CH_API_KEY", " abc123 ")
        assert mod.api_key_from_env() == "abc123"

    def test_api_key_long_name(self, monkeypatch):
        import uk_company_check.ch_api as mod

        monkeypatch.delenv("CH_API_KEY", raising=False)
        monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "xyz")
        assert mod.api_key_from_env() == "xyz"

    def test_api_key_ignores_placeholder(self, monkeypatch):
        import uk_company_check.ch_api as mod

        monkeypatch.setenv("CH_API_KEY", "your_companies_house_api_key_here")
        assert mod.api_key_from_env() is None

    def test_base_url_override(self, monkeypatch):
        import uk_company_check.ch_api as mod

        monkeypatch.setenv("CH_BASE_URL", "http://127.0.0.1:9/")
        assert mod.base_url_from_env() == "http://127.0.0.1:9/"
        monkeypatch.delenv("CH_BASE_URL")
        assert mod.base_url_from_env() == mod.DEFAULT_BASE_URL