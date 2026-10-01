# uk-company-check — UK Companies House as MCP agent tools

An [MCP](https://modelcontextprotocol.io) server that turns the **free, official Companies House API** (the UK company registry — profiles, officers, filing history for every UK company) into four tools your AI agent can call.

Every UK B2B seller doing prospect research needs this data: is the prospect still trading, who are its directors, what industry codes is it under. Non-UK (US included) lead-gen and KYB sellers need it too — UK registry data is public, in English, and every company number is a join key.

- **Audience:** B2B prospect researchers, lead-gen sellers, KYB/due-diligence workflows, any agent that needs UK company facts.
- **Cost: £0.** The API key is free (2-minute signup, no credit card). The server has no other dependencies beyond the official MCP SDK.
- **No API key? It still runs.** The server starts, lists its tools, and returns a plain-English "here's how to get the free key" message instead of crashing (agents can read that to the user).

## What it does — the four tools

| Tool | What it answers |
|---|---|
| `search_companies(query)` | "Find me UK companies named X" → name, company number, status, incorporation date, address |
| `get_company(company_number)` | Full registry profile: status (active/dissolved/…), incorporation date, registered office, SIC codes **with plain-English descriptions**, current officers (directors & secretary) with appointment dates and occupations |
| `get_filing_history(company_number)` | What the company filed and when — confirmation statements, accounts, officer changes |
| `explain_sic_code(code)` | Decode any UK SIC code to its official plain-English description. **Works offline** (embedded official list) — no key, no network |

`get_company` calls two registry endpoints (profile + officers) and returns one merged answer, so an agent doesn't have to orchestrate. Company numbers are accepted with or without leading zeros (`123456` → `00123456`), case-insensitively for `SC`/`NI` prefixes.

## 5-minute setup

Requirements: Python 3.10+ and either [`uv`](https://docs.astral.sh/uv/) or `pip`. Nothing else — no Node, no database, no paid services.

**1. Get your free Companies House API key (2 minutes, no card).**

Go to <https://developer.company-information.service.gov.uk/signin> and register an account. Sign in, open **Applications**, create a new application, and copy the API key it shows you. That's the whole signup. (Do not commit the key anywhere — see the `.env.example`.)

Companies House documents the limit as **600 requests per 5-minute window** per key — orders of magnitude more than prospect research needs. This server also caches answers for an hour, so repeat questions cost no quota.

**2. Install.**

```bash
# with uv (fastest):
uv venv && uv pip install -e .
# or with plain pip:
python3 -m venv .venv && .venv/bin/pip install -e .
```

**3. Test the install (30 seconds).**

```bash
.venv/bin/python -m pytest tests/ -q
```

You'll see `43 passed` — all tests run offline against a pretend registry on localhost, so they never spend your API quota or need the network.

**4. Wire it into your MCP client.**

Example for Claude Desktop / Cursor / any MCP client that uses JSON config:

```json
{
  "mcpServers": {
    "uk-company-check": {
      "command": "/ABSOLUTE/PATH/TO/uk-company-check/.venv/bin/python",
      "args": ["-m", "uk_company_check"],
      "env": { "CH_API_KEY": "your_real_api_key" }
    }
  }
}
```

(Any MCP client works the same way: launch `python -m uk_company_check` on stdio with `CH_API_KEY` in the environment. Alternative env name: `COMPANIES_HOUSE_API_KEY`.)

**5. Try it.** In your client, ask:

> Find UK companies called "Driver Plumbing" and tell me whether the top one is active, who its directors are, and what its SIC codes mean.

The agent will call `search_companies` → `get_company` → `explain_sic_code` and answer with sourced registry facts.

## Example tool calls and responses

These are real outputs from this server's test suite (run against a pretend registry whose fixture company mirrors the live API's exact field shapes — see `tests/fake_ch.py`). With your API key, the live Companies House returns the same shapes with real data.

`search_companies("driver plumbing")` →

```json
{
  "query": "driver plumbing",
  "total_results": 2,
  "companies": [
    {
      "company_number": "00123456",
      "company_name": "DRIVER PLUMBING & HEATING LTD",
      "company_status": "active",
      "incorporated": "2015-03-10",
      "address": "28 London Road, Great Glen, Leicestershire, LE8 9FL",
      "nature_of_business": "43220"
    },
    {
      "company_number": "08765432",
      "company_name": "OLD ROOFS LTD",
      "company_status": "dissolved",
      "incorporated": "2001-01-01",
      "address": "1 Old Street, Leicester, LE1 1AA",
      "nature_of_business": null
    }
  ]
}
```

`get_company("123456")` → (officers listed newest appointment first; resigned officers filtered out)

```json
{
  "company_name": "DRIVER PLUMBING & HEATING LTD",
  "company_number": "00123456",
  "status": "active",
  "type": "ltd",
  "incorporated": "2015-03-10",
  "ceased": null,
  "registered_office": "28 London Road, Great Glen, Leicestershire, LE8 9FL, United Kingdom",
  "sic_codes": [
    { "code": "43220", "description": "Plumbing, heat and air-conditioning installation" },
    { "code": "99999", "description": "Dormant Company" }
  ],
  "has_charges": false,
  "has_insolvency_history": false,
  "officers": [
    {
      "name": "Acme Corporate Services Limited",
      "role": "corporate-secretary",
      "appointed_on": "2020-01-01",
      "occupation": null,
      "nationality": null,
      "year_of_birth": null
    },
    {
      "name": "John Driver",
      "role": "director",
      "appointed_on": "2015-03-10",
      "occupation": "Plumber",
      "nationality": "British",
      "year_of_birth": 1980
    }
  ],
  "note": null
}
```

`get_filing_history("00123456")` →

```json
{
  "company_number": "00123456",
  "total_count": 2,
  "filings": [
    {
      "date": "2025-06-01",
      "type": "CS01",
      "category": "confirmation-statement",
      "description": "confirmation statement (2025-05-10)",
      "description_values": { "made_up_to": "2025-05-10" }
    },
    {
      "date": "2024-12-15",
      "type": "AA",
      "category": "accounts",
      "description": "legacy (group of companies' accounts made up to 31 March 2024)",
      "description_values": { "description": "group of companies' accounts made up to 31 March 2024" }
    }
  ]
}
```

`explain_sic_code("43220")` →

```json
{ "code": "43220", "description": "Plumbing, heat and air-conditioning installation" }
```

## How it's tested (and why you can trust the README)

43 tests in three layers — **all offline**, no network, no API quota:

1. **Client unit tests** (`tests/test_ch_api.py`) — company-number normalisation, caching (a cached call provably makes zero HTTP requests), friendly errors for 401/404/429, SIC descriptions.
2. **Tool layer tests** (`tests/test_server_tools.py`) — each tool's output shape, resigned-officer filtering, SIC enrichment, all four tools registered, every tool has a docstring (agent-visible descriptions).
3. **Protocol tests** (`tests/test_protocol.py`) — launch the real server as a subprocess, speak MCP JSON-RPC over stdio (initialize → tools/list → tools/call), and verify responses. Also proves zero-key mode starts and self-explains instead of crashing.

The fake registry (`tests/fake_ch.py`) mirrors the exact Companies House response field names (`company_name`, `company_status`, `sic_codes`, `officer_role`, `appointed_on`, …) as documented on the official reference pages.

Run them yourself:

```bash
.venv/bin/python -m pytest tests/ -v
```

## Works with both MCP SDK generations

The official Python SDK renamed its main class between versions: 1.x exposes `mcp.server.fastmcp.FastMCP`, 2.x renamed it to `mcp.server.mcpserver.MCPServer`. This server imports whichever is installed, so `pip install mcp` can give you either and it still runs. The protocol tests pass against both. (Tested against 1.14.0 and 2.2.0, on Python 3.12 and 3.14.)

## Who it's for

- **B2B sellers and agencies** doing UK prospect research (segment by SIC code, verify a company is active before outreach).
- **US and non-UK lead-gen sellers** — UK registry data is public, in English, structured; the company number is a stable join key for enrichment.
- **KYB / due-diligence agents** — status, officers, and filing history are the three core checks.
- **Any MCP client user** who just wants UK company facts on tap.

## Licence

MIT for the code (see `LICENSE`). The embedded SIC code descriptions in `src/uk_company_check/sic_data.py` are official UK SIC 2007 condensed-list data from Companies House, under the Open Government Licence v3.0 — Crown copyright preserved. Companies House data returned at runtime belongs to the UK Crown / Companies House and is used under OGL v3.0.