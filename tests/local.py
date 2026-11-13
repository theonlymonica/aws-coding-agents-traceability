"""Helpers of the local tests: a package directory with test settings, a local signing key, and JWTs.

A function of the system reads its settings from `config.json` in its package directory, not from the
environment. A local test makes a temporary directory with the source files of the package and with
test settings, and imports the handler from there. Each local test runs in its own process, so that the
modules `handler` and `identity` of one package do not stay in the place of those of a different package.
"""
import base64
import json
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / ".build" / "unit-results.json"
ACCOUNT = "111122223333"
REGION = "eu-central-1"
ISSUER = "https://issuer.test.example"
AUDIENCE = "agentfarm-gateway"
STS_NAMESPACE = "https://sts.amazonaws.com/"
REPOSITORIES = {"greeting-service": "agentfarm-greeting-service", "feature-flags": "agentfarm-feature-flags"}
IDENTITY_SETTINGS = {"issuer": ISSUER, "audience": AUDIENCE, "token_role_prefix": "agentfarm-runtime-exec-",
                     "account": ACCOUNT, "region": REGION, "repositories": REPOSITORIES}

KEY = ec.generate_private_key(ec.SECP384R1())
OTHER_KEY = ec.generate_private_key(ec.SECP384R1())
PUBLIC_PEM = KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
SESSION_ID = str(uuid.uuid4())


def package(sources: list[str], settings: dict) -> Path:
    """A package directory with the source files (paths from the root) and `config.json`, on the import path."""
    directory = Path(tempfile.mkdtemp(prefix="agentfarm-local-test-"))
    for source in sources:
        path = ROOT / source
        if path.is_dir():
            shutil.copytree(path, directory / path.name)
        else:
            shutil.copy(path, directory / path.name)
    (directory / "config.json").write_text(json.dumps(settings))
    sys.path.insert(0, str(directory))
    return directory


class LocalKeys:
    """Stands in for the key set of AWS STS."""
    def get_signing_key_from_jwt(self, token):
        return type("SigningKey", (), {"key": KEY.public_key()})()


def claims(**changes) -> dict:
    """The claims of a JWT of the execution role of agent-alpha. A change with the value None removes the claim."""
    now = int(time.time())
    base = {
        "iss": ISSUER, "aud": AUDIENCE, "sub": f"arn:aws:iam::{ACCOUNT}:role/agentfarm-runtime-exec-agent-alpha",
        "iat": now, "exp": now + 900, "jti": str(uuid.uuid4()),
        STS_NAMESPACE: {"request_tags": {
            "agent_id": "agent-alpha", "session_id": SESSION_ID, "repository": "agentfarm-greeting-service"}},
    }
    tags = base[STS_NAMESPACE]["request_tags"]
    for name, value in changes.items():
        target = tags if name in ("agent_id", "session_id", "repository") else base
        if value is None:
            target.pop(name, None)
        else:
            target[name] = value
    return base


def signed(payload: dict, key=KEY) -> str:
    return jwt.encode(payload, key, algorithm="ES384", headers={"kid": "local"})


def b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def save(results: list[dict], replaces: tuple[str, ...]) -> None:
    """Write the results to .build/unit-results.json, in the place of the earlier results of the same groups."""
    RESULTS.parent.mkdir(exist_ok=True)
    earlier = json.loads(RESULTS.read_text()) if RESULTS.exists() else []
    kept = [r for r in earlier if not r["group"].startswith(replaces)]
    RESULTS.write_text(json.dumps(kept + results, indent=2))


def report(results: list[dict]) -> int:
    for r in results:
        print(f"{'PASS' if r['passed'] else 'FAIL'}  {r['group']}: {r['case']} -> {r['detail'][:150]}")
    failed = [r for r in results if not r["passed"]]
    print(f"\n{len(results) - len(failed)} of {len(results)} local tests passed")
    return 1 if failed else 0
