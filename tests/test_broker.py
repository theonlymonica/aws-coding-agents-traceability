"""Local tests of the broker: the checks on a JWT, the identity lines, the session policy and the settings.

These tests need no AWS account. They sign JWTs with a local ES384 key and give the public key to
the broker in the place of the key set of AWS STS. AWS STS never signs a JWT of the execution role of
an agent with missing or malformed tags (the policy of the role prevents the request), so these cases
cannot run against the deployed system.

The broker reads its settings from the file `config.json` of its package. The tests import the broker
from a temporary package directory with test settings (tests/local.py).

Usage: python -m tests.test_broker        (writes .build/unit-results.json)
"""
import json
import os
import sys
import time

from tests.local import (
    ACCOUNT, IDENTITY_SETTINGS, OTHER_KEY, PUBLIC_PEM, REGION, SESSION_ID, STS_NAMESPACE, LocalKeys,
    b64, claims, package, report, save, signed,
)

# The broker reads no setting from the environment. These variables have values of an attacker: a test
# below checks that the broker does not use them.
ATTACKER = "https://issuer.attacker.example"
os.environ.update({
    "ISSUER": ATTACKER, "AUDIENCE": "agentfarm-other", "TOKEN_ROLE_PREFIX": "attacker-",
    "AGENT_ROLE_ARN": "arn:aws:iam::999999999999:role/attacker", "SESSION_TABLE": "attacker-table",
    "AWS_ENDPOINT_URL": "https://aws.attacker.example", "AWS_ENDPOINT_URL_DYNAMODB": "https://dynamodb.attacker.example",
    "AWS_ENDPOINT_URL_STS": "https://sts.attacker.example",
    "AWS_REGION": "us-west-2", "AWS_DEFAULT_REGION": "us-west-2",
})
SETTINGS = {**IDENTITY_SETTINGS, "agent_role_arn": f"arn:aws:iam::{ACCOUNT}:role/agentfarm-runtime-agent",
            "stack_prefix": "agentfarm-", "session_table": "agentfarm-runtime-sessions", "github_owner": "example-owner"}
package(["broker/handler.py", "broker/github.py", "shared/identity.py"], SETTINGS)

import handler as broker  # noqa: E402
import identity  # noqa: E402

identity._jwks = LocalKeys()


def unsigned(payload: dict) -> str:
    return f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(payload)}."


def hmac_with_the_public_key(payload: dict) -> str:
    """Algorithm confusion: an HS384 signature with the public key as the secret."""
    import base64
    import hashlib
    import hmac
    signing_input = f"{b64({'alg': 'HS384', 'typ': 'JWT', 'kid': 'local'})}.{b64(payload)}"
    signature = hmac.new(PUBLIC_PEM, signing_input.encode(), hashlib.sha384).digest()
    return f"{signing_input}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


without_tags = claims()
del without_tags[STS_NAMESPACE]["request_tags"]
namespace_is_text = {**claims(), STS_NAMESPACE: "request_tags"}
top_level_tags = {**without_tags, "agent_id": "agent-alpha", "session_id": SESSION_ID,
                  "repository": "agentfarm-greeting-service"}

TOKEN_CASES = [
    # (name, token, expected: None = accepted, text = the rejection reason must contain it)
    ("valid JWT of the execution role of the agent", signed(claims()), None),
    ("no request_tags claim", signed(without_tags), "agent_id"),
    ("the STS namespace claim is a text, not an object", signed(namespace_is_text), "agent_id"),
    ("the tags are top-level claims, not request_tags", signed(top_level_tags), "agent_id"),
    ("no agent_id tag", signed(claims(agent_id=None)), "agent_id"),
    ("agent_id is a number", signed(claims(agent_id=7)), "agent_id"),
    ("agent_id with a space and a slash", signed(claims(agent_id="agent alpha/../x")), "agent_id"),
    ("agent_id of 65 characters", signed(claims(agent_id="a" * 65)), "agent_id"),
    ("no session_id tag", signed(claims(session_id=None)), "session_id"),
    ("session_id is `*`", signed(claims(session_id="*")), "session_id"),
    ("session_id is a UUID with a wildcard at the end", signed(claims(session_id=SESSION_ID[:-1] + "*")), "session_id"),
    ("session_id is a path (`../main`)", signed(claims(session_id="../main")), "session_id"),
    ("session_id is a UUID in capital letters", signed(claims(session_id=SESSION_ID.upper())), "session_id"),
    ("session_id is a UUID with a line break and a second value",
     signed(claims(session_id=SESSION_ID + "\n" + SESSION_ID)), "session_id"),
    ("no repository tag", signed(claims(repository=None)), "repository"),
    ("repository is not a repository of the system", signed(claims(repository="agentfarm-other")), "repository"),
    ("repository is `*`", signed(claims(repository="*")), "repository"),
    ("sub is the execution role of a different agent",
     signed(claims(sub=f"arn:aws:iam::{ACCOUNT}:role/agentfarm-runtime-exec-agent-beta")), "sub"),
    ("sub is a role that is not the execution role of an agent",
     signed(claims(sub=f"arn:aws:iam::{ACCOUNT}:role/agentfarm-runtime-broker")), "sub"),
    ("sub is the name of the execution role in a different account",
     signed(claims(sub="arn:aws:iam::999999999999:role/agentfarm-runtime-exec-agent-alpha")), "sub"),
    ("a different audience", signed(claims(aud="agentfarm-other")), "invalid token"),
    ("a different issuer", signed(claims(iss="https://issuer.attacker.example")), "invalid token"),
    ("expired", signed(claims(iat=int(time.time()) - 1000, exp=int(time.time()) - 100)), "invalid token"),
    ("no exp claim", signed(claims(exp=None)), "invalid token"),
    ("no jti claim", signed(claims(jti=None)), "invalid token"),
    ("signed with a different key", signed(claims(), OTHER_KEY), "invalid token"),
    ("algorithm `none`, no signature", unsigned(claims()), "invalid token"),
    ("algorithm HS384 with the public key as the secret", hmac_with_the_public_key(claims()), "invalid token"),
    ("not a JWT", "not-a-jwt", "invalid token"),
]


def run() -> list[dict]:
    results = []

    def record(group: str, name: str, passed: bool, detail: str) -> None:
        results.append({"group": group, "case": name, "passed": passed, "detail": detail})

    for name, token, expected in TOKEN_CASES:
        try:
            verified = identity.verified_identity(token)
            outcome = f"accepted: agent {verified['agent_id']}, session {verified['session_id']}, repository {verified['repository']}"
            passed = expected is None
        except identity.Rejected as rejected:
            outcome = f"rejected: {rejected}"
            passed = expected is not None and expected in str(rejected)
        except Exception as error:   # the broker must answer with a rejection, not with an error
            outcome, passed = f"ERROR {type(error).__name__}: {error}", False
        record("JWT", name, passed, outcome)

    session = {"agent_id": "agent-alpha", "session_id": SESSION_ID, "repository": "agentfarm-greeting-service"}
    real_lines = f"Agent-Id: agent-alpha\nSession-Id: {SESSION_ID}"
    for name, text in [
        ("no identity line in the text", "Change the greeting"),
        ("false identity lines at the end", "Change the greeting\n\nAgent-Id: agent-admin\nSession-Id: 0000"),
        ("false identity lines with other capital letters and spaces", "Change\n  agent-id : agent-admin\nSESSION-ID:0000\nmore text"),
        ("false identity lines with other line separators", "Change\rAgent-Id: agent-admin\r\nSession-Id: 0000 Agent-Id: x"),
        ("a false identity key in the middle of a line", "Add a file. Agent-Id: agent-admin\nmore text"),
        ("false identity keys with other separators", "Change agent_id=agent-admin; SESSION ID : 0000, AgentId:agent-admin"),
        ("a false identity key with a non-breaking hyphen and full-width letters",
         "Change\nAgent\u2011Id: agent-admin and \uff21gent-Id\uff1a agent-admin"),
        ("empty text", ""),
        ("the text is not a string", ["Agent-Id: agent-admin"]),
    ]:
        output = broker.signed_text(text, session)
        agent_text = "\n".join(output.splitlines()[:-2])
        passed = output.endswith(real_lines) and not broker.IDENTITY_KEY.search(agent_text) \
            and "agent-admin" not in output and "0000" not in agent_text
        record("identity lines", name, passed, json.dumps(output))
    title = broker.without_identity_keys("Change the greeting. Agent-Id: agent-admin")
    record("identity lines", "a false identity key in the title of a pull request",
           title == "Change the greeting.", json.dumps(title))

    policy = broker.session_policy(SESSION_ID, "agentfarm-greeting-service")
    record("session policy", "the session policy is valid JSON and below the limit of 2,048 characters",
           len(policy) <= 2048 and bool(json.loads(policy)["Statement"]), f"{len(policy)} characters")
    record("session policy", "the session policy names the account and the Region of the package settings",
           f":{REGION}:{ACCOUNT}:" in policy and "us-west-2" not in policy, f"Region {REGION}, account {ACCOUNT}")

    # The settings come from config.json of the package. The environment has the values of an attacker.
    used = {"issuer": identity.ISSUER, "audience": identity.AUDIENCE, "token_role_prefix": identity.TOKEN_ROLE_PREFIX,
            "agent_role_arn": broker.AGENT_ROLE_ARN, "session_table": broker.SESSION_TABLE}
    record("settings", "the broker uses the settings of its package, not the environment variables of the same names",
           used == {k: SETTINGS[k] for k in used}, json.dumps(used))
    try:
        accepted = identity.verified_identity(signed(claims(iss=ATTACKER)))
        outcome, passed = f"accepted: {accepted}", False
    except identity.Rejected as rejected:
        outcome, passed = f"rejected: {rejected}", True
    record("settings", "a JWT of the issuer in the environment variable ISSUER", passed, outcome)
    endpoints = {"dynamodb": broker._sessions.meta.endpoint_url, "sts": broker._sts.meta.endpoint_url,
                 "cloudformation": broker.session_client("cloudformation", {
                     "AccessKeyId": "x", "SecretAccessKey": "x", "SessionToken": "x"}).meta.endpoint_url}
    record("settings", "the AWS clients ignore AWS_ENDPOINT_URL, AWS_ENDPOINT_URL_<SERVICE> and AWS_REGION of the environment",
           all(url == f"https://{service}.{REGION}.amazonaws.com" for service, url in endpoints.items()),
           json.dumps(endpoints))
    return results


def main() -> int:
    results = run()
    save(results, replaces=("JWT", "identity lines", "session policy", "settings"))
    return report(results)


if __name__ == "__main__":
    sys.exit(main())
