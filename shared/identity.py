"""Verification of the JWT of a session. The broker has a copy of this module in its package.

The settings are in `config.json`, a file of the signed package (infra/config.py writes it before the
signature). The module reads no setting from an environment variable: a change of the function
configuration cannot change the issuer, the audience or the roles that the code accepts.
"""
import base64
import json
import re
from pathlib import Path

import jwt
from botocore.config import Config

CONFIG = json.loads(Path(__file__).with_name("config.json").read_text())
ISSUER = CONFIG["issuer"]
AUDIENCE = CONFIG["audience"]
# The role that requests the JWT of an agent has the name <prefix><agent_id>: the execution role of the agent
# runtime of the agent (Amazon Bedrock AgentCore Runtime).
TOKEN_ROLE_PREFIX = CONFIG["token_role_prefix"]
ACCOUNT = CONFIG["account"]
REGION = CONFIG["region"]
REPOSITORIES = set(CONFIG["repositories"].values())   # {use case: repository}

STS_NAMESPACE = "https://sts.amazonaws.com/"
# Agent ID: the characters that sts:AssumeRole allows in SourceIdentity, without the characters "+=,".
AGENT_ID_PATTERN = re.compile(r"[A-Za-z0-9_.@-]{2,40}")
# Session ID: the UUID that the session starter makes. Also part of a stack name, of a branch name and of ARNs.
SESSION_ID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# Settings for every AWS client of the function: the Region of the package, and no endpoint from the
# environment (AWS_ENDPOINT_URL, AWS_ENDPOINT_URL_<SERVICE>).
AWS = {"region_name": REGION, "config": Config(ignore_configured_endpoint_urls=True)}

_jwks = jwt.PyJWKClient(f"{ISSUER}/.well-known/jwks.json")


class Rejected(Exception):
    pass


def unverified_ids(token: str) -> dict:
    """IDs that a rejected token claims. For the log only, never for a decision."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        tags = claims[STS_NAMESPACE]["request_tags"]
        return {"unverified_agent_id": str(tags.get("agent_id"))[:64],
                "unverified_session_id": str(tags.get("session_id"))[:64]}
    except Exception:
        return {}


def verified_identity(token: str) -> dict:
    """Return {"agent_id", "session_id", "repository", "jti", "exp"} from a valid JWT of the role of an agent."""
    try:
        key = _jwks.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token, key,
            algorithms=["ES384"],
            issuer=ISSUER,
            audience=AUDIENCE,
            options={"require": ["exp", "iat", "iss", "aud", "sub", "jti"]},
        )
    except jwt.PyJWTError as error:
        raise Rejected(f"invalid token: {error}") from error
    namespace = claims.get(STS_NAMESPACE)
    tags = namespace.get("request_tags") if isinstance(namespace, dict) else None
    tags = tags if isinstance(tags, dict) else {}
    agent_id, session_id, repository = tags.get("agent_id"), tags.get("session_id"), tags.get("repository")
    if not isinstance(agent_id, str) or not AGENT_ID_PATTERN.fullmatch(agent_id):
        raise Rejected("missing or invalid agent_id claim")
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
        raise Rejected("missing or invalid session_id claim")
    if not isinstance(repository, str) or repository not in REPOSITORIES:
        raise Rejected("missing or invalid repository claim")
    # Every agent has its own role. The role of a different agent cannot speak for this agent.
    if claims["sub"] != f"arn:aws:iam::{ACCOUNT}:role/{TOKEN_ROLE_PREFIX}{agent_id}":
        raise Rejected("the sub claim is not the role of the agent")
    return {"agent_id": agent_id, "session_id": session_id, "repository": repository,
            "jti": str(claims["jti"]), "exp": int(claims["exp"])}


def session_key(identity: dict) -> str:
    """The key of the session of a verified JWT in the session table: <agent_id>/<session_id>. The session
    starter writes the session with this key."""
    return f"{identity['agent_id']}/{identity['session_id']}"
