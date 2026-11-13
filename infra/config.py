"""The settings of the Lambda functions, as a file of their signed packages.

`infra/sign.py` writes `config.json` to the directory of a package before it makes the zip file and
starts the signing job. The signature of AWS Signer then covers the settings: a change of the issuer,
of the audience or of the roles that may request a JWT needs a signed package. The broker and the
interceptor have no environment variables of their own; the session starter has one, the ARNs of the
agent runtimes.

The settings are fixed names (infra/names.py), the account, the issuer of AWS STS and the owner of the
repositories on GitHub.
"""
import json
import os
import subprocess
from pathlib import Path

import boto3

from infra import names
from infra.registry import AGENTS, KMS_APP_KEY_AGENTS, USE_CASES


def repositories() -> dict[str, str]:
    return {use_case: f"{names.REPOSITORY_PREFIX}{use_case}" for use_case in USE_CASES}


def github_owner() -> str:
    """The account or the organization on GitHub that has the repositories: the variable GITHUB_OWNER, or the
    account of the GitHub CLI of the person who deploys."""
    owner = os.environ.get("GITHUB_OWNER") or subprocess.run(
        ["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True, check=False).stdout.strip()
    if not owner:
        raise SystemExit("Set GITHUB_OWNER, or sign in with the GitHub CLI (gh auth login).")
    return owner


def github_workflow_subject(owner: str, repository: str, branch: str = "main") -> str:
    """The subject (`sub`) of the OIDC token that GitHub gives to a workflow of a repository that runs from a
    branch. GitHub writes the subject with the immutable IDs of the owner and of the repository
    (`repo:<owner>@<id>/<repository>@<id>:ref:refs/heads/<branch>`), so a new repository with an old name does
    not get the subject of the old one. The prefix comes from GitHub, with the GitHub CLI of the person who
    deploys."""
    done = subprocess.run(
        ["gh", "api", f"repos/{owner}/{repository}/actions/oidc/customization/sub", "--jq", ".sub_claim_prefix // \"\""],
        capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise SystemExit(f"GitHub does not give the OIDC subject of {repository}: {done.stderr.strip()[:200]}")
    return f"{done.stdout.strip() or f'repo:{owner}/{repository}'}:ref:refs/heads/{branch}"


def github_settings() -> dict:
    """Where the repositories are, and where the broker finds the GitHub App of an agent."""
    return {"github_owner": github_owner(),
            "github_app_secret_prefix": names.GITHUB_APP_SECRET_PREFIX,
            # The agents that have a KMS key for the private key of their GitHub App.
            "github_app_kms_agents": sorted(KMS_APP_KEY_AGENTS),
            "github_app_key_alias_prefix": names.GITHUB_APP_KEY_ALIAS_PREFIX,
            "github_app_signing_algorithm": names.GITHUB_APP_SIGNING_ALGORITHM,
            "github_app_settings_prefix": names.GITHUB_APP_SETTINGS_PREFIX}


def broker(account: str, region: str, issuer: str) -> dict:
    return {**github_settings(), "issuer": issuer, "audience": names.AUDIENCE, "account": account, "region": region,
            "repositories": repositories(),
            # The execution role of the agent runtime of an agent requests the JWT.
            "token_role_prefix": names.RUNTIME_TOKEN_ROLE_PREFIX,
            "agent_role_arn": names.role_arn(account, names.RUNTIME_AGENT_ROLE),
            "stack_prefix": names.STACK_PREFIX, "session_table": names.RUNTIME_SESSION_TABLE}


def interceptor(account: str) -> dict:
    return {"token_role_arn_prefix": names.role_arn(account, names.RUNTIME_TOKEN_ROLE_PREFIX)}


def starter(region: str) -> dict:
    return {"region": region, "agents": AGENTS, "repositories": repositories(),
            "session_table": names.RUNTIME_SESSION_TABLE, "session_seconds": names.RUNTIME_SESSION_SECONDS}


def settings(package: str, region: str) -> dict:
    """The settings of a package, from the account of the current credentials."""
    account = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    federation = boto3.client("iam").get_outbound_web_identity_federation_info()
    if not federation["JwtVendingEnabled"]:
        raise SystemExit("IAM outbound identity federation is disabled in this account.")
    if package == "broker":
        return broker(account, region, federation["IssuerIdentifier"])
    if package == "interceptor":
        return interceptor(account)
    if package == "starter":
        return starter(region)
    raise SystemExit(f"no settings for the package {package}")


def write(package: str, directory: Path, region: str) -> None:
    """The same bytes for the same settings, so that an unchanged package is not signed again."""
    (directory / "config.json").write_text(json.dumps(settings(package, region), indent=2, sort_keys=True) + "\n")
