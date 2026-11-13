"""A template of a session that asks AWS CloudFormation for a value of the GitHub App of its agent, against the
deployed stack AgentFarmRuntime.

The template of a session is input of the agent. AWS CloudFormation resolves a dynamic reference of a
template with the credentials of the caller of the stack operation. The broker reads the values of the GitHub
App of the agent with credentials of the session, so a session must never give AWS CloudFormation credentials
that can read them.

The value that the template names: for an agent with a KMS key, the SSM parameter with the ID of the App (the
agent has no secret, and AWS CloudFormation has no reference that asks AWS KMS for a signature); for an agent
without a KMS key, the field `app_id` of the secret of the App. Both are identifiers, not the private key:
the test never names the key.

The script starts one scripted session (no model). The session commits a template whose SSM parameter has
the dynamic reference as its value, and it deploys the stack. The value of a scripted session has 80
characters at most. The script checks the result outside the system under test, and it never reads or prints
a value:

  K1  The session committed the template with the dynamic reference and asked for the deployment.
  K2  The stack is not complete, and the reason is that the session is not authorized to read the value.
  K3  The session has no SSM parameter.
  K4  AWS CloudTrail: the read of the value that AWS CloudFormation sent for the session is denied, and the
      reads of the broker for the same session are allowed.

If the stack is complete or a parameter exists, the script deletes the stack and says so.

Usage: python runtime/tests/run_secret_reference.py [--no-cloudtrail]
       (writes evidence/runtime/secret-reference-<start time>.json)
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
from infra.names import GITHUB_APP_SECRET_PREFIX, GITHUB_APP_SETTINGS_PREFIX, RUNTIME_STACK  # noqa: E402
from runtime.tests.repository import repository as hosted_repository  # noqa: E402
from runtime.tests.session import session_status  # noqa: E402

REGION = os.environ.get("AWS_REGION", "eu-central-1")
EVIDENCE = ROOT / "evidence" / "runtime"
USE_CASE = "greeting-service"
WORK_WAIT, TRAIL_WAIT = 8 * 60, 15 * 60   # seconds


def main() -> int:
    cloudformation = boto3.client("cloudformation", region_name=REGION)
    stack = cloudformation.describe_stacks(StackName=RUNTIME_STACK)["Stacks"][0]
    config = {output["OutputKey"]: output["OutputValue"] for output in stack["Outputs"]}
    agent_id, name = config["AgentId"], json.loads(config["Repositories"])[USE_CASE]
    kms_agents = json.loads(config["GitHubAppKeys"]) if config.get("GitHubAppKeys", "none") != "none" else {}
    if agent_id in kms_agents:   # the ID of the App is an SSM parameter
        target = f"{GITHUB_APP_SETTINGS_PREFIX}{agent_id}/app_id"
        reference, action, event_name = f"{{{{resolve:ssm:{target}}}}}", "ssm:GetParameters", "GetParameters"
    else:                        # the ID of the App is a field of the secret
        target = f"{GITHUB_APP_SECRET_PREFIX}{agent_id}"
        reference = f"{{{{resolve:secretsmanager:{target}:SecretString:app_id}}}}"
        action, event_name = "secretsmanager:GetSecretValue", "GetSecretValue"
    if len(reference) > 80:
        raise SystemExit(f"The dynamic reference has {len(reference)} characters: a scripted session cuts its value at 80.")
    started = datetime.datetime.now(datetime.timezone.utc)
    record = EVIDENCE / f"secret-reference-{started:%Y%m%dT%H%M%SZ}.json"
    result = {"started": started.strftime("%Y-%m-%dT%H:%M:%SZ"), "config": config, "dynamic_reference": reference,
              "value_of_the_app": target, "checks": {}}
    checks = result["checks"]

    def check(key: str, title: str, passed: bool, detail) -> None:
        checks[key] = {"name": title, "result": "PASS" if passed else "FAIL", "detail": detail}
        print(f"{key} {title}: {'PASS' if passed else 'FAIL'}\n    {json.dumps(detail, default=str)[:1000]}")

    functions = boto3.client("lambda", region_name=REGION, config=Config(read_timeout=120, retries={"total_max_attempts": 1}))
    answer = json.loads(functions.invoke(
        FunctionName=config["StarterFunctionName"],
        Payload=json.dumps({"agent_id": agent_id, "use_case": USE_CASE, "requested_by": "test run (run_secret_reference.py)",
                            "mode": "scripted", "greeting": reference}).encode())["Payload"].read())
    session_id = answer.get("session_id", "")
    if not session_id:
        raise SystemExit(f"The session did not start: {json.dumps(answer)[:300]}")
    print(f"Session {session_id}: wait for its work")
    deadline, status = time.time() + WORK_WAIT, {}
    while time.time() < deadline:
        status = session_status(functions, config, session_id)
        if status.get("state") in ("done", "failed"):
            break
        time.sleep(10)
    steps = [f"{step['step']}: {step['status']}" for step in status.get("steps", [])]
    result["session_id"], result["work"] = session_id, {"state": status.get("state"), "steps": steps, "error": status.get("error")}

    # ---- K1: the template of the session has the dynamic reference
    repo, branch = hosted_repository(config, name, REGION), f"agent/{session_id}"
    head = repo.head(branch)
    template = repo.file(head, "template.yaml") if head else ""
    check("K1", "The session committed the template with the dynamic reference and asked for the deployment",
          reference in template and any(step.startswith("deploy_stack") for step in steps),
          {"head": head, "the template has the dynamic reference": reference in template, "steps": steps})

    # ---- K2: the stack
    stack_name, deployed = f"{config['StackPrefix']}{session_id}", {}
    try:
        deployed["status"] = cloudformation.describe_stacks(StackName=stack_name)["Stacks"][0]["StackStatus"]
        events = cloudformation.describe_stack_events(StackName=stack_name)["StackEvents"]
        # The reason of a failed resource, or of the stack itself: AWS CloudFormation refuses a reference to an
        # SSM parameter before it makes a resource, and the event of the stack has the reason.
        deployed["failure_reasons"] = sorted({e["ResourceStatusReason"][:400] for e in events
                                              if e.get("ResourceStatusReason") and (
                                                  "FAILED" in e["ResourceStatus"] or "not authorized" in e["ResourceStatusReason"])})
    except ClientError as error:
        deployed["status"] = "no stack" if "does not exist" in error.response["Error"]["Message"] else error.response["Error"]["Code"]
    answers = " ".join(str(step.get("output", "")) for step in status.get("steps", []))
    deployed["the broker answered with an authorization error"] = action.split(":")[1] in answers and "not authorized" in answers
    result["stack"] = deployed
    reasons = " ".join(deployed.get("failure_reasons", [])) + " " + answers
    check("K2", "The stack is not complete: the session is not authorized to read the value of the App",
          deployed["status"] not in ("CREATE_COMPLETE", "UPDATE_COMPLETE")
          and action in reasons and "not authorized" in reasons, deployed)

    # ---- K3: no SSM parameter of the session. Names only: the script never reads a value.
    described = boto3.client("ssm", region_name=REGION).describe_parameters(
        ParameterFilters=[{"Key": "Name", "Option": "BeginsWith", "Values": [f"/agentfarm/{session_id}/"]}])["Parameters"]
    parameters = [p["Name"] for p in described]
    result["parameters"] = parameters
    check("K3", "The session has no SSM parameter", not parameters, {"parameter names": parameters})

    if deployed["status"] in ("CREATE_COMPLETE", "UPDATE_COMPLETE") or parameters:
        cloudformation.delete_stack(StackName=stack_name)
        result["cleanup"] = f"the stack {stack_name} had a value of the App: the script deleted the stack"
        print(result["cleanup"])

    # ---- K4: AWS CloudTrail
    if "--no-cloudtrail" not in sys.argv:
        trail, deadline, reads = boto3.client("cloudtrail", region_name=REGION), time.time() + TRAIL_WAIT, []
        print("  wait for the events in AWS CloudTrail (some minutes)")
        while time.time() < deadline:
            pages = trail.get_paginator("lookup_events").paginate(
                LookupAttributes=[{"AttributeKey": "Username", "AttributeValue": session_id}],
                StartTime=started - datetime.timedelta(minutes=1))
            events = [json.loads(event["CloudTrailEvent"]) for page in pages for event in page["Events"]]
            reads = [{"sent_by": e["userIdentity"].get("invokedBy") or "the broker (a direct request)", "error": e.get("errorCode"),
                      "role": e["userIdentity"].get("sessionContext", {}).get("sessionIssuer", {}).get("userName"),
                      "source_identity": e["userIdentity"].get("sessionContext", {}).get("sourceIdentity"),
                      "value": (e.get("requestParameters") or {}).get("secretId") or (e.get("requestParameters") or {}).get("names")}
                     for e in events if e["eventName"] == event_name
                     # The event of a denied request has no request parameters: it is the request of AWS
                     # CloudFormation for the value. An allowed request names what it read.
                     and (target in json.dumps(e.get("requestParameters") or {})
                          or (e.get("errorCode") and not e.get("requestParameters")))]
            if any(read["sent_by"] == "cloudformation.amazonaws.com" for read in reads):
                break
            time.sleep(30)
        through = [read for read in reads if read["sent_by"] == "cloudformation.amazonaws.com"]
        direct = [read for read in reads if read["sent_by"] != "cloudformation.amazonaws.com"]
        result["cloudtrail"] = {"reads_through_cloudformation": through, "direct_reads": len(direct),
                                "direct_reads_with_an_error": sum(1 for read in direct if read["error"])}
        check("K4", "AWS CloudTrail: the read through AWS CloudFormation is denied, the reads of the broker are allowed",
              bool(through) and all(read["error"] == "AccessDenied" and read["source_identity"] == agent_id for read in through)
              and bool(direct) and not any(read["error"] for read in direct), result["cloudtrail"])

    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(result, indent=2, default=str))
    print("\n" + ", ".join(f"{key}={value['result']}" for key, value in checks.items()))
    print(f"Wrote {record}")
    return 0 if all(value["result"] == "PASS" for value in checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
