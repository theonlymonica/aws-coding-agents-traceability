"""Many sessions of one agent in the deployed stack AgentFarmRuntime: is each session itself, and only itself?

One agent can have many sessions at the same time. Every session has its own microVM, its own JWT, its own
branch, its own stack and its own SSM path, and the credentials of a session are for that session only. The
script starts two scripted sessions (no model) of the same agent, at the same time and in the same
repository. Then it starts two more sessions of the same agent, whose templates ask AWS CloudFormation for a
parameter in the path of the first session. It checks the result outside the system under test:

  P1  Two sessions of the same agent run at the same time, and each does its work.
  P2  Each session has its own branch, commit, pull request, stack and SSM parameter, with its own value and
      its own session ID, and nothing of the other session.
  P3  A third session, whose template names a new parameter in the path of the first session: its stack is not
      complete, the reason is an authorization error of IAM, and the path of the first session has no new
      parameter.
  P4  A fourth session, whose template names the parameter of the first session itself: its stack is not
      complete, and the parameter of the first session is not changed.
  P5  IAM, by the policy simulator: the credentials of a runtime session can request a JWT with the ID of that
      session, and not with the ID of a different session of the same agent.
  P6  AWS CloudTrail: the events of each session name only the resources of that session, and the denied
      request of the third session has the ID of the third session.

Usage: python runtime/tests/run_sessions.py [--no-cloudtrail]     (writes evidence/runtime/sessions-<start time>.json)
"""
import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from infra.names import RUNTIME_STACK, RUNTIME_TOKEN_ROLE_PREFIX  # noqa: E402
from runtime.tests.repository import repository as hosted_repository  # noqa: E402
from runtime.tests.session import session_status  # noqa: E402

REGION = os.environ.get("AWS_REGION", "eu-central-1")
EVIDENCE = ROOT / "evidence" / "runtime"
USE_CASE = "greeting-service"
WORK_WAIT, TRAIL_WAIT = 8 * 60, 15 * 60   # seconds


def gh(*arguments: str) -> str:
    done = subprocess.run(["gh", *arguments], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise LookupError(done.stderr.strip()[:200])
    return done.stdout.strip()


def main() -> int:
    api = boto3.session.Session(region_name=REGION)
    stack = api.client("cloudformation").describe_stacks(StackName=RUNTIME_STACK)["Stacks"][0]
    config = {output["OutputKey"]: output["OutputValue"] for output in stack["Outputs"]}
    agent, account = config["AgentId"], stack["StackId"].split(":")[4]
    name = json.loads(config["Repositories"])[USE_CASE]
    owner = os.environ.get("GITHUB_OWNER") or gh("api", "user", "--jq", ".login")
    started = datetime.datetime.now(datetime.timezone.utc)
    record = EVIDENCE / f"sessions-{started:%Y%m%dT%H%M%SZ}.json"
    result = {"started": started.strftime("%Y-%m-%dT%H:%M:%SZ"), "agent": agent, "checks": {}}
    checks = result["checks"]

    def check(key: str, title: str, passed: bool, detail) -> None:
        checks[key] = {"name": title, "result": "PASS" if passed else "FAIL", "detail": detail}
        print(f"{key} {title}: {'PASS' if passed else 'FAIL'}\n    {json.dumps(detail, default=str)[:1100]}")

    def save() -> None:
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps(result, indent=2, default=str).replace(owner, "REDACTED"))

    functions = api.client("lambda", config=Config(read_timeout=120, retries={"total_max_attempts": 1}))
    table, cloudformation, ssm = api.client("dynamodb"), api.client("cloudformation"), api.client("ssm")

    def start(greeting: str, **more) -> str:
        answer = json.loads(functions.invoke(FunctionName=config["StarterFunctionName"], Payload=json.dumps({
            "agent_id": agent, "use_case": USE_CASE, "requested_by": "test run (run_sessions.py)", "mode": "scripted",
            "greeting": greeting, **more}).encode())["Payload"].read())
        if not answer.get("session_id"):
            raise SystemExit(f"The session did not start: {json.dumps(answer)[:300]}")
        return answer["session_id"]

    def wait(sessions: dict) -> None:
        deadline = time.time() + WORK_WAIT
        while time.time() < deadline:
            for session in sessions.values():
                if session.get("work", {}).get("state") not in ("done", "failed"):
                    session["work"] = session_status(functions, config, session["session_id"], agent)
            if all(session["work"].get("state") in ("done", "failed") for session in sessions.values()):
                break
            time.sleep(8)
        for session in sessions.values():
            item = table.get_item(TableName=config["SessionTableName"], Key={"session": {"S": f"{agent}/{session['session_id']}"}},
                                  ConsistentRead=True).get("Item") or {}
            session["item"] = {k: list(v.values())[0] for k, v in item.items()}
            session["steps"] = [f"{step['step']}: {step['status']}" for step in session["work"].get("steps", [])]

    def state_of(session_id: str) -> dict:
        """The stack and the SSM parameters of a session. A value of an SSM parameter of the test is not a secret."""
        answer = {"stack_name": f"{config['StackPrefix']}{session_id}"}
        try:
            described = cloudformation.describe_stacks(StackName=answer["stack_name"])["Stacks"][0]
            answer.update(status=described["StackStatus"], tags={t["Key"]: t["Value"] for t in described.get("Tags", [])})
            events = cloudformation.describe_stack_events(StackName=answer["stack_name"])["StackEvents"]
            answer["failure_reasons"] = sorted({e["ResourceStatusReason"][:420] for e in events
                                                if "FAILED" in e["ResourceStatus"] and e.get("ResourceStatusReason")})
        except ClientError as error:
            answer["status"] = "no stack" if "does not exist" in error.response["Error"]["Message"] else error.response["Error"]["Code"]
        answer["parameters"] = {p["Name"]: {"value": p["Value"], "version": p["Version"]}
                                for p in ssm.get_parameters_by_path(Path=f"/agentfarm/{session_id}/")["Parameters"]}
        return answer

    # ---- two sessions of the same agent, at the same time, in the same repository
    sessions = {"first": {"greeting": "Hello from the first session"}, "second": {"greeting": "Hello from the second session"}}
    for session in sessions.values():
        session["session_id"] = start(session["greeting"])
    print("sessions: " + ", ".join(f"{label} {session['session_id']}" for label, session in sessions.items()))
    wait(sessions)
    spans = [(int(s["item"].get("started_at", 0)), int(s["item"].get("ended_at", 0))) for s in sessions.values()]
    together = max(span[0] for span in spans) < min(span[1] for span in spans)
    check("P1", "Two sessions of the same agent at the same time",
          all(s["work"].get("state") == "done" and s["item"].get("agent_id") == agent and s["item"].get("state") == "ended"
              for s in sessions.values()) and sessions["first"]["session_id"] != sessions["second"]["session_id"] and together,
          {**{label: {"session_id": s["session_id"], "work": s["work"].get("state"), "steps": s["steps"],
                      "started_at": s["item"].get("started_at"), "ended_at": s["item"].get("ended_at")} for label, s in sessions.items()},
           "the two sessions ran at the same time": together})

    # ---- P2: each session has its own things
    repo, passed, detail = hosted_repository(config, name, REGION), True, {}
    for label, session in sessions.items():
        other = [s for key, s in sessions.items() if key != label][0]
        sid, branch = session["session_id"], f"agent/{session['session_id']}"
        try:
            head = repo.head(branch)
            commit, pull, template = repo.commit(head), repo.pull_request(branch), repo.file(head, "template.yaml")
            session["state"] = state_of(sid)
            values = session["state"]["parameters"]
            ok = (commit["message"].rstrip().endswith(f"Agent-Id: {agent}\nSession-Id: {sid}") and other["session_id"] not in commit["message"]
                  and session["greeting"] in template and other["greeting"] not in template
                  and pull.get("source_commit") == head and (pull.get("description") or "").rstrip().endswith(f"Session-Id: {sid}")
                  and session["state"].get("status") == "CREATE_COMPLETE" and session["state"].get("tags", {}).get("session_id") == sid
                  and session["state"].get("tags", {}).get("commit_id") == head
                  and {k: v["value"] for k, v in values.items()} == {f"/agentfarm/{sid}/greeting": session["greeting"]})
            detail[label] = {"branch": branch, "head": head, "pull_request": pull.get("id"), "stack": session["state"].get("status"),
                             "tag session_id": session["state"].get("tags", {}).get("session_id"),
                             "parameters": {k: v["value"] for k, v in values.items()}}
        except (LookupError, ClientError, KeyError, TypeError) as error:
            ok, detail[label] = False, {"error": str(error)[:200]}
        passed = passed and ok
    check("P2", "Each session has its own branch, commit, pull request, stack and parameter", passed, detail)

    # ---- P3 and P4: two more sessions of the same agent ask for a parameter in the path of the first session
    first = sessions["first"]["session_id"]
    existing, planted = f"/agentfarm/{first}/greeting", f"/agentfarm/{first}/planted"
    before = state_of(first)["parameters"]
    intruders = {"third": {"greeting": "Written by a different session", "target": planted},
                 "fourth": {"greeting": "Written by a different session", "target": existing}}
    for label, session in intruders.items():
        session["session_id"] = start(session["greeting"], parameter_name=session["target"])
        print(f"{label} session: {session['session_id']}, its template names {session['target']}")
    wait(intruders)
    after, outcome = state_of(first)["parameters"], {}
    for label, session in intruders.items():
        session["state"] = state_of(session["session_id"])
        head = repo.head(f"agent/{session['session_id']}")
        answers = " ".join(str(step.get("output", "")) for step in session["work"].get("steps", []))
        outcome[label] = {
            "session_id": session["session_id"], "steps": session["steps"],
            "its template names the parameter": bool(head) and f'Name: "{session["target"]}"' in repo.file(head, "template.yaml"),
            "stack": session["state"].get("status"), "failure_reasons": session["state"].get("failure_reasons"),
            "parameters of this session": session["state"]["parameters"],
            "reasons": " ".join(session["state"].get("failure_reasons", [])) + " " + answers}
    unchanged = after == before and list(after) == [existing] and after[existing]["value"] == sessions["first"]["greeting"]
    third = outcome["third"]
    # A name that does not exist: AWS CloudFormation sends the request to Systems Manager with the credentials of
    # the third session, and IAM must deny it.
    check("P3", "A session cannot make a parameter in the path of a different session",
          third["its template names the parameter"] and third["stack"] not in ("CREATE_COMPLETE", "UPDATE_COMPLETE")
          and bool(re.search(r"access ?denied|not authorized", third["reasons"], re.IGNORECASE)) and "PutParameter" in third["reasons"]
          and not third["parameters of this session"] and unchanged,
          {**{k: v for k, v in third.items() if k != "reasons"}, "parameters in the path of the first session after": sorted(after)})
    fourth = outcome["fourth"]
    # The name of the parameter of the first session: the request must fail, and the parameter must stay as it was.
    check("P4", "A session cannot take the parameter of a different session",
          fourth["its template names the parameter"] and fourth["stack"] not in ("CREATE_COMPLETE", "UPDATE_COMPLETE")
          and not fourth["parameters of this session"] and unchanged,
          {**{k: v for k, v in fourth.items() if k != "reasons"}, "parameter of the first session before": before.get(existing),
           "after": after.get(existing)})

    # ---- P5: IAM, by the policy simulator
    iam, second = api.client("iam"), sessions["second"]["session_id"]

    def may_tag(session_tag: str, runtime_session: str) -> str:
        entry = lambda key, value, kind="string": {"ContextKeyName": key, "ContextKeyValues": value if isinstance(value, list) else [value], "ContextKeyType": kind}  # noqa: E731
        return iam.simulate_principal_policy(
            PolicySourceArn=f"arn:aws:iam::{account}:role/{RUNTIME_TOKEN_ROLE_PREFIX}{agent}", ActionNames=["sts:TagGetWebIdentityToken"],
            ContextEntries=[entry("aws:RequestTag/agent_id", agent), entry("aws:RequestTag/session_id", session_tag),
                            entry("aws:RequestTag/repository", name), entry("aws:AgentCoreRuntimeSourceSid", runtime_session),
                            entry("aws:AgentCoreRuntimeSourceArn", json.loads(config["AgentRuntimeArns"])[agent]),
                            entry("aws:TagKeys", ["agent_id", "session_id", "repository"], "stringList")])["EvaluationResults"][0]["EvalDecision"]

    simulated = {"the second session asks for a JWT with its own ID": may_tag(second, second),
                 "the second session asks for a JWT with the ID of the first session": may_tag(first, second)}
    check("P5", "IAM: a session requests a JWT only with its own session ID",
          list(simulated.values())[0] == "allowed" and list(simulated.values())[1] != "allowed", simulated)

    # ---- P6: AWS CloudTrail
    everyone = {**sessions, **intruders}
    if "--no-cloudtrail" not in sys.argv:
        result["sessions"] = {label: {k: v for k, v in s.items() if k != "work"} for label, s in everyone.items()}
        save()
        trail, deadline, found = api.client("cloudtrail"), time.time() + TRAIL_WAIT, {}
        print("  wait for the events in AWS CloudTrail (some minutes)")
        while time.time() < deadline:
            for label, session in everyone.items():
                found[label] = [json.loads(e["CloudTrailEvent"]) for p in trail.get_paginator("lookup_events").paginate(
                    LookupAttributes=[{"AttributeKey": "Username", "AttributeValue": session["session_id"]}],
                    StartTime=started - datetime.timedelta(minutes=1)) for e in p["Events"]]
            done = all("CreateStack" in {e["eventName"] for e in events} for events in found.values()) \
                and any(e.get("errorCode") for e in found["third"]) and all("PutParameter" in {e["eventName"] for e in found[key]} for key in sessions)
            if done:
                break
            time.sleep(30)
        ids = {label: session["session_id"] for label, session in everyone.items()}
        passed, detail = True, {}
        for label in sessions:
            others = [sid for key, sid in ids.items() if key != label]
            named = json.dumps([e.get("requestParameters") for e in found[label] if e["eventName"] in ("CreateStack", "PutParameter", "GetParameters", "DescribeStacks")])
            ok = bool(found[label]) and ids[label] in named and not any(sid in named for sid in others) and not any(e.get("errorCode") for e in found[label])
            passed = passed and ok
            detail[label] = {"events": len(found[label]), "names its own session ID": ids[label] in named,
                             "names the ID of a different session": any(sid in named for sid in others)}
        denied = [{"event": e["eventName"], "source": e.get("eventSource"), "error": e.get("errorCode"),
                   "sent_by": e["userIdentity"].get("invokedBy"), "role_session": e["userIdentity"].get("arn", "").rsplit("/", 1)[-1],
                   "names the path of the first session": f"/agentfarm/{first}/" in json.dumps([e.get("requestParameters"), e.get("errorMessage")])}
                  for e in found["third"] if e.get("errorCode")]
        ok = bool(denied) and all(d["role_session"] == ids["third"] and d["error"] == "AccessDenied" for d in denied) \
            and any(d["source"] == "ssm.amazonaws.com" and d["names the path of the first session"] for d in denied)
        detail["third"] = {"events": len(found["third"]), "denied requests": denied}
        detail["fourth"] = {"events": len(found["fourth"]), "requests to Systems Manager": sorted(
            {f"{e['eventName']}: {e.get('errorCode') or 'no error'}" for e in found["fourth"] if e.get("eventSource") == "ssm.amazonaws.com"})}
        check("P6", "AWS CloudTrail: each session names its own resources, and the denied request has the third session", passed and ok, detail)

    result["sessions"] = {label: {k: v for k, v in s.items() if k != "work"} for label, s in everyone.items()}
    save()
    print("\n" + ", ".join(f"{key}={value['result']}" for key, value in checks.items()))
    print(f"Wrote {record}")
    return 0 if all(value["result"] == "PASS" for value in checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
