"""One session of the LLM agent, against the deployed stack AgentFarmRuntime.

The session gets the task of the use case `word-count`: a library with a defect and four tests, three of
which fail. The agent must run the tests in its workspace, correct the library, run the tests again,
commit, open a pull request and deploy. The script checks the result with administrator credentials,
outside the system under test:

  G1  The work of the agent ends, and the model ends its answer.
  G2  The model ran the tests in the workspace, and the last test run passed.
  G3  The repository: the corrected library on the branch of the session, the tests not changed, the author
      and the identity lines of the commit, the pull request.
  G4  The stack of the session: complete, with the version that the task asked for.
  G5  The session ended itself in the session table.
  G6  No JWT and no AWS access key in the logs of the agent runtime (with the transcript of the session),
      of the gateway and of the broker.
  G7  The workspace has no path to the internet, and the packages of the build come from the agent.
  G8  Only for an agent with the setting for a box: the work of the session ran inside a box of Strands Box.

Usage: python runtime/tests/run_agent.py        (writes evidence/runtime/agent-<start time>.json)
"""
import datetime
import json
import os
import re
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
USE_CASE = "word-count"
WORK_WAIT = 25 * 60   # seconds
JWT_PATTERN = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
# An installation access token of a GitHub App, and a private key in PEM form.
GITHUB_SECRET_PATTERN = re.compile(r"gh[spour]_[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----")
ACCESS_KEY_PATTERN = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")


def client(service: str, **kwargs):
    return boto3.client(service, region_name=REGION, **kwargs)


def main() -> int:
    stack = client("cloudformation").describe_stacks(StackName=RUNTIME_STACK)["Stacks"][0]
    config = {output["OutputKey"]: output["OutputValue"] for output in stack["Outputs"]}
    agent_id = config["AgentId"]
    repository = json.loads(config["Repositories"])[USE_CASE]
    functions = client("lambda", config=Config(read_timeout=120, retries={"total_max_attempts": 1}))
    started = datetime.datetime.now(datetime.timezone.utc)
    # One file for each run: a later run does not write over the record of an earlier run.
    record = EVIDENCE / f"agent-{started:%Y%m%dT%H%M%SZ}.json"
    result = {"started": started.strftime("%Y-%m-%dT%H:%M:%SZ"), "config": config, "checks": {}}
    checks = result["checks"]

    def check(key: str, name: str, passed: bool, detail) -> None:
        checks[key] = {"name": name, "result": "PASS" if passed else "FAIL", "detail": detail}
        print(f"{key} {name}: {'PASS' if passed else 'FAIL'}\n    {json.dumps(detail, default=str)[:1100]}")

    print(f"Start a session of {agent_id} for the use case {USE_CASE} (model {config['ModelId']})")
    response = functions.invoke(FunctionName=config["StarterFunctionName"], Payload=json.dumps({
        "agent_id": agent_id, "use_case": USE_CASE, "requested_by": "test run (run_agent.py)"}).encode())
    session = json.loads(response["Payload"].read())
    session_id = session.get("session_id", "")
    result["session"] = session
    if not session_id:
        print(f"the session did not start: {session}")
        return 1

    print(f"  session {session_id}: wait for the work of the agent")
    deadline, status = time.time() + WORK_WAIT, {}
    while time.time() < deadline:
        status = session_status(functions, config, session_id)
        if status.get("state") in ("done", "failed"):
            break
        print(f"    {int(time.time() - started.timestamp())} s: {status.get('state')}, {len(status.get('steps', []))} steps, "
              f"last: {(status.get('steps') or [{}])[-1].get('step')}")
        time.sleep(20)
    result["work"] = status
    steps = status.get("steps", [])
    names = [step["step"] for step in steps]

    # ---- G1: the work of the agent
    check("G1", "Work of the agent",
          status.get("state") == "done" and status.get("stop_reason") == "end_turn"
          and all(name in names for name in ("workspace_checkout", "workspace_run", "workspace_commit",
                                             "open_pull_request", "deploy_stack")),
          {"state": status.get("state"), "stop_reason": status.get("stop_reason"), "turns": status.get("turns"),
           "steps": [f"{step['step']}: {step['status']}" for step in steps], "report": (status.get("report") or "")[:600]})

    # ---- G2: the tests in the workspace
    runs = [step for step in steps if step["step"] == "workspace_run"]
    test_runs = [step for step in runs if "pytest" in step["output"] or "unittest" in step["output"]]
    last = test_runs[-1]["output"] if test_runs else ""
    commit_index = names.index("workspace_commit") if "workspace_commit" in names else -1
    tests_before_commit = [step for step in steps[:commit_index] if step in test_runs] if commit_index >= 0 else []
    check("G2", "Tests in the workspace",
          bool(test_runs) and "4 passed" in last and " failed" not in last.split("passed")[-1]
          and bool(tests_before_commit) and "4 passed" in tests_before_commit[-1]["output"],
          {"commands of the model": [json.loads(step["output"]).get("command", "")[:140] if step["output"].startswith("{")
                                     and step["output"].rstrip().endswith("}") else step["output"][:140] for step in runs],
           "first test run": test_runs[0]["output"][-260:] if test_runs else None, "last test run": last[-260:]})

    # ---- G7: the workspace has no path to the internet, and the packages of the build come from the agent
    installs = [step for step in steps if step["step"] == "workspace_install"]
    check("G7", "Workspace without a path to the internet",
          status.get("workspace_network") == "no path to the internet"
          and status.get("workspace_names") == "resolves no name"
          and any(step["status"] == "success" and "pytest" in step["output"] for step in installs),
          {"check of the agent at the start of the work": status.get("workspace_network"),
           "name resolution in the workspace": status.get("workspace_names"),
           "answers of the name resolution probe": status.get("workspace_name_resolution"),
           "installations": [f"{step['status']}: {step['output'][:200]}" for step in installs]})

    # ---- G3: the repository
    repo, branch = hosted_repository(config, repository, REGION), f"agent/{session_id}"
    code = {"host": repo.host}
    try:
        head = repo.head(branch)
        if head:
            code.update({"head": head, **repo.commit(head), "pull_request": repo.pull_request(branch)})
            code.update({"wordcount.py": repo.file(head, "wordcount.py"), "template.yaml": repo.file(head, "template.yaml"),
                         "tests_unchanged": repo.file(head, "test_wordcount.py")
                         == (ROOT / "repositories" / USE_CASE / "test_wordcount.py").read_text()})
        else:
            code["error"] = "the session has no branch"
    except (ClientError, LookupError) as error:
        code["error"] = str(error)[:200]
    result["repository"] = code
    lines = f"Agent-Id: {agent_id}\nSession-Id: {session_id}"
    check("G3", "Repository",
          repo.author_of(agent_id, code.get("author", "")) and code.get("message", "").rstrip().endswith(lines)
          and 'split(" ")' not in code.get("wordcount.py", 'split(" ")') and "1.0.1" in code.get("template.yaml", "")
          and code.get("tests_unchanged") is True
          and (code.get("pull_request", {}).get("description") or "").rstrip().endswith(lines)
          and code.get("pull_request", {}).get("source_commit") == code.get("head"), code)

    # ---- G4: the stack of the session
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
    check("G4", "Stack of the session",
          deployed.get("status") == "CREATE_COMPLETE" and deployed.get("tags", {}).get("commit_id") == code.get("head")
          and deployed.get("parameters") == {f"/agentfarm/{session_id}/word-count-version": "1.0.1"}, deployed)

    # ---- G5: the end of the session
    item = client("dynamodb").get_item(TableName=config["SessionTableName"], Key={
        "session": {"S": f"{agent_id}/{session_id}"}}, ConsistentRead=True).get("Item") or {}
    result["session_table_item"] = {k: list(v.values())[0] for k, v in item.items()}
    ended = next((step for step in steps if step["step"] == "end_session"), {})
    check("G5", "End of the session",
          item.get("state", {}).get("S") == "ended" and ended.get("status") == "success", result["session_table_item"])

    # ---- G6: no JWT and no access key in the logs
    time.sleep(45)   # the vended log of the gateway arrives some seconds after a request
    logs = client("logs")
    groups = {"agent runtime": f"/aws/bedrock-agentcore/runtimes/{config['AgentRuntimeId']}-DEFAULT",
              "gateway": config["GatewayLogGroupName"], "broker": config["BrokerLogGroupName"]}
    searched, transcript, box_events = {}, [], []
    for name, group in groups.items():
        try:
            pages = logs.get_paginator("filter_log_events").paginate(
                logGroupName=group, startTime=int(started.timestamp() * 1000))
            messages = [event["message"] for page in pages for event in page["events"]]
            text = "\n".join(messages)
            searched[name] = {"log_group": group, "events": len(messages), "characters": len(text),
                              "jwt_shaped_strings": len(JWT_PATTERN.findall(text)),
                              "github_tokens_and_private_keys": len(GITHUB_SECRET_PATTERN.findall(text)),
                              "aws_access_key_ids": len(ACCESS_KEY_PATTERN.findall(text))}
            if name == "agent runtime":
                for message in messages:
                    try:
                        line = json.loads(message[message.index("{"):])
                    except ValueError:
                        continue
                    if line.get("event") in ("task", "model_text", "tool_call", "tool_result", "session_end", "work_end"):
                        transcript.append(line)
                    if line.get("event") in ("box_run", "box_end", "box_error"):
                        box_events.append(line)
        except ClientError as error:
            searched[name] = {"log_group": group, "error": error.response["Error"]["Code"]}
    result["logs"], result["transcript"] = searched, transcript
    check("G6", "No JWT and no access key in the logs",
          all(v.get("jwt_shaped_strings") == 0 and v.get("aws_access_key_ids") == 0
              and v.get("github_tokens_and_private_keys") == 0 for v in searched.values())
          and searched["agent runtime"].get("events", 0) > 0 and bool(transcript), searched)

    # ---- G8: with the setting for a box, the work of the session ran inside a box of Strands Box
    if agent_id in config.get("BoxAgents", "").split(","):
        process = status.get("box") or {}
        ended_box = next((event for event in box_events if event["event"] == "box_end"), {})
        result["box"] = box_events
        check("G8", "The work of the session ran inside a box",
              process.get("mode") == config.get("BoxMode")
              # The process of the work has the placeholder key of a box, and the box refuses an address that
              # the agent does not need.
              and str(process.get("access_key_id_of_the_process", "")).startswith("AKIAIOSFODNN")
              and "403" in str(process.get("https://example.com", ""))
              # In the mode `work` the server outside the box writes the end of `box run` to the log.
              and (config.get("BoxMode") != "work" or ended_box.get("exit_code") == 0)
              and not any(event["event"] == "box_error" for event in box_events),
              {"agents with a box": config.get("BoxAgents"), "mode": config.get("BoxMode"),
               "process of the work": process, "exit code of box run (mode work)": ended_box.get("exit_code")})

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (record).write_text(json.dumps(result, indent=2, default=str))
    print("\n" + ", ".join(f"{key}={value['result']}" for key, value in checks.items()))
    print(f"Wrote {record}")
    return 0 if all(value["result"] == "PASS" for value in checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
