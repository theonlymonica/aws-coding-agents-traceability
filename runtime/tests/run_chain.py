"""The chain of a session, against the deployed stack AgentFarmRuntime.

One session, from the session starter to the stack:

  session starter ──▶ runtime session ──▶ JWT of the session ──▶ gateway ──▶ broker ──▶ GitHub and CloudFormation

The script checks the result with administrator credentials, outside the system under test:

  C1  The session starter makes the session ID and writes the session. The work of the session ends, and the
      session ends itself in the session table.
  C2  The repository: the commit on the branch of the session, its author and its identity lines, the pull request.
  C3  The stack of the session: its status, its tags and the deployed value.
  C4  A principal that is not the session starter cannot send a request to the agent runtime: not for a new
      session, and not for the session of this test.
  C5  The session starter rejects a use case that the agent may not work on.
  C6  No JWT in the logs of the gateway, of the broker, of the session starter and of the agent runtime.
  C7  CloudTrail: the events of the session have the agent ID as the source identity and the session ID as
      the role session name, and the requests for its credentials have the hash of its task as a session tag.

Usage: python runtime/tests/run_chain.py [--no-cloudtrail]        (writes evidence/runtime/chain-<start time>.json)

--no-cloudtrail skips C7, which waits several minutes for the events.
"""
import datetime
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from infra.names import RUNTIME_STACK  # noqa: E402
from runtime.tests.repository import repository as hosted_repository  # noqa: E402
from runtime.tests.session import session_status  # noqa: E402

REGION = os.environ.get("AWS_REGION", "eu-central-1")
EVIDENCE = ROOT / "evidence" / "runtime"
JWT_PATTERN = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
# An installation access token of a GitHub App, and a private key in PEM form.
GITHUB_SECRET_PATTERN = re.compile(r"gh[spour]_[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----")
WORK_WAIT = 6 * 60          # seconds, for the scripted work of a session
CLOUDTRAIL_WAIT = 14 * 60   # seconds; CloudTrail delivers events with a delay of several minutes
GREETING = "Good morning, world"


def client(service: str, **kwargs):
    return boto3.client(service, region_name=REGION, **kwargs)


def main() -> int:
    stack = client("cloudformation").describe_stacks(StackName=RUNTIME_STACK)["Stacks"][0]
    config = {output["OutputKey"]: output["OutputValue"] for output in stack["Outputs"]}
    agent_id, repository = config["AgentId"], config["Repository"]
    runtime = client("bedrock-agentcore", config=Config(read_timeout=120, retries={"total_max_attempts": 2}))
    functions = client("lambda", config=Config(read_timeout=120, retries={"total_max_attempts": 1}))
    started = datetime.datetime.now(datetime.timezone.utc)
    # One file for each run: a later run does not write over the record of an earlier run.
    record = EVIDENCE / f"chain-{started:%Y%m%dT%H%M%SZ}.json"
    result = {"started": started.strftime("%Y-%m-%dT%H:%M:%SZ"), "config": config, "checks": {}}
    checks = result["checks"]

    def check(key: str, name: str, passed: bool, detail) -> None:
        checks[key] = {"name": name, "result": "PASS" if passed else "FAIL", "detail": detail}
        print(f"{key} {name}: {'PASS' if passed else 'FAIL'}\n    {json.dumps(detail, default=str)[:900]}")

    def start(event: dict) -> dict:
        response = functions.invoke(FunctionName=config["StarterFunctionName"], Payload=json.dumps(event).encode())
        return json.loads(response["Payload"].read())

    def ask(session_id: str, request: dict) -> dict:
        try:
            response = runtime.invoke_agent_runtime(
                agentRuntimeArn=config["AgentRuntimeArn"], runtimeSessionId=session_id,
                payload=json.dumps(request).encode(), contentType="application/json", accept="application/json")
            return json.loads(response["response"].read() or b"{}")
        except ClientError as error:
            return {"error_code": error.response["Error"]["Code"], "message": error.response["Error"]["Message"][:300]}

    def wait_for_work(session_id: str) -> dict:
        deadline, status = time.time() + WORK_WAIT, {}
        while time.time() < deadline:
            status = session_status(functions, config, session_id)
            if status.get("state") in ("done", "failed"):
                break
            time.sleep(10)
        return status

    def steps_of(status: dict) -> dict:
        """The last answer of the broker for each step of the work."""
        answers = {}
        for step in status.get("steps", []):
            try:
                answers[step["step"]] = json.loads(step["output"])
            except (json.JSONDecodeError, TypeError):
                answers[step["step"]] = {"text": step.get("output")}
        return answers

    # ---- C1: one session from the session starter
    print(f"Start a session of {agent_id} for the use case greeting-service")
    session = start({"agent_id": agent_id, "use_case": "greeting-service", "requested_by": "test run (run_chain.py)",
                     "mode": "scripted", "session_id": "selected-by-the-caller-000000000000000000"})
    session_id = session.get("session_id", "")
    result["session"] = session
    print(f"  session {session_id}: wait for the work")
    status = wait_for_work(session_id) if session_id else {}
    result["work"] = status
    steps = steps_of(status)
    item = client("dynamodb").get_item(TableName=config["SessionTableName"], Key={
        "session": {"S": f"{agent_id}/{session_id}"}}, ConsistentRead=True).get("Item") or {}
    result["session_table_item"] = {k: list(v.values())[0] for k, v in item.items()}
    check("C1", "Session from the session starter",
          bool(re.fullmatch(r"[0-9a-f-]{36}", session_id)) and "selected-by-the-caller" not in json.dumps(session)
          and status.get("state") == "done" and item.get("state", {}).get("S") == "ended"
          and steps.get("end_session", {}).get("ended") is True
          and status.get("runtime_session_id_header") == session_id,
          {"session_id": session_id, "work": status.get("state"),
           "steps": [f"{s['step']}: {s['status']}" for s in status.get("steps", [])][:14],
           "session_table_item": result["session_table_item"]})

    # ---- C2: the repository
    repo, branch = hosted_repository(config, repository, REGION), f"agent/{session_id}"
    code = {"host": repo.host}
    try:
        head = repo.head(branch)
        if head:
            code.update({"head": head, **repo.commit(head), "pull_request": repo.pull_request(branch)})
        else:
            code["error"] = "the session has no branch"
    except (ClientError, LookupError) as error:
        code["error"] = str(error)[:200]
    result["repository"] = code
    lines = f"Agent-Id: {agent_id}\nSession-Id: {session_id}"
    check("C2", "Repository",
          repo.author_of(agent_id, code.get("author", "")) and code.get("message", "").rstrip().endswith(lines)
          and (code.get("pull_request", {}).get("description") or "").rstrip().endswith(lines)
          and code.get("pull_request", {}).get("source_commit") == code.get("head")
          and steps.get("commit_changes", {}).get("commit_id") == code.get("head"), code)

    # ---- C3: the stack of the session
    stack_name = f"{config['StackPrefix']}{session_id}"
    deployed = {"stack_name": stack_name}
    try:
        described = client("cloudformation").describe_stacks(StackName=stack_name)["Stacks"][0]
        deployed.update(status=described["StackStatus"], tags={t["Key"]: t["Value"] for t in described.get("Tags", [])})
        parameters = client("ssm").get_parameters_by_path(Path=f"/agentfarm/{session_id}/")["Parameters"]
        deployed["parameters"] = {p["Name"]: p["Value"] for p in parameters}
    except ClientError as error:
        deployed["error"] = error.response["Error"]["Code"]
    result["stack"] = deployed
    check("C3", "Stack of the session",
          deployed.get("status") == "CREATE_COMPLETE" and deployed.get("tags", {}).get("session_id") == session_id
          and deployed.get("tags", {}).get("agent_id") == agent_id and deployed.get("tags", {}).get("commit_id") == code.get("head")
          and deployed.get("parameters") == {f"/agentfarm/{session_id}/greeting": GREETING}, deployed)

    # ---- C4: a request to the agent runtime from a principal that is not the session starter
    stray = str(uuid.uuid4())
    print(f"A runtime session without the session starter: {stray}")
    direct = ask(stray, {"action": "work", "repository": repository, "mode": "scripted"})
    active = ask(session_id, {"action": "status"})
    result["stray_session"] = {"session_id": stray, "answer": direct, "request to the session of this test": active}
    stray_branch = repo.head(f"agent/{stray}")
    check("C4", "Runtime session without the session starter",
          direct.get("error_code") == "AccessDeniedException" and active.get("error_code") == "AccessDeniedException"
          and stray_branch is None,
          {"session_id": stray, "a new session, direct request": direct.get("error_code") or "accepted",
           "the session of this test, direct request": active.get("error_code") or "accepted", "branch": stray_branch})

    # ---- C5: the session starter and the agent registry
    refused = start({"agent_id": agent_id, "use_case": "payroll"})
    unknown = start({"agent_id": "agent-omega", "use_case": "greeting-service"})
    result["refused"] = {"use case": refused, "agent": unknown}
    check("C5", "Session starter and the agent registry",
          refused.get("error") == "rejected" and unknown.get("error") == "rejected", result["refused"])

    # ---- C6: no JWT in the logs
    logs = client("logs")
    groups = {"gateway": config["GatewayLogGroupName"], "broker": config["BrokerLogGroupName"],
              "session starter": config["StarterLogGroupName"],
              "agent runtime": f"/aws/bedrock-agentcore/runtimes/{config['AgentRuntimeId']}-DEFAULT"}
    time.sleep(45)   # the vended log of the gateway arrives some seconds after a request
    searched = {}
    for name, group in groups.items():
        try:
            pages = logs.get_paginator("filter_log_events").paginate(
                logGroupName=group, startTime=int(started.timestamp() * 1000))
            messages = [event["message"] for page in pages for event in page["events"]]
            text = "\n".join(messages)
            searched[name] = {"log_group": group, "events": len(messages), "characters": len(text),
                              "jwt_shaped_strings": len(JWT_PATTERN.findall(text)),
                              "github_tokens_and_private_keys": len(GITHUB_SECRET_PATTERN.findall(text)),
                              "has the session ID": session_id in text}
            if name == "gateway":
                result["gateway_log_sample"] = messages[:6]
        except ClientError as error:
            searched[name] = {"log_group": group, "error": error.response["Error"]["Code"]}
    result["logs"] = searched
    check("C6", "No JWT in the logs",
          all(v.get("jwt_shaped_strings") == 0 and v.get("github_tokens_and_private_keys") == 0 for v in searched.values())
          and all(searched[name].get("events", 0) > 0 for name in ("gateway", "broker", "session starter")), searched)
    (EVIDENCE).mkdir(parents=True, exist_ok=True)
    (record).write_text(json.dumps(result, indent=2, default=str))

    if "--no-cloudtrail" in sys.argv:
        print("\n" + ", ".join(f"{key}={value['result']}" for key, value in checks.items()) + " (without C7)")
        return 0 if all(value["result"] == "PASS" for value in checks.values()) else 1

    # ---- C7: CloudTrail
    print(f"\nPoll CloudTrail for the events of session {session_id}, for {CLOUDTRAIL_WAIT // 60} minutes at most")
    cloudtrail, deadline, events = client("cloudtrail"), time.time() + CLOUDTRAIL_WAIT, []
    # The commit and the pull request on GitHub are not events of AWS CloudTrail: the event of the repository is the
    # signature of the JWT of the GitHub App by AWS KMS, or, for an agent without a KMS key, the read of the secret
    # of the App.
    kms_agents = json.loads(config["GitHubAppKeys"]) if config.get("GitHubAppKeys", "none") != "none" else {}
    app_event = "Sign" if config["AgentId"] in kms_agents else "GetSecretValue"
    needed = {"AssumeRole", "CreateStack", "PutParameter", app_event}
    while time.time() < deadline:
        found = {}
        for attribute, value in (("Username", session_id), ("Username", stray), ("EventName", "AssumeRole")):
            pages = cloudtrail.get_paginator("lookup_events").paginate(
                LookupAttributes=[{"AttributeKey": attribute, "AttributeValue": value}], StartTime=started)
            for page in pages:
                for entry in page["Events"]:
                    event = json.loads(entry["CloudTrailEvent"])
                    text = json.dumps([event.get("userIdentity"), event.get("requestParameters")])
                    if session_id in text or stray in text:
                        found[event["eventID"]] = event
        events = sorted(found.values(), key=lambda e: e["eventTime"])
        names = {e["eventName"] for e in events if session_id in json.dumps([e.get("userIdentity"), e.get("requestParameters")])}
        print(f"  {len(events)} events: {sorted(names)}")
        if needed <= names:
            break
        time.sleep(30)
    result["cloudtrail"] = events
    own = [e for e in events if e["userIdentity"].get("arn", "").endswith(f"/{session_id}")]
    assumes = [e for e in events if e["eventName"] == "AssumeRole"
               and (e.get("requestParameters") or {}).get("roleSessionName") == session_id]
    attributed = bool(own) and all(
        e["userIdentity"].get("sessionContext", {}).get("sourceIdentity") == agent_id
        and e["userIdentity"]["arn"].split("/")[1] == config["AgentRoleArn"].rsplit("/", 1)[-1] for e in own) \
        and bool(assumes) and all(e["requestParameters"].get("sourceIdentity") == agent_id for e in assumes)
    stray_events = [e for e in events if stray in json.dumps([e.get("userIdentity"), e.get("requestParameters")])]
    # The session tags of every AssumeRole request: the hash of the task that the session starter recorded.
    # The session of this test has no issue, so it has no tag `issue`.
    tags = sorted({json.dumps(sorted((t["key"], t["value"]) for t in (e["requestParameters"].get("tags") or []))) for e in assumes})
    tagged = tags == [json.dumps([["task_sha256", session.get("task_sha256", "")]])] and len(session.get("task_sha256", "")) == 64
    check("C7", "CloudTrail",
          attributed and needed <= {e["eventName"] for e in own + assumes} and not stray_events
          and tagged,
          {"events of the session with the agent role": len(own), "AssumeRole requests of the broker": len(assumes),
           "event names": sorted({e["eventName"] for e in own}),
           "source identity of every event": sorted({str(e["userIdentity"].get("sessionContext", {}).get("sourceIdentity")) for e in own}),
           "role session name of every event": sorted({e["userIdentity"]["arn"].rsplit("/", 1)[-1] for e in own}),
           "caller of AssumeRole": sorted({e["userIdentity"].get("arn", "") for e in assumes}),
           "session tags of every AssumeRole request": tags,
           "the tag task_sha256 is the hash that the session starter recorded": tagged,
           "events of the runtime session without the session starter": len(stray_events)})
    (record).write_text(json.dumps(result, indent=2, default=str))

    print("\n" + ", ".join(f"{key}={value['result']}" for key, value in checks.items()))
    print(f"Wrote {record}")
    return 0 if all(value["result"] == "PASS" for value in checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
