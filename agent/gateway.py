"""MCP client: JSON-RPC over streamable HTTP, standard library only.

The agent sends its requests to the proxy container of the task, without a token. The proxy adds
the JWT of the session. The tests use the same client with a token and the address of the gateway.
"""
import json
import time
import urllib.error
import urllib.request


class GatewayError(Exception):
    pass


class Gateway:
    def __init__(self, url: str, token: str = "") -> None:
        self.url, self.token = url, token
        self._request_id = 0
        self._mcp_session = None

    def rpc(self, method: str, params: dict | None = None, notification: bool = False) -> tuple[int, dict | None]:
        """Send one JSON-RPC message. Return (HTTP status, parsed body or None)."""
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notification:
            self._request_id += 1
            message["id"] = self._request_id
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self._mcp_session:
            headers["Mcp-Session-Id"] = self._mcp_session
        request = urllib.request.Request(self.url, json.dumps(message).encode(), headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                status, raw = response.status, response.read().decode()
                content_type = response.headers.get("Content-Type", "")
                self._mcp_session = response.headers.get("Mcp-Session-Id") or self._mcp_session
        except urllib.error.HTTPError as error:
            status, raw, content_type = error.code, error.read().decode(), ""
        except (urllib.error.URLError, OSError) as error:
            return 0, {"unreachable": type(error).__name__, "detail": str(error)[:200]}
        if "text/event-stream" in content_type:
            data_lines = [line[5:].strip() for line in raw.splitlines() if line.startswith("data:")]
            raw = data_lines[-1] if data_lines else ""
        try:
            return status, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return status, {"raw": raw[:500]}

    def connect(self, wait_seconds: int = 0) -> None:
        """Start the MCP session. `wait_seconds`: time to wait for the proxy container to listen."""
        deadline = time.time() + wait_seconds
        while True:
            status, body = self.rpc("initialize", {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "agentfarm-agent", "version": "1.0.0"},
            })
            if status != 0 or time.time() >= deadline:
                break
            time.sleep(1)
        if status != 200 or not body or "error" in body:
            raise GatewayError(json.dumps({"request": "initialize", "http_status": status, "response": body}))
        self.rpc("notifications/initialized", notification=True)

    def list_tools(self) -> list[dict]:
        status, body = self.rpc("tools/list")
        if status != 200 or not body or "error" in body:
            raise GatewayError(json.dumps({"request": "tools/list", "http_status": status, "response": body}))
        return body["result"]["tools"]

    def call_tool(self, name: str, arguments: dict) -> dict:
        """Return {"http_status", "result"} on success, or {"http_status", "response"} on a gateway error."""
        status, body = self.rpc("tools/call", {"name": name, "arguments": arguments})
        if status != 200 or not body or "error" in body:
            return {"http_status": status, "response": body}
        result = body["result"]
        text = result["content"][0]["text"] if result.get("content") else ""
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = {"raw": text}
        return {"http_status": status, "result": parsed, "is_error": bool(result.get("isError"))}
