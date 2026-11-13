"""The trail of the stack AgentFarmRuntime: does it keep the events of a session?

The event history of AWS CloudTrail keeps 90 days and has management events only. The trail of the stack
writes log files to an S3 bucket. The script reads the log files of the trail for one session of a test run
(the newest record of `make runtime-chain`, or the record of `--record`: a record of `make runtime-chain`,
`make runtime-agent` or `make runtime-hostile`) and checks, outside the system under test:

  T1  The trail: it is logging, for all Regions, with the events of global services and with log file
      validation, and its data events are the session table, the three functions that decide an identity,
      and the agent runtimes, the workspace and the gateway of AgentCore.
  T2  Management events of the session in the log files: the actions of the session with the agent ID as
      the source identity, and the requests of the broker for credentials with the session tags.
  T3  Data events of the session table for the item of the session: the write of the session starter, the
      reads of the broker, and the end of the session.
  T4  Data events of the functions: the invocation of the session starter and the invocations of the broker
      in the time of the session.
  T5  Data events of AgentCore: the requests of the session starter to the agent runtime with the session ID.
      For a session of the LLM agent, also the requests to the workspace of the microVM of the session: the
      role session of the execution role that requested a JWT with the session ID.

The script also says whether the bucket has a digest file. AWS CloudTrail delivers a digest file every hour,
so a new trail has none: `aws cloudtrail validate-logs` needs one.

Usage: python runtime/tests/run_trail.py [--record <file of a test run>]
       (writes evidence/runtime/trail-<start time>.json)
"""
import datetime
import gzip
import json
import os
import sys
import time
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from infra.names import RUNTIME_STACK  # noqa: E402

REGION = os.environ.get("AWS_REGION", "eu-central-1")
EVIDENCE = ROOT / "evidence" / "runtime"
WAIT = 20 * 60   # seconds: AWS CloudTrail delivers a log file some minutes after an event


def principal(event: dict) -> str:
    """Who sent a request: the name of the role, or a word for a person. The record names no person."""
    identity = event.get("userIdentity", {})
    if identity.get("type") == "AssumedRole":
        return identity.get("sessionContext", {}).get("sessionIssuer", {}).get("userName", "a role")
    if identity.get("type") == "IAMUser":
        return "the person who ran the test"
    return identity.get("invokedBy") or identity.get("type", "?")


def main() -> int:
    session_api = boto3.session.Session(region_name=REGION)
    stack = session_api.client("cloudformation").describe_stacks(StackName=RUNTIME_STACK)["Stacks"][0]
    config = {output["OutputKey"]: output["OutputValue"] for output in stack["Outputs"]}
    source = Path(sys.argv[sys.argv.index("--record") + 1]) if "--record" in sys.argv else sorted(EVIDENCE.glob("chain-*.json"))[-1]
    chain = json.loads(source.read_text())
    # A record of a chain run has the session in its first check; a record of an agent run has it at the top.
    session_id = (chain.get("session") or {}).get("session_id") or chain["checks"]["C1"]["detail"]["session_id"]
    agent_id = config["AgentId"]
    uses_workspace = "workspace_run" in source.read_text()   # a session of the LLM agent
    session_start = datetime.datetime.strptime(chain["started"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    started = datetime.datetime.now(datetime.timezone.utc)
    record = EVIDENCE / f"trail-{started:%Y%m%dT%H%M%SZ}.json"
    result = {"started": started.strftime("%Y-%m-%dT%H:%M:%SZ"), "session_id": session_id, "session_record": source.name, "checks": {}}
    checks = result["checks"]

    def check(key: str, title: str, passed: bool, detail) -> None:
        checks[key] = {"name": title, "result": "PASS" if passed else "FAIL", "detail": detail}
        print(f"{key} {title}: {'PASS' if passed else 'FAIL'}\n    {json.dumps(detail, default=str)[:1000]}")

    # ---- T1: the trail
    cloudtrail = session_api.client("cloudtrail")
    trail = cloudtrail.get_trail(Name=config["TrailName"])["Trail"]
    status = cloudtrail.get_trail_status(Name=config["TrailName"])
    selectors = cloudtrail.get_event_selectors(TrailName=config["TrailName"]).get("AdvancedEventSelectors", [])

    def values(selector: dict, name: str) -> list[str]:
        return [value for field in selector["FieldSelectors"] if field["Field"] == name
                for value in field.get("Equals", []) + field.get("StartsWith", [])]

    management = any(values(selector, "eventCategory") == ["Management"] for selector in selectors)
    data = sorted(value.rsplit(":", 1)[-1].rsplit("/", 1)[-1] for selector in selectors for value in values(selector, "resources.ARN"))
    types = sorted({value for selector in selectors for value in values(selector, "resources.type")})
    logging_since = status.get("StartLoggingTime")
    wanted = sorted([config["SessionTableName"], config["StarterFunctionName"], config["BrokerFunctionName"],
                     config["BrokerFunctionName"].replace("-broker", "-interceptor")])
    wanted_types = sorted(["AWS::DynamoDB::Table", "AWS::Lambda::Function"] + [f"AWS::BedrockAgentCore::{name}" for name in (
        "Runtime", "RuntimeEndpoint", "CodeInterpreterCustom", "Gateway")])
    check("T1", "The trail",
          status.get("IsLogging") is True and trail.get("IsMultiRegionTrail") is True and trail.get("LogFileValidationEnabled") is True
          and trail.get("IncludeGlobalServiceEvents") is True and management and data == wanted and types == wanted_types
          and logging_since < session_start,
          {"logging": status.get("IsLogging"), "logging_since": logging_since, "all_regions": trail.get("IsMultiRegionTrail"),
           "log_file_validation": trail.get("LogFileValidationEnabled"), "management_events": management,
           "data_events_of": data, "data_event_types": types,
           "the session started after the trail": logging_since < session_start})

    # ---- the log files of the trail since the start of the session
    s3, bucket = session_api.client("s3"), config["TrailBucketName"]
    account = stack["StackId"].split(":")[4]
    prefix = f"AWSLogs/{account}/CloudTrail/{REGION}/{session_start:%Y/%m/%d}/"
    session_key = f"{agent_id}/{session_id}"
    deadline, events, files = time.time() + WAIT, [], 0
    needed = {"CreateStack", "AssumeRole", "PutItem", "GetItem", "UpdateItem", "Invoke", "InvokeAgentRuntime"} | (
        {"InvokeCodeInterpreter"} if uses_workspace else set())
    print(f"Read the log files of s3://{bucket}/{prefix} for the session {session_id}")
    while True:
        events, files = [], 0
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                if item["LastModified"] < session_start:
                    continue
                files += 1
                body = gzip.decompress(s3.get_object(Bucket=bucket, Key=item["Key"])["Body"].read())
                events += [e for e in json.loads(body)["Records"] if e["eventTime"] >= chain["started"]]
        names = {e["eventName"] for e in events
                 if session_id in json.dumps([e.get("userIdentity"), e.get("requestParameters"), e.get("responseElements")])
                 or e["eventName"] in ("Invoke", "InvokeCodeInterpreter")}
        print(f"  {files} log files, {len(events)} events since the start of the session")
        if needed <= names or time.time() >= deadline:
            break
        time.sleep(45)

    # ---- T2: management events of the session
    own = [e for e in events if e.get("userIdentity", {}).get("arn", "").endswith(f"/{session_id}")]
    assumes = [e for e in events if e["eventName"] == "AssumeRole" and (e.get("requestParameters") or {}).get("roleSessionName") == session_id]
    tags = sorted({json.dumps(sorted((t["key"], t["value"]) for t in (e["requestParameters"].get("tags") or []))) for e in assumes})
    check("T2", "Management events of the session in the log files",
          "CreateStack" in {e["eventName"] for e in own} and bool({"Sign", "GetSecretValue"} & {e["eventName"] for e in own})
          and all(e["userIdentity"].get("sessionContext", {}).get("sourceIdentity") == agent_id for e in own)
          and bool(assumes) and all("task_sha256" in tag for tag in tags),
          {"events of the session": len(own), "event names": sorted({e["eventName"] for e in own}),
           "source identity of every event": sorted({str(e["userIdentity"].get("sessionContext", {}).get("sourceIdentity")) for e in own}),
           "requests of the broker for credentials": len(assumes), "session tags": tags})

    # ---- T3: data events of the session table for the item of the session
    table_events = [e for e in events if e.get("eventSource") == "dynamodb.amazonaws.com"
                    and session_key in json.dumps(e.get("requestParameters") or {})]
    by_name = sorted({f"{e['eventName']} by {principal(e)}" for e in table_events})
    result["table_event_sample"] = (table_events[0].get("requestParameters") if table_events else None)
    check("T3", "Data events of the session table for the item of the session",
          any(e["eventName"] == "PutItem" and principal(e) == config["StarterFunctionName"] for e in table_events)
          and any(e["eventName"] == "GetItem" and principal(e) == config["BrokerFunctionName"] for e in table_events)
          and any(e["eventName"] == "UpdateItem" and principal(e) == config["BrokerFunctionName"] for e in table_events),
          {"events": len(table_events), "who did what": by_name})

    # ---- T4: data events of the functions
    invokes = [e for e in events if e["eventName"] == "Invoke" and e.get("eventSource") == "lambda.amazonaws.com"]
    functions = {}
    for e in invokes:
        name = str((e.get("requestParameters") or {}).get("functionName", "")).rsplit(":function:", 1)[-1].split(":")[0]
        functions.setdefault(name, set()).add(principal(e))
    functions = {name: sorted(who) for name, who in sorted(functions.items())}
    check("T4", "Data events of the functions",
          bool(functions.get(config["StarterFunctionName"])) and bool(functions.get(config["BrokerFunctionName"])),
          {"invocations": len(invokes), "who invoked each function": functions})

    # ---- T5: data events of AgentCore
    agentcore = [e for e in events if e.get("eventSource") == "bedrock-agentcore.amazonaws.com" and e.get("eventCategory") == "Data"]
    starts = [e for e in agentcore if e["eventName"] == "InvokeAgentRuntime"
              and (e.get("responseElements") or {}).get("runtimeSessionId") == session_id]
    # The microVM of the session: the role session of the execution role that requested a JWT with the session ID.
    microvms = sorted({e["userIdentity"].get("arn", "") for e in events if e["eventName"] == "GetWebIdentityToken" and any(
        tag.get("key") == "session_id" and tag.get("value") == session_id for tag in (e.get("requestParameters") or {}).get("tags") or [])})
    workspace = [e for e in agentcore if "CodeInterpreter" in e["eventName"] and e["userIdentity"].get("arn") in microvms]
    workspace_names = {}
    for e in workspace:
        workspace_names[e["eventName"]] = workspace_names.get(e["eventName"], 0) + 1
    result["agentcore_event_samples"] = {
        "InvokeAgentRuntime": {k: starts[0].get(k) for k in ("eventTime", "resources", "requestParameters", "responseElements")} if starts else None,
        "workspace": {k: workspace[0].get(k) for k in ("eventName", "eventTime", "requestParameters", "responseElements")} if workspace else None}
    check("T5", "Data events of AgentCore",
          bool(starts) and all(principal(e) == config["StarterFunctionName"] for e in starts)
          and (not uses_workspace or (len(microvms) == 1 and workspace_names.get("InvokeCodeInterpreter", 0) > 0)),
          {"requests to the agent runtime with the session ID": len(starts),
           "who sent them": sorted({principal(e) for e in starts}),
           "session of the LLM agent": uses_workspace, "microVM of the session": [arn.rsplit("/", 1)[-1] for arn in microvms],
           "requests of that microVM to the workspace": workspace_names,
           "requests to the gateway in the log files": sum(1 for e in agentcore if e["eventName"] == "InvokeGateway")})

    digests = s3.list_objects_v2(Bucket=bucket, Prefix=f"AWSLogs/{account}/CloudTrail-Digest/", MaxKeys=5).get("KeyCount", 0)
    result["digest_files"] = {"found": digests, "note": "AWS CloudTrail delivers a digest file every hour; `aws cloudtrail validate-logs` needs one"}
    print(f"digest files in the bucket: {digests}")

    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(result, indent=2, default=str))
    print("\n" + ", ".join(f"{key}={value['result']}" for key, value in checks.items()))
    print(f"Wrote {record}")
    return 0 if all(value["result"] == "PASS" for value in checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
