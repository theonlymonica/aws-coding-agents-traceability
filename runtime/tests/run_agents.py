"""Two agents in the deployed stack AgentFarmRuntime: is each agent itself, and only itself?

Every agent has its own agent runtime, its own execution role, its own GitHub App and its own key. The
script starts one scripted session (no model) of each of two agents, at the same time and in the same
repository, and checks the result outside the system under test:

  A1  Each session does its work, and the session table has it with its agent.
  A2  The repository: the commit and the pull request of each session have the GitHub App of its agent as the
      author, with a signature of GitHub, and the identity lines of its agent.
  A3  The stack of each session has the tag of its agent and the value of its session.
  A4  The session starter: an agent gets no session for a use case that is not its own, and the state of a
      session is not given under the name of a different agent.
  A5  IAM, by the policy simulator: the execution role of an agent can request a JWT with its own agent ID and
      a repository of its own, and not with the agent ID of the other agent or with a repository that is not
      its own.
  A6  AWS CloudTrail: every event of a session has its agent as the source identity. For an agent with a KMS
      key: every signature is a signature with the KMS key of its agent, the session policies name the alias
      of its agent only, every setting that the session reads is a setting of its agent, and it reads no
      secret. For an agent without a KMS key: every read of a secret is the read of the secret of its agent,
      and the session policies name that secret only.

Usage: python runtime/tests/run_agents.py [--no-cloudtrail]      (writes evidence/runtime/agents-<start time>.json)
"""
import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from infra.names import (GITHUB_APP_KEY_ALIAS_PREFIX, GITHUB_APP_NAME_PREFIX, GITHUB_APP_SECRET_PREFIX,  # noqa: E402
                         GITHUB_APP_SETTINGS_PREFIX, RUNTIME_STACK, RUNTIME_TOKEN_ROLE_PREFIX)
from runtime.tests.repository import repository as hosted_repository  # noqa: E402
from runtime.tests.session import session_status  # noqa: E402

REGION = os.environ.get("AWS_REGION", "eu-central-1")
EVIDENCE = ROOT / "evidence" / "runtime"
USE_CASE = "greeting-service"     # a use case of both agents
WORK_WAIT, TRAIL_WAIT = 8 * 60, 15 * 60   # seconds


def gh(*arguments: str) -> str:
    done = subprocess.run(["gh", *arguments], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise LookupError(done.stderr.strip()[:200])
    return done.stdout.strip()


def main() -> int:
    session_api = boto3.session.Session(region_name=REGION)
    stack = session_api.client("cloudformation").describe_stacks(StackName=RUNTIME_STACK)["Stacks"][0]
    config = {output["OutputKey"]: output["OutputValue"] for output in stack["Outputs"]}
    registry, repositories = json.loads(config["Agents"]), json.loads(config["Repositories"])
    agents = [agent for agent, use_cases in registry.items() if USE_CASE in use_cases][:2]
    if len(agents) < 2:
        raise SystemExit("The test needs a stack with two agents that may work on the same use case.")
    account, name = stack["StackId"].split(":")[4], repositories[USE_CASE]
    owner = os.environ.get("GITHUB_OWNER") or gh("api", "user", "--jq", ".login")
    started = datetime.datetime.now(datetime.timezone.utc)
    record = EVIDENCE / f"agents-{started:%Y%m%dT%H%M%SZ}.json"
    result = {"started": started.strftime("%Y-%m-%dT%H:%M:%SZ"), "agents": agents, "registry": registry, "checks": {}}
    checks = result["checks"]

    def check(key: str, title: str, passed: bool, detail) -> None:
        checks[key] = {"name": title, "result": "PASS" if passed else "FAIL", "detail": detail}
        print(f"{key} {title}: {'PASS' if passed else 'FAIL'}\n    {json.dumps(detail, default=str)[:1100]}")

    def save() -> None:
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps(result, indent=2, default=str).replace(owner, "REDACTED"))

    functions = session_api.client("lambda", config=Config(read_timeout=120, retries={"total_max_attempts": 1}))

    def start(event: dict) -> dict:
        response = functions.invoke(FunctionName=config["StarterFunctionName"], Payload=json.dumps(event).encode())
        return json.loads(response["Payload"].read())

    # ---- one session of each agent, at the same time, in the same repository
    sessions = {}
    for agent in agents:
        answer = start({"agent_id": agent, "use_case": USE_CASE, "requested_by": "test run (run_agents.py)",
                        "mode": "scripted", "greeting": f"Hello from {agent}"})
        if not answer.get("session_id"):
            raise SystemExit(f"The session of {agent} did not start: {json.dumps(answer)[:300]}")
        sessions[agent] = {"session_id": answer["session_id"]}
        print(f"session of {agent}: {answer['session_id']}")
    deadline = time.time() + WORK_WAIT
    while time.time() < deadline:
        for agent, session in sessions.items():
            if session.get("work", {}).get("state") not in ("done", "failed"):
                session["work"] = session_status(functions, config, session["session_id"], agent)
        if all(session["work"].get("state") in ("done", "failed") for session in sessions.values()):
            break
        time.sleep(10)

    # ---- A1: the work and the session table
    table = session_api.client("dynamodb")
    for agent, session in sessions.items():
        item = table.get_item(TableName=config["SessionTableName"], Key={"session": {"S": f"{agent}/{session['session_id']}"}},
                              ConsistentRead=True).get("Item") or {}
        session["item"] = {k: list(v.values())[0] for k, v in item.items()}
        session["steps"] = [f"{step['step']}: {step['status']}" for step in session["work"].get("steps", [])]
    check("A1", "Each session does its work",
          all(session["work"].get("state") == "done" and session["item"].get("agent_id") == agent
              and session["item"].get("state") == "ended" for agent, session in sessions.items()),
          {agent: {"session_id": session["session_id"], "work": session["work"].get("state"), "steps": session["steps"],
                   "agent of the session table item": session["item"].get("agent_id")} for agent, session in sessions.items()})

    # ---- A2: the repository
    repo, passed, detail = hosted_repository(config, name, REGION), True, {}
    for agent, session in sessions.items():
        branch, app = f"agent/{session['session_id']}", f"{GITHUB_APP_NAME_PREFIX}{agent}[bot]"
        lines = f"Agent-Id: {agent}\nSession-Id: {session['session_id']}"
        try:
            head = repo.head(branch)
            commit, pull = repo.commit(head), repo.pull_request(branch)
            verified = json.loads(gh("api", f"repos/{owner}/{name}/commits/{head}", "--jq", ".commit.verification | {verified}"))["verified"]
            session["repository"] = {"head": head, "author": commit["author"], "signature_of_github": verified,
                                     "message": commit["message"], "pull_request_author": pull.get("author"),
                                     "pull_request": pull.get("id"), "greeting in the template": f"Hello from {agent}" in repo.file(head, "template.yaml")}
            ok = (commit["author"] == app and verified is True and commit["message"].rstrip().endswith(lines)
                  and pull.get("author") == app and (pull.get("description") or "").rstrip().endswith(lines)
                  and session["repository"]["greeting in the template"])
        except (LookupError, ClientError, KeyError, TypeError) as error:
            session["repository"], ok = {"error": str(error)[:200]}, False
        passed = passed and ok
        detail[agent] = {k: session["repository"].get(k) for k in ("author", "signature_of_github", "pull_request_author", "pull_request", "error")
                         if k in session["repository"]}
    check("A2", "The commit and the pull request of each session have the GitHub App of its agent", passed, detail)

    # ---- A3: the stack of each session
    cloudformation, ssm, passed, detail = session_api.client("cloudformation"), session_api.client("ssm"), True, {}
    for agent, session in sessions.items():
        stack_name = f"{config['StackPrefix']}{session['session_id']}"
        try:
            described = cloudformation.describe_stacks(StackName=stack_name)["Stacks"][0]
            tags = {t["Key"]: t["Value"] for t in described.get("Tags", [])}
            values = {p["Name"]: p["Value"] for p in ssm.get_parameters_by_path(Path=f"/agentfarm/{session['session_id']}/")["Parameters"]}
            ok = (described["StackStatus"] == "CREATE_COMPLETE" and tags.get("agent_id") == agent
                  and values == {f"/agentfarm/{session['session_id']}/greeting": f"Hello from {agent}"})
            detail[agent] = {"status": described["StackStatus"], "tag agent_id": tags.get("agent_id"), "value": list(values.values())}
        except ClientError as error:
            ok, detail[agent] = False, {"error": error.response["Error"]["Code"]}
        passed = passed and ok
    check("A3", "The stack of each session has the tag of its agent and the value of its session", passed, detail)

    # ---- A4: the session starter and the two agents
    first, second = agents
    other_use_cases = [use_case for use_case in registry[first] if use_case not in registry[second]]
    refused = start({"agent_id": second, "use_case": other_use_cases[0]}) if other_use_cases else {"error": "no such use case in the registry"}
    crossed = start({"action": "status", "agent_id": second, "session_id": sessions[first]["session_id"]})
    own = start({"action": "status", "agent_id": first, "session_id": sessions[first]["session_id"]})
    check("A4", "The session starter keeps the two agents apart",
          refused.get("error") == "rejected" and crossed.get("error") == "rejected" and "state" in own,
          {f"{second} for the use case {other_use_cases[0] if other_use_cases else '-'}": refused.get("reason") or refused,
           f"the state of the session of {first}, asked as {second}": crossed.get("reason") or crossed,
           f"the state of the session of {first}, asked as {first}": own.get("state")})

    # ---- A5: IAM, by the policy simulator
    iam, simulated, passed = session_api.client("iam"), {}, True
    runtime_arns = json.loads(config["AgentRuntimeArns"])
    sid = sessions[first]["session_id"]

    def may_tag(agent: str, agent_tag: str, repository_tag: str) -> str:
        entry = lambda key, value, kind="string": {"ContextKeyName": key, "ContextKeyValues": value if isinstance(value, list) else [value], "ContextKeyType": kind}  # noqa: E731
        answer = iam.simulate_principal_policy(
            PolicySourceArn=f"arn:aws:iam::{account}:role/{RUNTIME_TOKEN_ROLE_PREFIX}{agent}", ActionNames=["sts:TagGetWebIdentityToken"],
            ContextEntries=[entry("aws:RequestTag/agent_id", agent_tag), entry("aws:RequestTag/session_id", sid),
                            entry("aws:RequestTag/repository", repository_tag), entry("aws:AgentCoreRuntimeSourceSid", sid),
                            entry("aws:AgentCoreRuntimeSourceArn", runtime_arns[agent]),
                            entry("aws:TagKeys", ["agent_id", "session_id", "repository"], "stringList")])
        return answer["EvaluationResults"][0]["EvalDecision"]

    for agent in agents:
        other = [a for a in agents if a != agent][0]
        own_repository = repositories[registry[agent][0]]
        foreign = [repositories[u] for u in repositories if u not in registry[agent]]
        simulated[agent] = {"its own agent ID and a repository of its own": may_tag(agent, agent, own_repository),
                            f"the agent ID {other}": may_tag(agent, other, own_repository),
                            **({f"the repository {foreign[0]}, which is not its own": may_tag(agent, agent, foreign[0])} if foreign else {})}
        decisions = list(simulated[agent].values())
        passed = passed and decisions[0] == "allowed" and all(decision != "allowed" for decision in decisions[1:])
    check("A5", "IAM: the execution role of an agent requests a JWT only as itself", passed, simulated)

    # ---- A6: AWS CloudTrail
    if "--no-cloudtrail" not in sys.argv:
        save()
        trail, deadline, found = session_api.client("cloudtrail"), time.time() + TRAIL_WAIT, {}
        print("  wait for the events in AWS CloudTrail (some minutes)")
        while time.time() < deadline:
            assumes = [json.loads(e["CloudTrailEvent"]) for p in trail.get_paginator("lookup_events").paginate(
                LookupAttributes=[{"AttributeKey": "EventName", "AttributeValue": "AssumeRole"}],
                StartTime=started - datetime.timedelta(minutes=1)) for e in p["Events"]]
            for agent, session in sessions.items():
                events = [json.loads(e["CloudTrailEvent"]) for p in trail.get_paginator("lookup_events").paginate(
                    LookupAttributes=[{"AttributeKey": "Username", "AttributeValue": session["session_id"]}],
                    StartTime=started - datetime.timedelta(minutes=1)) for e in p["Events"]]
                found[agent] = (events, [e for e in assumes if (e.get("requestParameters") or {}).get("roleSessionName") == session["session_id"]])
            if all("CreateStack" in {e["eventName"] for e in events} and {"Sign", "GetSecretValue"} & {e["eventName"] for e in events}
                   and requests for events, requests in found.values()):
                break
            time.sleep(30)
        passed, detail = True, {}
        kms_agents = json.loads(config["GitHubAppKeys"]) if config.get("GitHubAppKeys", "none") != "none" else {}
        for agent, (events, requests) in found.items():
            identities = sorted({str(e["userIdentity"].get("sessionContext", {}).get("sourceIdentity")) for e in events})
            asked_as = sorted({str(e["requestParameters"].get("sourceIdentity")) for e in requests})
            statements = [statement for e in requests
                          for statement in json.loads(e["requestParameters"].get("policy", "{}")).get("Statement", [])]
            secrets_read = sorted({str((e.get("requestParameters") or {}).get("secretId")) for e in events if e["eventName"] == "GetSecretValue"})
            detail[agent] = {"events": len(events), "source identity of every event": identities, "requests for credentials": len(requests)}
            if agent in kms_agents:
                # The App of the agent: a signature with the KMS key of the agent, the IDs from the parameters of
                # the agent, and no secret.
                alias, settings = f"{GITHUB_APP_KEY_ALIAS_PREFIX}{agent}", f"{GITHUB_APP_SETTINGS_PREFIX}{agent}/"
                keys_used = sorted({str((e.get("requestParameters") or {}).get("keyId")) for e in events if e["eventName"] == "Sign"})
                key_arns = sorted({resource.get("ARN", "") for e in events if e["eventName"] == "Sign" for resource in e.get("resources") or []})
                settings_read = sorted({name for e in events if e["eventName"] == "GetParameters" and not e["userIdentity"].get("invokedBy")
                                        for name in (e.get("requestParameters") or {}).get("names") or []})
                aliases = sorted({statement["Condition"]["StringEquals"]["kms:RequestAlias"] for statement in statements
                                  if statement.get("Action") == "kms:Sign"})
                ok = (identities == [agent] and asked_as == [agent] and keys_used == [alias] and aliases == [alias]
                      and key_arns == [f"arn:aws:kms:{REGION}:{config['AgentRoleArn'].split(':')[4]}:key/{kms_agents[agent]}"]
                      and bool(settings_read) and all(name.startswith(settings) for name in settings_read) and not secrets_read)
                detail[agent].update({"keys of the signatures": keys_used, "alias in the session policies": aliases,
                                      "settings read": settings_read, "secrets read": secrets_read})
            else:
                secret = f"{GITHUB_APP_SECRET_PREFIX}{agent}"
                in_policies = sorted({resource.rsplit(":secret:", 1)[-1] for statement in statements
                                      if "secretsmanager" in json.dumps(statement.get("Action"))
                                      for resource in ([statement["Resource"]] if isinstance(statement["Resource"], str) else statement["Resource"])})
                ok = (identities == [agent] and secrets_read == [secret] and in_policies == [f"{secret}-??????"] and asked_as == [agent])
                detail[agent].update({"secrets read": secrets_read, "secrets in the session policies": in_policies})
            passed = passed and ok
        check("A6", "AWS CloudTrail: each agent acts as itself and uses the key and the IDs of its own GitHub App only", passed, detail)

    result["sessions"] = {agent: {k: v for k, v in session.items() if k != "work"} for agent, session in sessions.items()}
    save()
    print("\n" + ", ".join(f"{key}={value['result']}" for key, value in checks.items()))
    print(f"Wrote {record}")
    return 0 if all(value["result"] == "PASS" for value in checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
