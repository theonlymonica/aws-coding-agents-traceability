"""Session starter: starts a session of an agent in Amazon Bedrock AgentCore Runtime.

AgentCore Runtime accepts every runtime session ID that a caller sends, and it accepts the ID of a
session that ended for a different microVM. So the session ID of the system does not come from a
caller. This function makes the session ID (a UUID), writes the session to the session table one
time, and starts the runtime session with that ID. The broker accepts a JWT only for a session that
this function wrote, and only until the session expires.

The role of this function is the only role of the system that can invoke an agent runtime. The
function never has a token: the runtime session requests its own JWT, and IAM binds that JWT to the
runtime session ID.

A session from an issue. A workflow of a repository on GitHub invokes the alias of this function that
has the name of the use case of the repository. The IAM role of the repository can invoke that alias
only, so the use case comes from the alias, not from the event. The task of the session is the text
of the issue. A person wrote that text: it is input of the agent, not an instruction for this function.

The agent gets the text that a person sees on the page of the issue. The page does not show an HTML
comment, a comment in the form of a link definition (`[//]: # (text)`) or a format character, and a
model reads them: the person who sets the label would start a session with a text that this person did
not see. The function removes them before it makes the task (`visible_text`), and it records how many
characters it removed.

  Event: {"agent_id": "<agent>", "requested_by": "<GitHub user>",
          "issue": {"number": <number>, "title": "<title>", "body": "<text>", "url": "<address>"}}

A session without an issue (the tests): an invocation of the function without an alias.

  Event: {"agent_id": "<agent>", "use_case": "<use case>", "requested_by": "<who asked for the work>"}
  The task text is the file of the use case in this package (`tasks/<use case>.md`).
  Test fields, optional: "parameter_name" (a scripted work asks for an SSM parameter with this name, which can be
  a name of a different session: the request must fail), "mode": "scripted" (a scripted work without a model), "greeting" (the value that
  the scripted work writes), "task" (a different task text).

The settings are in `config.json`, a file of the signed package. The ARNs of the agent runtimes are
an environment variable (AGENT_RUNTIMES): CloudFormation makes them with the stack that has this function.
"""
import hashlib
import json
import os
import re
import time
import unicodedata
import uuid
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

CONFIG = json.loads(Path(__file__).with_name("config.json").read_text())
REGION = CONFIG["region"]
AGENTS = CONFIG["agents"]                          # {agent ID: [use case, ...]}
REPOSITORIES = CONFIG["repositories"]              # {use case: repository}
SESSION_TABLE = CONFIG["session_table"]
SESSION_SECONDS = CONFIG["session_seconds"]        # a session expires this time after its start
TASKS = Path(__file__).resolve().parent / "tasks"
TEST_FIELDS = ("mode", "greeting", "task", "parameter_name")
MAX_ISSUE_TEXT = 8000   # characters of the text of an issue that become the task
KEEP_ITEM_SECONDS = 365 * 24 * 3600   # the item of a session stays in the session table for one year after its expiry
AGENT_RUNTIMES = json.loads(os.environ.get("AGENT_RUNTIMES", "{}"))   # {agent ID: ARN of its agent runtime}

_aws = {"region_name": REGION, "config": Config(ignore_configured_endpoint_urls=True, read_timeout=60,
                                                 retries={"total_max_attempts": 2})}
_table = boto3.client("dynamodb", **_aws)
_runtime = boto3.client("bedrock-agentcore", **_aws)


def _log(decision: str, request_id: str, **fields) -> None:
    print(json.dumps({"decision": decision, "request_id": request_id, **fields}))


def _end_session(key: dict, reason: str) -> None:
    _table.update_item(
        TableName=SESSION_TABLE, Key=key,
        UpdateExpression="SET #state = :ended, ended_at = :now, end_reason = :reason",
        ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues={":ended": {"S": "ended"}, ":now": {"N": str(int(time.time()))},
                                   ":reason": {"S": reason}})


# What the page of an issue does not show. An HTML comment ends at `-->`, or at the end of the text if it has no end.
HTML_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
# A link definition that points to nothing: the usual form of a comment in Markdown.
MARKDOWN_COMMENT = re.compile(r"^ {0,3}\[[^\]\n]*\]:[ \t]*(?:#|<>)(?:[ \t][^\n]*)?$", re.MULTILINE)


def visible_text(text: str) -> tuple[str, int]:
    """A text without the parts that the page of an issue does not show: HTML comments, comments in the form of
    a link definition, and the characters that a reader does not see (format characters such as a zero-width
    space or a tag character, control characters, private-use characters). Line breaks and tabs stay: an issue
    that a person wrote in a browser has a carriage return in every line break.
    Return (the text, the number of characters removed)."""
    kept = MARKDOWN_COMMENT.sub("", HTML_COMMENT.sub("", text))
    kept = "".join(character for character in kept
                   if character in "\n\r\t" or unicodedata.category(character) not in ("Cf", "Cc", "Co"))
    return kept, len(text) - len(kept)


def alias_of(function_arn: str) -> str:
    """The alias of an invocation: the last element of an ARN with a qualifier, or an empty text."""
    elements = str(function_arn or "").split(":")
    return elements[7] if len(elements) == 8 else ""


def issue_of(event: dict) -> dict | None:
    """The issue of an event: {"number", "title", "body", "url"}, or None if the event has no valid issue."""
    issue = event.get("issue")
    if not isinstance(issue, dict) or not isinstance(issue.get("number"), int) or issue["number"] < 1:
        return None
    title, body, url = issue.get("title"), issue.get("body") or "", issue.get("url") or ""
    if not isinstance(title, str) or not title.strip() or not isinstance(body, str) or not isinstance(url, str):
        return None
    return {"number": issue["number"], "title": title.strip()[:300], "body": body[:MAX_ISSUE_TEXT],
            "url": url[:300] if url.startswith("https://github.com/") else ""}


def status(event: dict, request_id: str) -> dict:
    """The state of the work of a session, for a caller that cannot invoke the agent runtime: the policy of an
    agent runtime lets only the role of this function send it a request. Only for a session of the session
    table, and only the request `status`, which changes nothing in the session."""
    agent_id, session_id = event.get("agent_id"), event.get("session_id")
    if (not isinstance(agent_id, str) or agent_id not in AGENT_RUNTIMES or not isinstance(session_id, str)
            or not re.fullmatch(r"[0-9a-f-]{36}", session_id)):
        return {"error": "rejected", "reason": "unknown agent or session"}
    item = _table.get_item(TableName=SESSION_TABLE, Key={"session": {"S": f"{agent_id}/{session_id}"}}).get("Item")
    if not item:
        _log("rejected", request_id, reason="status of a session that is not in the session table", agent_id=agent_id)
        return {"error": "rejected", "reason": "unknown agent or session"}
    try:
        response = _runtime.invoke_agent_runtime(
            agentRuntimeArn=AGENT_RUNTIMES[agent_id], runtimeSessionId=session_id,
            payload=json.dumps({"action": "status"}).encode(), contentType="application/json", accept="application/json")
        answer = json.loads(response["response"].read() or b"{}")
    except ClientError as error:
        return {"state": "unknown", "error": error.response["Error"]["Code"]}
    return answer if isinstance(answer, dict) else {"state": "unknown"}


def handler(event, context):
    event = event if isinstance(event, dict) else {}
    request_id = context.aws_request_id
    agent_id, use_case = event.get("agent_id"), event.get("use_case")
    requested_by = str(event.get("requested_by") or "")[:200]
    # An alias with the name of a use case: the invocation of a repository on GitHub. Its role can invoke this
    # alias only, so the alias says which repository asks. The event cannot select a different use case.
    alias = alias_of(getattr(context, "invoked_function_arn", ""))
    if event.get("action") == "status" and alias not in REPOSITORIES:   # not a request of a repository
        return status(event, request_id)
    issue = None
    if alias in REPOSITORIES:
        use_case, issue = alias, issue_of(event)
        if issue is None:
            _log("rejected", request_id, reason="no valid issue", use_case=use_case)
            return {"error": "rejected", "reason": "no valid issue"}
    if not isinstance(agent_id, str) or agent_id not in AGENTS or agent_id not in AGENT_RUNTIMES:
        _log("rejected", request_id, reason="unknown agent", agent_id=str(agent_id)[:64])
        return {"error": "rejected", "reason": "unknown agent"}
    if not isinstance(use_case, str) or use_case not in AGENTS[agent_id] or use_case not in REPOSITORIES:
        _log("rejected", request_id, reason="the agent may not work on the use case", agent_id=agent_id,
             use_case=str(use_case)[:64])
        return {"error": "rejected", "reason": "the agent may not work on the use case"}
    repository = REPOSITORIES[use_case]

    # The task of the session. With an issue, the text of the issue is the task, and the session has no test field:
    # the session of an issue is a session of the agent.
    if issue:
        test_fields = {}
        task = f"Issue #{issue['number']} of the repository: {issue['title']}\n\n{issue['body']}"
    else:
        test_fields = {name: event[name] for name in TEST_FIELDS if isinstance(event.get(name), str) and event[name]}
        task = test_fields.get("task") or (TASKS / f"{use_case}.md").read_text()
    # The agent gets the text that a person sees: no HTML comment, no character that a reader does not see.
    task, hidden_characters = visible_text(task)
    if "task" in test_fields:
        test_fields["task"] = task
    # The hash of the text that the agent receives. The session table and, through the broker, AWS CloudTrail
    # keep it: a later change of the issue can be compared with it.
    task_sha256 = hashlib.sha256(task.encode()).hexdigest()

    # The session ID is a value of this function, not of the caller: one session, one ID, one time.
    session_id = str(uuid.uuid4())
    now = int(time.time())
    session = {"agent_id": agent_id, "session_id": session_id, "repository": repository, "use_case": use_case}
    key = {"session": {"S": f"{agent_id}/{session_id}"}}
    _table.put_item(
        TableName=SESSION_TABLE, ConditionExpression="attribute_not_exists(#session)",
        ExpressionAttributeNames={"#session": "session"},
        Item={**key, **{name: {"S": value} for name, value in session.items()},
              "requested_by": {"S": requested_by or "(not given)"}, "state": {"S": "active"},
              "task_sha256": {"S": task_sha256}, "task_hidden_characters": {"N": str(hidden_characters)},
              **({"issue_number": {"N": str(issue["number"])}, "issue_url": {"S": issue["url"] or "(not given)"}}
                 if issue else {}),
              "started_at": {"N": str(now)}, "expires_at": {"N": str(now + SESSION_SECONDS)},
              "delete_after": {"N": str(now + SESSION_SECONDS + KEEP_ITEM_SECONDS)}})

    work = {"action": "work", "repository": repository, **test_fields, "task": task,
            **({"issue_number": issue["number"]} if issue else {})}
    try:
        response = _runtime.invoke_agent_runtime(
            agentRuntimeArn=AGENT_RUNTIMES[agent_id], runtimeSessionId=session_id,
            payload=json.dumps(work).encode(), contentType="application/json", accept="application/json")
        answer = json.loads(response["response"].read() or b"{}")
        if response.get("statusCode") != 200 or not isinstance(answer, dict) or not answer.get("accepted"):
            raise ValueError(json.dumps(answer)[:300])
    except (ClientError, ValueError) as error:
        reason = (f"{error.response['Error']['Code']}: {error.response['Error']['Message']}"[:400]
                  if isinstance(error, ClientError) else str(error))
        _end_session(key, "the runtime session did not start")
        _log("failed", request_id, reason=reason, **session)
        return {"error": "not_started", "reason": reason, **session}

    _log("started", request_id, reason="the agent may work on the use case", requested_by=requested_by,
         agent_runtime=AGENT_RUNTIMES[agent_id], expires_at=now + SESSION_SECONDS, test_fields=sorted(test_fields),
         task_sha256=task_sha256, task_hidden_characters=hidden_characters,
         issue=({"number": issue["number"], "url": issue["url"]} if issue else None), **session)
    return {**session, "agent_runtime_arn": AGENT_RUNTIMES[agent_id], "expires_at": now + SESSION_SECONDS,
            "task_sha256": task_sha256, "task_hidden_characters": hidden_characters,
            **({"issue_number": issue["number"]} if issue else {})}
