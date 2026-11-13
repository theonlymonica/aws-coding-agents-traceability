"""One session that an issue on GitHub starts, against the deployed stack AgentFarmRuntime.

The script opens an issue with the label `agentfarm` in the repository of a use case, with the GitHub CLI of
the person who runs it. The workflow of the repository must start a session, and the agent must do the work
that the issue asks for. The script checks the result outside the system under test:

  I1  The workflow of the repository ran, and the session starter started one session for the issue. The
      session table has the issue, the GitHub user who asked, and the hash of the task: the hash of the text
      of the issue.
  I2  The work of the agent ends.
  I3  The repository: the change on the branch of the session, the other files and the workflows not changed,
      a commit with a signature of GitHub and the GitHub App of the agent as its author, and the lines of
      the broker (the issue, the agent, the session) at the end of the commit message and of the pull request.
  I4  The stack of the session: complete, with the value that the issue asked for.
  I5  The session ended itself in the session table.
  I6  AWS CloudTrail: the role session of the workflow has the issue and the run in its name, and every
      request of the broker for credentials of the session has the issue and the hash of the task as
      session tags.

With `--issue <number>` the script opens no issue: it runs the last workflow run of the repository again,
which must be the run of that issue, and checks the session of that issue.

Usage: python runtime/tests/run_issue.py [--use-case word-count|feature-flags] [--issue <number>] [--no-cloudtrail]
       (writes evidence/runtime/issue-<start time>.json)
"""
import datetime
import hashlib
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
from infra.names import RUNTIME_STACK  # noqa: E402
from runtime.tests.repository import repository as hosted_repository  # noqa: E402
from runtime.tests.session import session_status  # noqa: E402

REGION = os.environ.get("AWS_REGION", "eu-central-1")
EVIDENCE = ROOT / "evidence" / "runtime"
LABEL = "agentfarm"
WORKFLOWS = (".github/workflows/agentfarm.yml", ".github/workflows/agentfarm-deploy.yml")
START_WAIT, WORK_WAIT, TRAIL_WAIT = 4 * 60, 20 * 60, 15 * 60   # seconds


def flags_of(template: str) -> dict:
    """The feature flags of a template of the use case feature-flags: the JSON value of its SSM parameter."""
    value = re.search(r"Value:\s*'(.*)'", template)
    try:
        return json.loads(value.group(1)) if value else {}
    except json.JSONDecodeError:
        return {}


def json_value(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


# For each use case: the issue, the files that the check reads from the head of the session branch, the check
# of these files, and the SSM parameter of the stack of the session with the check of its value.
CASES = {
    "word-count": {
        "title": "count_words gives a wrong number for a text with more than one space between words",
        "body": """The function `count_words` in `wordcount.py` gives a wrong number in three cases:

- a text with more than one space between two words,
- an empty text,
- a text with tabs or line breaks.

Three of the four tests in `test_wordcount.py` fail.

Please correct `wordcount.py` so that all four tests pass. Do not change the tests. Then set the version in
`template.yaml` to `1.0.1`, open a pull request and deploy the stack.
""",
        "files": ["wordcount.py", "template.yaml", "test_wordcount.py"],
        "code": lambda files: 'split(" ")' not in files["wordcount.py"] and "1.0.1" in files["template.yaml"]
        and files["test_wordcount.py"] == (ROOT / "repositories" / "word-count" / "test_wordcount.py").read_text(),
        "parameter": "word-count-version",
        "value": lambda value: value == "1.0.1",
    },
    "feature-flags": {
        "title": "Turn on the new checkout for all customers",
        "body": """The new checkout is ready for all customers.

Please set the flag `new_checkout` to `true` in `template.yaml`. Keep the value valid JSON and do not change
the flag `dark_mode`. Then open a pull request and deploy the stack.
""",
        "files": ["template.yaml", "README.md"],
        "code": lambda files: flags_of(files["template.yaml"]) == {"dark_mode": False, "new_checkout": True}
        and files["README.md"] == (ROOT / "repositories" / "feature-flags" / "README.md").read_text(),
        "parameter": "flags",
        "value": lambda value: json_value(value) == {"dark_mode": False, "new_checkout": True},
    },
}


def gh(*arguments: str) -> str:
    done = subprocess.run(["gh", *arguments], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise SystemExit(f"gh {' '.join(arguments[:3])}: {done.stderr.strip()[:300]}")
    return done.stdout.strip()


def argument(name: str, default: str = "") -> str:
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def main() -> int:
    use_case = argument("--use-case", "word-count")
    if use_case not in CASES:
        raise SystemExit(f"--use-case must be one of {', '.join(CASES)}")
    case = CASES[use_case]
    stack = boto3.client("cloudformation", region_name=REGION).describe_stacks(StackName=RUNTIME_STACK)["Stacks"][0]
    config = {output["OutputKey"]: output["OutputValue"] for output in stack["Outputs"]}
    agent_id, name = config["AgentId"], json.loads(config["Repositories"])[use_case]
    start_role = json.loads(config["GitHubRoles"])[use_case].rsplit("/", 1)[-1]
    owner = os.environ.get("GITHUB_OWNER") or gh("api", "user", "--jq", ".login")
    started = datetime.datetime.now(datetime.timezone.utc)
    # One file for each run: a later run does not write over the record of an earlier run.
    record = EVIDENCE / f"issue-{started:%Y%m%dT%H%M%SZ}.json"
    result = {"started": started.strftime("%Y-%m-%dT%H:%M:%SZ"), "use_case": use_case, "config": config, "checks": {}}
    checks = result["checks"]

    def check(key: str, title: str, passed: bool, detail) -> None:
        checks[key] = {"name": title, "result": "PASS" if passed else "FAIL", "detail": detail}
        print(f"{key} {title}: {'PASS' if passed else 'FAIL'}\n    {json.dumps(detail, default=str)[:1000]}")

    def save() -> None:
        record.parent.mkdir(parents=True, exist_ok=True)
        # The GitHub user of the person who runs the test stays out of the record.
        record.write_text(json.dumps(result, indent=2, default=str).replace(owner, "REDACTED"))

    if "--issue" in sys.argv:
        number = int(argument("--issue"))
        last = json.loads(gh("api", f"repos/{owner}/{name}/actions/runs?event=issues&per_page=1"))["workflow_runs"][0]
        print(f"Run the workflow run {last['run_number']} of the repository {name} again, for the issue #{number}")
        gh("run", "rerun", str(last["id"]), "--repo", f"{owner}/{name}")
        result["workflow_run_again"] = last["run_number"]
    else:
        print(f"Open an issue with the label {LABEL} in the repository {name}")
        url = gh("issue", "create", "--repo", f"{owner}/{name}", "--title", case["title"], "--body", case["body"], "--label", LABEL)
        number = int(url.rstrip("/").rsplit("/", 1)[-1])
    issue = json.loads(gh("api", f"repos/{owner}/{name}/issues/{number}"))
    result["issue"] = {"number": number, "title": issue["title"], "body": issue["body"]}
    # The task that the session starter makes from an issue, and its hash.
    task = f"Issue #{number} of the repository: {issue['title'].strip()[:300]}\n\n{(issue['body'] or '')[:8000]}"
    task_sha256 = hashlib.sha256(task.encode()).hexdigest()
    print(f"  issue #{number}: wait for the workflow and for the session")

    # ---- I1: the workflow and the session
    table, deadline, item, run = boto3.client("dynamodb", region_name=REGION), time.time() + START_WAIT, None, {}
    while time.time() < deadline and not item:
        time.sleep(10)
        runs = json.loads(gh("api", f"repos/{owner}/{name}/actions/runs?event=issues&per_page=5"))["workflow_runs"]
        run = next(({"id": r["id"], "status": r["status"], "conclusion": r["conclusion"], "workflow": r["name"], "branch": r["head_branch"]}
                    for r in runs if r["updated_at"] >= result["started"]), {})
        found = table.scan(
            TableName=config["SessionTableName"], FilterExpression="issue_number = :n AND repository = :r AND started_at >= :t",
            ExpressionAttributeValues={":n": {"N": str(number)}, ":r": {"S": name}, ":t": {"N": str(int(started.timestamp()))}})["Items"]
        item = found[0] if found else None
    session = {k: list(v.values())[0] for k, v in (item or {}).items()}
    session_id = session.get("session_id", "")
    result["workflow_run"], result["session_at_start"] = run, session
    check("I1", "Workflow and session of the issue",
          bool(session_id) and session.get("issue_number") == str(number) and session.get("agent_id") == agent_id
          and session.get("requested_by") == owner and session.get("issue_url", "").endswith(f"/issues/{number}")
          and run.get("branch") == "main" and session.get("task_sha256") == task_sha256,
          {"workflow_run": run, "session_id": session_id, "issue_number": session.get("issue_number"),
           "requested_by is the GitHub user who set the label": session.get("requested_by") == owner,
           "task_sha256 of the session": session.get("task_sha256"),
           "task_sha256 is the hash of the text of the issue": session.get("task_sha256") == task_sha256})
    if not session_id:
        save()
        return 1

    # ---- I2: the work of the agent
    functions = boto3.client("lambda", region_name=REGION, config=Config(read_timeout=120, retries={"total_max_attempts": 2}))
    deadline, status = time.time() + WORK_WAIT, {}
    while time.time() < deadline:
        status = session_status(functions, config, session_id)
        if status.get("state") in ("done", "failed"):
            break
        time.sleep(15)
    result["work"] = status
    steps = status.get("steps", [])
    check("I2", "Work of the agent", status.get("state") == "done" and status.get("stop_reason") == "end_turn",
          {"state": status.get("state"), "stop_reason": status.get("stop_reason"), "turns": status.get("turns"),
           "steps": [f"{step['step']}: {step['status']}" for step in steps], "report": (status.get("report") or "")[:500]})

    # ---- I3: the repository
    repo, branch = hosted_repository(config, name, REGION), f"agent/{session_id}"
    code = {"host": repo.host}
    try:
        head = repo.head(branch)
        if head:
            files = {path: repo.file(head, path) for path in case["files"]}
            verification = json.loads(gh("api", f"repos/{owner}/{name}/commits/{head}", "--jq", ".commit.verification | {verified, reason}"))
            code.update({"head": head, **repo.commit(head), "signature_of_github": verification,
                         "pull_request": repo.pull_request(branch), "files": files, "change_as_asked": bool(case["code"](files)),
                         "workflows_unchanged": all(repo.file(head, path) == repo.file(repo.head("main"), path) for path in WORKFLOWS)})
        else:
            code["error"] = "the session has no branch"
    except (ClientError, LookupError, KeyError) as error:
        code["error"] = str(error)[:200]
    result["repository"] = code
    lines = f"Issue: #{number}\nAgent-Id: {agent_id}\nSession-Id: {session_id}"
    description = (code.get("pull_request", {}).get("description") or "")
    check("I3", "Repository",
          repo.author_of(agent_id, code.get("author", "")) and code.get("signature_of_github", {}).get("verified") is True
          and code.get("message", "").rstrip().endswith(lines) and description.rstrip().endswith(lines)
          and code.get("change_as_asked") is True and code.get("workflows_unchanged") is True
          and code.get("pull_request", {}).get("source_commit") == code.get("head"), code)

    # ---- I4: the stack of the session
    deployed, parameter = {"stack_name": f"{config['StackPrefix']}{session_id}"}, f"/agentfarm/{session_id}/{case['parameter']}"
    try:
        described = boto3.client("cloudformation", region_name=REGION).describe_stacks(StackName=deployed["stack_name"])["Stacks"][0]
        deployed.update(status=described["StackStatus"], tags={t["Key"]: t["Value"] for t in described.get("Tags", [])})
        parameters = boto3.client("ssm", region_name=REGION).get_parameters_by_path(Path=f"/agentfarm/{session_id}/")["Parameters"]
        deployed["parameters"] = {p["Name"]: p["Value"] for p in parameters}
    except ClientError as error:
        deployed["error"] = error.response["Error"]["Code"]
    result["stack"] = deployed
    values = deployed.get("parameters", {})
    check("I4", "Stack of the session",
          deployed.get("status") == "CREATE_COMPLETE" and deployed.get("tags", {}).get("commit_id") == code.get("head")
          and list(values) == [parameter] and bool(case["value"](values[parameter])), deployed)

    # ---- I5: the end of the session
    item = table.get_item(TableName=config["SessionTableName"], Key={"session": {"S": f"{agent_id}/{session_id}"}},
                          ConsistentRead=True).get("Item") or {}
    result["session_table_item"] = {k: list(v.values())[0] for k, v in item.items()}
    check("I5", "End of the session", item.get("state", {}).get("S") == "ended",
          {k: result["session_table_item"].get(k) for k in ("state", "end_reason", "issue_number")})

    # ---- I6: AWS CloudTrail
    if "--no-cloudtrail" not in sys.argv:
        save()
        trail, deadline = boto3.client("cloudtrail", region_name=REGION), time.time() + TRAIL_WAIT
        wanted_session, workflow_sessions, assumes = f"issue-{number}-run-{run.get('id')}", [], []
        print("  wait for the events in AWS CloudTrail (some minutes)")
        while time.time() < deadline:
            events = []
            for attribute in ("AssumeRoleWithWebIdentity", "AssumeRole"):
                pages = trail.get_paginator("lookup_events").paginate(
                    LookupAttributes=[{"AttributeKey": "EventName", "AttributeValue": attribute}],
                    StartTime=started - datetime.timedelta(minutes=1))
                events += [json.loads(event["CloudTrailEvent"]) for page in pages for event in page["Events"]]
            workflow_sessions = sorted({(e.get("requestParameters") or {}).get("roleSessionName", "") for e in events
                                        if e["eventName"] == "AssumeRoleWithWebIdentity" and not e.get("errorCode")
                                        and (e.get("requestParameters") or {}).get("roleArn", "").endswith(f"/{start_role}")})
            assumes = [e for e in events if e["eventName"] == "AssumeRole"
                       and (e.get("requestParameters") or {}).get("roleSessionName") == session_id]
            if wanted_session in workflow_sessions and assumes:
                break
            time.sleep(30)
        tags = sorted({json.dumps(sorted((t["key"], t["value"]) for t in (e["requestParameters"].get("tags") or []))) for e in assumes})
        result["cloudtrail"] = {"role sessions of the workflow role": workflow_sessions, "role session of this run": wanted_session,
                                "requests of the broker for credentials of the session": len(assumes),
                                "session tags of every request": tags}
        check("I6", "AWS CloudTrail: the issue in the role session of the workflow and in the session tags",
              wanted_session in workflow_sessions and bool(assumes)
              and tags == [json.dumps([["issue", str(number)], ["task_sha256", task_sha256]])]
              and all(e["requestParameters"].get("sourceIdentity") == agent_id for e in assumes), result["cloudtrail"])

    save()
    print("\n" + ", ".join(f"{key}={value['result']}" for key, value in checks.items()))
    print(f"Wrote {record}")
    return 0 if all(value["result"] == "PASS" for value in checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
