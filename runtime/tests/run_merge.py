"""What a merge does, against the deployed stack AgentFarmRuntime.

A session of an agent ends with a pull request and with a stack of the session. A person merges the pull
request. The merge must deploy the live environment of the repository and delete the stack of the session.
The script changes nothing: it reads the result of a pull request that a person merged, outside the system
under test.

  M1  The pull request is merged, and the workflow agentfarm-deploy ran for the merge commit and ended well.
  M2  The live stack: complete, with the merge commit as its tag, and with the values of the template of
      the merge commit.
  M3  The stack of the session and its SSM parameters are not there any more.
  M4  AWS CloudTrail: the role of the repository for deployments changed the live stack and deleted the
      stack of the session, in a role session with the name of the merge commit. No session of an agent
      changed the live stack.

Usage: python runtime/tests/run_merge.py --pull <number> [--use-case <use case>] [--no-cloudtrail]
       (writes evidence/runtime/merge-<start time>.json)
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
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from infra.names import RUNTIME_STACK  # noqa: E402

REGION = os.environ.get("AWS_REGION", "eu-central-1")
EVIDENCE = ROOT / "evidence" / "runtime"
RUN_WAIT, TRAIL_WAIT = 6 * 60, 15 * 60   # seconds


def gh(*arguments: str) -> str:
    done = subprocess.run(["gh", *arguments], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise SystemExit(f"gh {' '.join(arguments[:3])}: {done.stderr.strip()[:300]}")
    return done.stdout.strip()


def argument(name: str, default: str = "") -> str:
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def main() -> int:
    if "--pull" not in sys.argv:
        raise SystemExit(__doc__)
    number, use_case = int(argument("--pull")), argument("--use-case", "word-count")
    cloudformation = boto3.client("cloudformation", region_name=REGION)
    stack = cloudformation.describe_stacks(StackName=RUNTIME_STACK)["Stacks"][0]
    config = {output["OutputKey"]: output["OutputValue"] for output in stack["Outputs"]}
    name = json.loads(config["Repositories"])[use_case]
    deploy_role = json.loads(config["GitHubDeployRoles"])[use_case].rsplit("/", 1)[-1]
    environment = f"{config['LiveEnvironmentPrefix']}{use_case}"
    owner = os.environ.get("GITHUB_OWNER") or gh("api", "user", "--jq", ".login")
    started = datetime.datetime.now(datetime.timezone.utc)
    record = EVIDENCE / f"merge-{started:%Y%m%dT%H%M%SZ}.json"
    result = {"started": started.strftime("%Y-%m-%dT%H:%M:%SZ"), "config": config, "checks": {}}
    checks = result["checks"]

    def check(key: str, title: str, passed: bool, detail) -> None:
        checks[key] = {"name": title, "result": "PASS" if passed else "FAIL", "detail": detail}
        print(f"{key} {title}: {'PASS' if passed else 'FAIL'}\n    {json.dumps(detail, default=str)[:1000]}")

    def save() -> None:
        record.parent.mkdir(parents=True, exist_ok=True)
        # The GitHub user of the person who runs the test stays out of the record.
        record.write_text(json.dumps(result, indent=2, default=str).replace(owner, "REDACTED"))

    # ---- M1: the merge and the workflow
    pull = json.loads(gh("api", f"repos/{owner}/{name}/pulls/{number}"))
    branch, commit = pull["head"]["ref"], pull.get("merge_commit_sha") or ""
    session = re.fullmatch(r"agent/([0-9a-f-]{36})", branch)
    if not pull.get("merged") or not session:
        raise SystemExit(f"The pull request #{number} of {name} is not a merged pull request of a session.")
    session_id, deadline, run = session.group(1), time.time() + RUN_WAIT, {}
    while time.time() < deadline:
        runs = json.loads(gh("api", f"repos/{owner}/{name}/actions/runs?event=push&head_sha={commit}"))["workflow_runs"]
        run = next(({"workflow": r["name"], "status": r["status"], "conclusion": r["conclusion"], "branch": r["head_branch"],
                     "attempt": r["run_attempt"]} for r in runs if r["name"] == "agentfarm-deploy"), {})
        if run.get("status") == "completed":
            break
        time.sleep(10)
    result["pull_request"] = {"number": number, "branch": branch, "merge_commit": commit, "merged_at": pull.get("merged_at"),
                              "merged_by_the_agent": (pull.get("merged_by") or {}).get("login", "").endswith("[bot]")}
    result["workflow_run"] = run
    check("M1", "Merge and workflow", run.get("conclusion") == "success" and run.get("branch") == "main"
          and not result["pull_request"]["merged_by_the_agent"], {**result["pull_request"], "workflow_run": run})

    # ---- M2: the live stack
    live = {"stack_name": f"{config['StackPrefix']}{environment}"}
    template = gh("api", f"repos/{owner}/{name}/contents/template.yaml?ref={commit}", "-H", "Accept: application/vnd.github.raw+json")
    try:
        described = cloudformation.describe_stacks(StackName=live["stack_name"])["Stacks"][0]
        live.update(status=described["StackStatus"], tags={t["Key"]: t["Value"] for t in described.get("Tags", [])})
        parameters = boto3.client("ssm", region_name=REGION).get_parameters_by_path(Path=f"/agentfarm/{environment}/")["Parameters"]
        live["parameters"] = {p["Name"]: p["Value"] for p in parameters}
    except ClientError as error:
        live["error"] = error.response["Error"]["Code"]
    result["live_stack"] = live
    values = live.get("parameters", {})
    check("M2", "Live stack", live.get("status") in ("CREATE_COMPLETE", "UPDATE_COMPLETE")
          and live.get("tags", {}).get("commit_id") == commit and live.get("tags", {}).get("repository") == name
          and bool(values) and all(json.dumps(value) in template or value in template for value in values.values()), live)

    # ---- M3: the stack of the session
    gone = {"stack_name": f"{config['StackPrefix']}{session_id}"}
    try:
        gone["status"] = cloudformation.describe_stacks(StackName=gone["stack_name"])["Stacks"][0]["StackStatus"]
    except ClientError as error:
        gone["status"] = "not there" if "does not exist" in error.response["Error"]["Message"] else error.response["Error"]["Code"]
    gone["parameters"] = [p["Name"] for p in boto3.client("ssm", region_name=REGION).get_parameters_by_path(
        Path=f"/agentfarm/{session_id}/")["Parameters"]]
    result["session_stack"] = gone
    check("M3", "Stack of the session", gone["status"] == "not there" and not gone["parameters"], gone)

    # ---- M4: AWS CloudTrail
    if "--no-cloudtrail" in sys.argv:
        save()
        print(f"Wrote {record} (without AWS CloudTrail)")
        return 0 if all(value["result"] == "PASS" for value in checks.values()) else 1
    trail, deadline, events = boto3.client("cloudtrail", region_name=REGION), time.time() + TRAIL_WAIT, []
    merged_at = datetime.datetime.fromisoformat(pull["merged_at"].replace("Z", "+00:00"))
    wanted = {"ExecuteChangeSet", "DeleteStack"}
    print("  wait for the events in AWS CloudTrail (some minutes)")
    while time.time() < deadline:
        pages = trail.get_paginator("lookup_events").paginate(
            LookupAttributes=[{"AttributeKey": "Username", "AttributeValue": f"merge-{commit}"}],
            StartTime=merged_at - datetime.timedelta(minutes=1))
        events = [json.loads(event["CloudTrailEvent"]) for page in pages for event in page["Events"]]
        if wanted <= {event["eventName"] for event in events}:
            break
        time.sleep(30)
    summary = [{"event": e["eventName"], "role": e["userIdentity"].get("sessionContext", {}).get("sessionIssuer", {}).get("userName"),
                "role_session": e["userIdentity"].get("arn", "").rsplit("/", 1)[-1],
                "stack": (e.get("requestParameters") or {}).get("stackName"), "error": e.get("errorCode")}
               for e in events if e["eventName"] in wanted | {"CreateChangeSet"}]
    # Who changed the live stack since it exists: only the role for deployments, never the agent role.
    pages = trail.get_paginator("lookup_events").paginate(
        LookupAttributes=[{"AttributeKey": "EventName", "AttributeValue": "ExecuteChangeSet"}],
        StartTime=started - datetime.timedelta(hours=12))
    writers = sorted({json.loads(e["CloudTrailEvent"])["userIdentity"].get("sessionContext", {}).get("sessionIssuer", {}).get("userName", "?")
                      for page in pages for e in page["Events"]
                      if (json.loads(e["CloudTrailEvent"]).get("requestParameters") or {}).get("stackName") == live["stack_name"]})
    result["cloudtrail"] = {"events_of_the_merge": summary, "roles_that_changed_the_live_stack": writers}
    check("M4", "AWS CloudTrail",
          any(e["event"] == "ExecuteChangeSet" and e["stack"] == live["stack_name"] and not e["error"] for e in summary)
          and any(e["event"] == "DeleteStack" and e["stack"] == gone["stack_name"] and not e["error"] for e in summary)
          and all(e["role"] == deploy_role and e["role_session"] == f"merge-{commit}" for e in summary)
          and writers == [deploy_role], result["cloudtrail"])

    save()
    print("\n" + ", ".join(f"{key}={value['result']}" for key, value in checks.items()))
    print(f"Wrote {record}")
    return 0 if all(value["result"] == "PASS" for value in checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
