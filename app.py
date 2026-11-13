#!/usr/bin/env python3
import json
import os
from pathlib import Path

import aws_cdk as cdk
import boto3

from infra import names
from infra.config import github_owner, github_workflow_subject
from infra.registry import AGENTS, KMS_APP_KEY_AGENTS, MODEL_ID, USE_CASES
from infra.signing import SigningStack
from runtime.infra.stack import RuntimeStack

region = os.environ.get("AWS_REGION", "eu-central-1")
account = boto3.client("sts", region_name=region).get_caller_identity()["Account"]

# The STS issuer of the account. It exists only when IAM outbound identity federation is enabled.
federation = boto3.client("iam").get_outbound_web_identity_federation_info()
if not federation["JwtVendingEnabled"]:
    raise SystemExit("IAM outbound identity federation is disabled in this account.")

# The execution role of an agent may call exactly this inference profile and the foundation model behind it.
profile = boto3.client("bedrock", region_name=region).get_inference_profile(inferenceProfileIdentifier=MODEL_ID)

app = cdk.App()
environment = cdk.Environment(account=account, region=region)

# The signing stack exists first. `make runtime-deploy` then signs the packages of the Lambda functions
# (infra/sign.py), and the stack AgentFarmRuntime uses the signed packages.
SigningStack(app, names.SIGNING_STACK, env=environment)
signed_file = Path(__file__).resolve().parent / ".build" / "signed.json"
signed = json.loads(signed_file.read_text()) if signed_file.exists() else {"packages": {}}
packages = signed["packages"]

# The agents in Amazon Bedrock AgentCore Runtime, platform version V2, without a VPC. One agent runtime for each
# agent of the stack. The agents of the stack: the variable RUNTIME_AGENTS, a list of agent IDs of the registry
# with commas between them. Every agent of the list needs its GitHub App.
RUNTIME_AGENTS = {agent.strip(): AGENTS[agent.strip()] for agent in os.environ.get("RUNTIME_AGENTS", "agent-alpha,agent-beta").split(",")}
if all(name in packages for name in ("broker", "interceptor", "starter")):
    OWNER = github_owner()
    RuntimeStack(app, names.RUNTIME_STACK, issuer=federation["IssuerIdentifier"], agents=RUNTIME_AGENTS,
                 use_cases=USE_CASES, signed=signed, model_id=MODEL_ID,
                 inference_profile_arn=profile["inferenceProfileArn"],
                 foundation_model_arns=sorted(model["modelArn"] for model in profile["models"]),
                 github_owner=OWNER,
                 github_subjects={f"{names.REPOSITORY_PREFIX}{use_case}": github_workflow_subject(
                     OWNER, f"{names.REPOSITORY_PREFIX}{use_case}") for use_case in USE_CASES},
                 # The agents whose sessions do their work inside a box of Strands Box: the variable RUNTIME_BOX_AGENTS.
                 box_agents=[agent.strip() for agent in os.environ.get("RUNTIME_BOX_AGENTS", "").split(",") if agent.strip()],
                 box_mode=os.environ.get("RUNTIME_BOX_MODE") or "agent",
                 # The agents whose GitHub App has its private key in AWS KMS (infra/registry.py).
                 kms_app_key_agents=[agent for agent in KMS_APP_KEY_AGENTS if agent in RUNTIME_AGENTS],
                 env=environment)
app.synth()
