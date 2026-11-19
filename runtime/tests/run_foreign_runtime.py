"""A second agent runtime with the execution role of an agent: does it get the JWT of a session of that agent?

The JWT of a session names an agent, a session and a repository, and the broker gives the credentials of that
session to the holder of the JWT. IAM gives this JWT only to the credentials of a runtime session of the agent
runtime of that agent: the policy of the execution role requires the ARN of that agent runtime
(`aws:AgentCoreRuntimeSourceArn`) and takes the session ID from the runtime session
(`aws:AgentCoreRuntimeSourceSid`). AgentCore Runtime accepts every runtime session ID of a caller. A principal
with the permissions to make an agent runtime and to pass the execution role (`CreateAgentRuntime`,
`iam:PassRole`) can make a second agent runtime with the same role, and invoke it with the ID of an existing
session of the agent. The script does this, as that principal, with the image of the agents:

  R1  The second agent runtime starts, and its runtime session has the credentials of the execution role of
      the agent.
  R2  IAM denies to that runtime session a JWT with the agent ID of the agent and the ID of an existing
      session of the agent as the session ID.
  R3  IAM, by the policy simulator: the execution role gives the JWT of a session to the agent runtime of the
      agent, and denies the same request from a different agent runtime.

The script deletes the second agent runtime at the end, also when a check fails.

Usage: python runtime/tests/run_foreign_runtime.py [--agent <agent ID>]
       (writes evidence/runtime/foreign-runtime-<start time>.json; the default agent is the first agent of the stack)
"""
import datetime
import json
import os
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from infra.names import AUDIENCE, JWT_TAGS, RUNTIME_STACK, RUNTIME_TOKEN_ROLE_PREFIX, TOKEN_LIFETIME_SECONDS, role_arn  # noqa: E402

REGION = os.environ.get("AWS_REGION", "eu-central-1")
EVIDENCE = ROOT / "evidence" / "runtime"
READY_WAIT = 10 * 60   # seconds


def main() -> int:
    api = boto3.session.Session(region_name=REGION)
    stack = api.client("cloudformation").describe_stacks(StackName=RUNTIME_STACK)["Stacks"][0]
    config = {output["OutputKey"]: output["OutputValue"] for output in stack["Outputs"]}
    account = stack["StackId"].split(":")[4]
    agent = sys.argv[sys.argv.index("--agent") + 1] if "--agent" in sys.argv else config["AgentId"]
    runtime_arns = json.loads(config["AgentRuntimeArns"]) if "AgentRuntimeArns" in config else {
        config["AgentId"]: config["AgentRuntimeArn"]}
    agent_runtime_arn = runtime_arns[agent]
    execution_role = role_arn(account, f"{RUNTIME_TOKEN_ROLE_PREFIX}{agent}")
    started = datetime.datetime.now(datetime.timezone.utc)
    record = EVIDENCE / f"foreign-runtime-{started:%Y%m%dT%H%M%SZ}.json"
    result = {"started": started.strftime("%Y-%m-%dT%H:%M:%SZ"), "agent": agent,
              "agent_runtime_of_the_agent": agent_runtime_arn, "execution_role": execution_role, "checks": {}}
    checks = result["checks"]

    def check(key: str, title: str, passed: bool, detail) -> None:
        checks[key] = {"name": title, "result": "PASS" if passed else "FAIL", "detail": detail}
        print(f"{key} {title}: {'PASS' if passed else 'FAIL'}\n    {json.dumps(detail, default=str)[:1100]}")

    def save() -> None:
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps(result, indent=2, default=str))

    control = api.client("bedrock-agentcore-control")
    data = api.client("bedrock-agentcore", config=Config(read_timeout=300, retries={"total_max_attempts": 1}))

    # An existing session of the agent: the newest item of the session table.
    items = [item for page in api.client("dynamodb").get_paginator("scan").paginate(TableName=config["SessionTableName"])
             for item in page["Items"] if item["agent_id"]["S"] == agent]
    target = max(items, key=lambda item: int(item["started_at"]["N"]))
    session_id = target["session_id"]["S"]
    result["existing_session"] = {"session_id": session_id, "state": target["state"]["S"],
                                  "repository": target["repository"]["S"],
                                  "started_at": datetime.datetime.fromtimestamp(int(target["started_at"]["N"]),
                                                                                datetime.timezone.utc).isoformat()}
    print(f"Agent {agent}, agent runtime {agent_runtime_arn}\nExisting session {session_id} ({target['state']['S']})")

    # The second agent runtime: the image, the settings and the execution role of the agent runtime of the agent.
    # No box and no workspace: the probe needs neither.
    original = control.get_agent_runtime(agentRuntimeId=agent_runtime_arn.rsplit("/", 1)[-1])
    environment = {k: v for k, v in (original.get("environmentVariables") or {}).items() if k != "CODE_INTERPRETER_ID"}
    environment["AGENT_BOX"] = "none"
    name = f"agentfarm_foreign_{agent}".replace("-", "_")
    foreign_arn, foreign_id = None, None
    try:
        print(f"Make the agent runtime {name} with the execution role {execution_role}")
        made = control.create_agent_runtime(
            agentRuntimeName=name, description=f"Test: a second agent runtime with the execution role of {agent}",
            agentRuntimeArtifact=original["agentRuntimeArtifact"], roleArn=execution_role,
            networkConfiguration={"networkMode": "PUBLIC"}, protocolConfiguration={"serverProtocol": "HTTP"},
            platformVersion=original.get("platformVersion") or config["PlatformVersion"], environmentVariables=environment)
        foreign_arn, foreign_id = made["agentRuntimeArn"], made["agentRuntimeId"]
        result["second_agent_runtime"] = {"arn": foreign_arn, "name": name}
        deadline, status = time.time() + READY_WAIT, made.get("status")
        while status not in ("READY", "CREATE_FAILED") and time.time() < deadline:
            time.sleep(10)
            status = control.get_agent_runtime(agentRuntimeId=foreign_id)["status"]
        result["second_agent_runtime"]["status"] = status
        print(f"  {status}")
        save()

        print(f"Invoke {name} with the session ID {session_id}: the probe requests the JWTs")
        probe: dict = {}
        if status == "READY":
            try:
                response = data.invoke_agent_runtime(
                    agentRuntimeArn=foreign_arn, runtimeSessionId=session_id, contentType="application/json",
                    accept="application/json", payload=json.dumps({"action": "probe"}).encode())
                probe = json.loads(response["response"].read())
            except (ClientError, json.JSONDecodeError) as error:
                probe = {"error": str(error)[:400]}
            try:
                data.stop_runtime_session(agentRuntimeArn=foreign_arn, runtimeSessionId=session_id)
            except ClientError:
                pass
        result["probe"] = {k: probe.get(k) for k in ("runtime_session_id_header", "identity_of_the_session_credentials",
                                                      "jwt", "error")}
        identity = (probe.get("identity_of_the_session_credentials") or {}).get("arn", "")
        check("R1", "The second agent runtime starts, and its runtime session has the credentials of the execution role",
              status == "READY" and probe.get("runtime_session_id_header") == session_id
              and f":assumed-role/{RUNTIME_TOKEN_ROLE_PREFIX}{agent}/" in identity,
              {"status": status, "runtime session ID": probe.get("runtime_session_id_header"), "identity": identity})
        answers = probe.get("jwt") or {}
        own = answers.get("control: the runtime session ID of this session", {})
        check("R2", "IAM denies to the second agent runtime a JWT with the ID of an existing session of the agent",
              bool(answers) and own.get("outcome") == "DENIED"
              and all(answer.get("outcome") in ("DENIED", "NOT RUN") for answer in answers.values()),
              {request: {k: answer.get(k) for k in ("outcome", "error_code", "request_tags", "claims") if answer.get(k)}
               for request, answer in answers.items()})
        save()

        # R3: the same request from the two agent runtimes, by the policy simulator.
        iam = api.client("iam")
        repository = target["repository"]["S"]

        def decisions(source_arn: str) -> dict:
            entry = lambda key, value, kind="string": {"ContextKeyName": key, "ContextKeyValues": value if isinstance(value, list) else [value], "ContextKeyType": kind}  # noqa: E731
            answer = iam.simulate_principal_policy(
                PolicySourceArn=execution_role, ActionNames=["sts:GetWebIdentityToken", "sts:TagGetWebIdentityToken"],
                ContextEntries=[entry("aws:RequestTag/agent_id", agent), entry("aws:RequestTag/session_id", session_id),
                                entry("aws:RequestTag/repository", repository), entry("aws:TagKeys", JWT_TAGS, "stringList"),
                                entry("aws:AgentCoreRuntimeSourceSid", session_id),
                                entry("aws:AgentCoreRuntimeSourceArn", source_arn),
                                entry("sts:IdentityTokenAudience", [AUDIENCE], "stringList"),
                                entry("sts:DurationSeconds", str(TOKEN_LIFETIME_SECONDS), "numeric"),
                                entry("sts:SigningAlgorithm", "ES384")])
            return {r["EvalActionName"]: r["EvalDecision"] for r in answer["EvaluationResults"]}

        simulated = {"the agent runtime of the agent": decisions(agent_runtime_arn),
                     "the second agent runtime": decisions(foreign_arn)}
        check("R3", "IAM: the JWT of a session only for the agent runtime of the agent",
              all(d == "allowed" for d in simulated["the agent runtime of the agent"].values())
              and all(d != "allowed" for d in simulated["the second agent runtime"].values()), simulated)
    finally:
        if foreign_id:
            print(f"Delete the agent runtime {name}")
            try:
                control.delete_agent_runtime(agentRuntimeId=foreign_id)
                deadline = time.time() + READY_WAIT
                while time.time() < deadline:
                    time.sleep(10)
                    control.get_agent_runtime(agentRuntimeId=foreign_id)
                result.setdefault("second_agent_runtime", {})["deleted"] = "requested; still listed after the wait"
            except ClientError as error:
                code = error.response["Error"]["Code"]
                result.setdefault("second_agent_runtime", {})["deleted"] = "deleted" if code == "ResourceNotFoundException" else code
            print(f"  {result['second_agent_runtime']['deleted']}")
        save()

    print(f"\nWrote {record}")
    return 0 if checks and all(c["result"] == "PASS" for c in checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
