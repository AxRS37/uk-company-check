"""Protocol test: run the REAL server as a subprocess and speak MCP to it.

This is the closest a test can get to "a buyer runs the README": the
server is launched exactly as an MCP client would launch it, and we
send actual JSON-RPC messages over stdio — initialize handshake,
tools/list, tools/call — then check the responses.

No MCP client SDK is needed on the test side: MCP over stdio is one
JSON-RPC message per line, so we can speak it with plain subprocess IO.
This works identically against MCP SDK 1.x and 2.x servers.

The Companies House calls inside the server are pointed at the fake
registry (CH_BASE_URL env var, exactly as documented in the README).
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

sys.path.insert(0, str(ROOT / "tests"))
from fake_ch import start_fake_companies_house  # noqa: E402


class MCPStdioClient:
    """Minimal MCP client: newline-delimited JSON-RPC over stdio."""

    def __init__(self, command):
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(ROOT),
            env=env,
        )

    def send(self, message):
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def receive(self, timeout=30):
        # line-buffered read with a timeout guard
        import selectors

        sel = selectors.DefaultSelector()
        sel.register(self.proc.stdout, selectors.EVENT_READ)
        if not sel.select(timeout):
            raise TimeoutError("server did not answer in time")
        line = self.proc.stdout.readline()
        if not line:
            raise AssertionError(
                "server closed stdout. stderr:\n" + self.proc.stderr.read()
            )
        return json.loads(line)

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


@pytest.fixture()
def fake_env(monkeypatch):
    server, thread = start_fake_companies_house()
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("CH_BASE_URL", base)
    monkeypatch.setenv("CH_API_KEY", "test-key")
    yield base
    server.shutdown()
    thread.join(timeout=5)


def call_tool(client, name, arguments):
    client.send(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    response = client.receive()
    assert response.get("id") == 4
    assert "error" not in response, response.get("error")
    result = response["result"]
    assert result.get("isError") is False, result
    # FastMCP-style servers return text content blocks; the structured
    # payload is inside. Parse the first text block.
    text = result["content"][0]["text"]
    return json.loads(text)


class TestMcpProtocol:
    def test_initialize_and_tools(self, fake_env, monkeypatch):
        # Ensure the subprocess can import from src/ (uninstalled mode).
        env_path = str(SRC)
        monkeypatch.setenv("PYTHONPATH", env_path)
        client = MCPStdioClient([sys.executable, "-m", "uk_company_check"])
        try:
            client.send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "pytest", "version": "1.0"},
                    },
                }
            )
            init = client.receive()
            assert init["result"]["serverInfo"]["name"] == "uk-company-check"
            assert init["result"]["protocolVersion"]

            client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

            client.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            listed = client.receive()
            tool_names = {t["name"] for t in listed["result"]["tools"]}
            assert tool_names == {
                "search_companies",
                "get_company",
                "get_filing_history",
                "explain_sic_code",
            }

            # Every tool must carry a description for agents.
            for tool in listed["result"]["tools"]:
                assert tool.get("description"), f"tool {tool['name']} lacks description"

            data = call_tool(client, "get_company", {"company_number": "123456"})
            assert data["company_number"] == "00123456"
            assert data["status"] == "active"
            officers = {o["name"]: o for o in data["officers"]}
            assert officers["John Driver"]["role"] == "director"

            data = call_tool(client, "search_companies", {"query": "driver plumbing"})
            assert data["total_results"] == 2

            data = call_tool(client, "get_filing_history", {"company_number": "00123456"})
            assert data["filings"][0]["type"] == "CS01"

            data = call_tool(client, "explain_sic_code", {"code": "43220"})
            assert data["description"] == "Plumbing, heat and air-conditioning installation"
        finally:
            client.close()

    def test_zero_key_mode_still_lists_tools(self, monkeypatch):
        """No API key configured: server must still start and expose tools."""
        monkeypatch.setenv("PYTHONPATH", str(SRC))
        monkeypatch.delenv("CH_API_KEY", raising=False)
        monkeypatch.delenv("COMPANIES_HOUSE_API_KEY", raising=False)
        monkeypatch.setenv("CH_BASE_URL", "https://api.company-information.service.gov.uk")
        client = MCPStdioClient([sys.executable, "-m", "uk_company_check"])
        try:
            client.send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "pytest", "version": "1.0"},
                    },
                }
            )
            client.receive()
            client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            client.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            listed = client.receive()
            assert len(listed["result"]["tools"]) == 4

            # And calling a tool returns the actionable no-key message
            # instead of crashing.
            client.send(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "search_companies", "arguments": {"query": "tesco"}},
                }
            )
            response = client.receive()
            assert response["result"]["isError"] is False
            text = response["result"]["content"][0]["text"]
            payload = json.loads(text)
            assert "CH_API_KEY" in payload["error"]
        finally:
            client.close()