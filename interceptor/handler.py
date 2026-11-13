"""Request interceptor of the gateway.

The gateway calls this function for every MCP request, after the JWT authorizer accepted the
token (signature, issuer, audience, expiry). The function does two things:

1. It checks that the role of an agent requested the token (the execution role of the agent runtime
   of the agent), and that the role is the role of the agent that the token names. The JWT authorizer
   of the gateway cannot do this check: its custom claims feature compares a claim with one fixed
   value, and every agent has its own role. A token that fails the check gets an error answer and
   does not reach the broker.
2. It returns the Authorization header. A Lambda target receives no caller identity by default.
   The gateway forwards the header to the broker only when a request interceptor returns it.

The function has no permissions and writes no log. The broker verifies the token again.

The setting of the function is in `config.json`, a file of the signed package. The function has no
environment variables of its own.
"""
import base64
import json
from pathlib import Path

CONFIG = json.loads(Path(__file__).with_name("config.json").read_text())
# The role that requests the JWT of an agent: arn:aws:iam::<account>:role/<prefix><agent_id>
TOKEN_ROLE_ARN_PREFIX = CONFIG["token_role_arn_prefix"]
STS_NAMESPACE = "https://sts.amazonaws.com/"


def _requested_by_the_role_of_the_agent(authorization: str) -> bool:
    try:
        payload = authorization.split(" ", 1)[1].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        agent_id = claims[STS_NAMESPACE]["request_tags"]["agent_id"]
        return isinstance(agent_id, str) and bool(agent_id) and claims["sub"] == TOKEN_ROLE_ARN_PREFIX + agent_id
    except Exception:
        return False


def handler(event, context):
    request = event["mcp"]["gatewayRequest"]
    headers = request.get("headers") or {}
    authorization = next((v for k, v in headers.items() if k.lower() == "authorization"), None)
    body = request.get("body")
    if not authorization or not _requested_by_the_role_of_the_agent(authorization):
        return {"interceptorOutputVersion": "1.0", "mcp": {"transformedGatewayResponse": {
            "statusCode": 403,
            "body": {"jsonrpc": "2.0", "id": body.get("id") if isinstance(body, dict) else None,
                     "error": {"code": -32002, "message": "The token is not a token of the role of the agent."}},
        }}}
    return {"interceptorOutputVersion": "1.0", "mcp": {"transformedGatewayRequest": {
        "body": body, "headers": {"Authorization": authorization}}}}
