"""Broker Lambda: the Lambda target of the AgentCore Gateway.

The broker verifies the STS-signed JWT again, reads agent_id, session_id and repository from the
verified claims only, and checks in the session table that the session is active. Then it assumes
the agent role with SourceIdentity = agent ID, RoleSessionName = session ID and a session policy
for that session, and performs the action on GitHub or in CloudFormation with those credentials.

The broker reads these tool arguments and no others: `path` (read_file), `files` and `message`
(commit_changes), `title` and `description` (open_pull_request).

Text of the agent is input that nobody reviewed. A file that a session reads is complete or it is not
read: the broker never gives a part of a file, because a later commit would write the part back. The
description of a pull request is the text of the agent in a code block, so that the page of the
repository host shows it as plain text: no image, no link, no mention, no HTML.
The broker writes one log line for every decision. The log never contains the token.

The private key of the GitHub App of an agent signs the JWT that GitHub exchanges for an installation
access token. For an agent with a KMS key the private key is in AWS KMS and the
IDs of the App are SSM parameters: the broker asks AWS KMS for the signature with the credentials of the
session, reads no secret and never has the key. For every other agent the key is in the secret of the App.

The settings of the broker are in `config.json`, a file of the signed package. The function has no
environment variables of its own (identity.py).
"""
import json
import posixpath
import re
import time
import unicodedata

import boto3
from botocore.exceptions import ClientError

import github
from identity import ACCOUNT, AWS, CONFIG, REGION, Rejected, session_key, unverified_ids, verified_identity

AGENT_ROLE_ARN = CONFIG["agent_role_arn"]
STACK_PREFIX = CONFIG["stack_prefix"]
SESSION_TABLE = CONFIG["session_table"]
SESSION_KEY_ATTRIBUTE = "session"   # the partition key of the session table: <agent_id>/<session_id>
# The repositories are on GitHub (github.py), in the account or the organization GITHUB_OWNER. Every agent has its
# own GitHub App.
GITHUB_OWNER = CONFIG["github_owner"]
# For an agent without a KMS key, the secret <prefix><agent_id> in AWS Secrets Manager has the ID, the installation
# ID and the private key of its App.
GITHUB_APP_SECRET_PREFIX = CONFIG.get("github_app_secret_prefix", "agentfarm/github-app/")
# The agents whose GitHub App has its private key in AWS KMS, as the imported key material of the KMS key with
# the alias <prefix><agent_id>. For such an agent the ID of the App and the ID of its installation are the SSM
# parameters <settings prefix><agent_id>/app_id and <settings prefix><agent_id>/installation_id, and the agent has
# no secret. For every other agent the secret has the two IDs and the private key.
GITHUB_APP_KMS_AGENTS = set(CONFIG.get("github_app_kms_agents", []))
GITHUB_APP_KEY_ALIAS_PREFIX = CONFIG.get("github_app_key_alias_prefix", "alias/agentfarm/github-app/")
GITHUB_APP_SIGNING_ALGORITHM = CONFIG.get("github_app_signing_algorithm", "RSASSA_PKCS1_V1_5_SHA_256")
GITHUB_APP_SETTINGS_PREFIX = CONFIG.get("github_app_settings_prefix", "/agentfarm-settings/github-app/")
GITHUB_APP_ID_NAMES = ("app_id", "installation_id")
GITHUB_APP_ID = re.compile(r"[0-9]{1,20}")   # an ID of GitHub is a number: it becomes a part of an address

MAIN_BRANCH = "main"
TEMPLATE_PATH = "template.yaml"
MAX_FILE_CHARACTERS = 100_000
MAX_FILE_BYTES = 4 * MAX_FILE_CHARACTERS   # a text of MAX_FILE_CHARACTERS characters has no more bytes in UTF-8
MAX_FILES = 20
IDENTITY_LINE = re.compile(r"\s*(agent-id|session-id)\s*:", re.IGNORECASE)
# An identity key and its value at any place of a text: `Agent-Id: x`, `agent_id=x`, `SESSION ID : y`.
IDENTITY_KEY = re.compile(r"(agent|session)[\s_\-\u2010-\u2015]*id\s*[:=]\s*[^\s,;'\"\])}]*", re.IGNORECASE)
# The line that names the issue of a session is a line of the broker, like the identity lines.
ISSUE_LINE = re.compile(r"\s*issue\s*:", re.IGNORECASE)
# The words with which GitHub closes an issue at the merge of a pull request or of a commit (`Fixes #5`,
# `closes: owner/repository#5`, `resolved https://github.com/.../issues/5`). The match ends before the reference.
CLOSING_WORD = re.compile(
    r"\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b(?=\s*:?\s*(?:(?:[\w.-]+/[\w.-]+)?#\d+|https?://\S+/(?:issues|pull)/\d+))",
    re.IGNORECASE)
# A session cannot write the configuration of the repository host: workflows, and the files of git.
REFUSED_FOLDERS = (".github", ".git")
# A session cannot write a file of this name in any folder. `.gitattributes` says how the repository host shows a
# file in a review: with it a change can be shown as a generated file or as a binary file, without its lines.
REFUSED_NAMES = (".gitattributes",)
# A word that closes an issue, before the number of an issue.
OWN_CLOSING_WORD = r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b\s*:?\s*#{issue}(?!\d)"
# The session tags of the credentials of a session: AWS CloudTrail has them in the AssumeRole event.
SESSION_TAGS = ("issue", "task_sha256")

# The regional endpoint of AWS STS: CloudTrail then has the AssumeRole event in the Region of the system.
_sts = boto3.client("sts", endpoint_url=f"https://sts.{REGION}.amazonaws.com", **AWS)
_sessions = boto3.client("dynamodb", **AWS)


class Invalid(Exception):
    """A tool request that the broker does not send to AWS."""


def _log(decision: str, tool: str, request_id: str, **fields) -> None:
    print(json.dumps({"decision": decision, "tool": tool, "request_id": request_id, **fields}))


def _bearer_token(custom: dict) -> str:
    headers = custom.get("bedrockAgentCorePropagatedHeaders") or {}
    if isinstance(headers, str):
        headers = json.loads(headers)
    value = next((v for k, v in headers.items() if k.lower() == "authorization"), "")
    if not value.startswith("Bearer "):
        raise Rejected("no bearer token")
    return value[len("Bearer "):]


def check_active(identity: dict) -> dict:
    """The session table has the session from its start. The session ends there, or at the time `expires_at`.
    Return the item of the session."""
    item = _sessions.get_item(TableName=SESSION_TABLE, Key={SESSION_KEY_ATTRIBUTE: {"S": session_key(identity)}},
                              ConsistentRead=True).get("Item")
    if not item:
        raise Rejected("the session is not in the session table")
    if item.get("state", {}).get("S") != "active":
        raise Rejected("the session ended")
    if "expires_at" in item and time.time() >= int(item["expires_at"]["N"]):
        raise Rejected("the session expired")
    # IAM allows a JWT with every repository of the agent. A session works on one repository: the repository
    # that the session table has for it.
    if "repository" in item and item["repository"].get("S") != identity["repository"]:
        raise Rejected("the repository of the token is not the repository of the session")
    return item


def session_facts(item: dict) -> dict:
    """What the session table says about a session beside its state: the issue that started it, and the hash
    of the task that the agent received. The broker writes them in its texts and in the session tags."""
    facts = {}
    number = item.get("issue_number", {}).get("N", "")
    if number.isdigit() and int(number) > 0:
        facts["issue_number"] = int(number)
    task = item.get("task_sha256", {}).get("S", "")
    if re.fullmatch(r"[0-9a-f]{64}", task):
        facts["task_sha256"] = task
    return facts


def end_session(identity: dict) -> dict:
    """End the session of the verified JWT in the session table. From then the broker rejects its JWTs."""
    _sessions.update_item(
        TableName=SESSION_TABLE, Key={SESSION_KEY_ATTRIBUTE: {"S": session_key(identity)}},
        ConditionExpression="#state = :active",
        UpdateExpression="SET #state = :ended, ended_at = :now, end_reason = :reason",
        ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues={":active": {"S": "active"}, ":ended": {"S": "ended"},
                                   ":now": {"N": str(int(time.time()))}, ":reason": {"S": "the session ended its work"}})
    return {"ended": True}


def session_branch(session_id: str) -> str:
    return f"agent/{session_id}"


REPOSITORY_ACCESS, STACK_ACCESS = "repository", "stack"   # the two purposes of the credentials of a session


def session_policy(session_id: str, repository: str, agent_id: str = "", purpose: str = STACK_ACCESS) -> str:
    """The session policy of the credentials of a session for one purpose.

    `STACK_ACCESS`: the stack of the session and its SSM parameters, and nothing else. AWS CloudFormation
    uses these credentials for the template of the agent, so they can read no secret and no repository.

    `REPOSITORY_ACCESS`: what the broker needs for the GitHub App of the agent, and nothing else: the two
    IDs of the App and a signature with the KMS key of the agent, or the secret of the App for an agent
    without a KMS key. The broker asks for them with the credentials of the session, so CloudTrail has
    the requests with the session. Only a request that comes directly from the broker is allowed, not a
    request that an AWS service sends for the session.

    One set of credentials for both purposes would let a template of the agent read the IDs or the
    secret of the App through a dynamic reference of AWS CloudFormation.
    """
    if purpose == STACK_ACCESS:
        statements = [
            {"Effect": "Allow",
             "Action": ["cloudformation:CreateStack", "cloudformation:UpdateStack", "cloudformation:TagResource",
                        "cloudformation:UntagResource", "cloudformation:DescribeStacks",
                        "cloudformation:DescribeStackEvents"],
             "Resource": f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/{STACK_PREFIX}{session_id}/*"},
            {"Effect": "Allow",
             "Action": ["ssm:PutParameter", "ssm:AddTagsToResource", "ssm:RemoveTagsFromResource",
                        "ssm:ListTagsForResource", "ssm:GetParameters", "ssm:DeleteParameter"],
             "Resource": f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/agentfarm/{session_id}/*"}]
    elif purpose != REPOSITORY_ACCESS:
        raise ValueError(f"unknown purpose of session credentials: {purpose}")
    elif agent_id in GITHUB_APP_KMS_AGENTS:
        # The two IDs of the App of the agent, and the signature of the JWT of the App with the KMS key of the
        # agent only and with one algorithm. No secret.
        statements = [
            {"Effect": "Allow", "Action": "ssm:GetParameters",
             "Resource": f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter{GITHUB_APP_SETTINGS_PREFIX}{agent_id}/*",
             "Condition": {"Null": {"aws:CalledViaFirst": "true"}}},
            {"Effect": "Allow", "Action": "kms:Sign", "Resource": f"arn:aws:kms:{REGION}:{ACCOUNT}:key/*",
             "Condition": {"StringEquals": {"kms:RequestAlias": f"{GITHUB_APP_KEY_ALIAS_PREFIX}{agent_id}",
                                            "kms:SigningAlgorithm": GITHUB_APP_SIGNING_ALGORITHM,
                                            "kms:MessageType": "RAW"},
                           "Null": {"aws:CalledViaFirst": "true"}}}]
    else:
        statements = [
            {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
             "Resource": f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{GITHUB_APP_SECRET_PREFIX}{agent_id}-??????",
             "Condition": {"Null": {"aws:CalledViaFirst": "true"}}}]
    return json.dumps({"Version": "2012-10-17", "Statement": statements}, separators=(",", ":"))


def session_credentials(identity: dict, purpose: str) -> tuple[dict, dict]:
    """Temporary credentials of the agent role for one session and one purpose (`STACK_ACCESS` or
    `REPOSITORY_ACCESS`). Return (credentials, size of the session policy)."""
    policy = session_policy(identity["session_id"], identity["repository"], identity["agent_id"], purpose)
    # The issue of the session and the hash of its task are session tags: the AssumeRole event in AWS CloudTrail
    # then connects the session to the issue and to the text that the agent received.
    tags = [{"Key": key, "Value": str(identity[name])}
            for key, name in zip(SESSION_TAGS, ("issue_number", "task_sha256")) if identity.get(name)]
    response = _sts.assume_role(
        RoleArn=AGENT_ROLE_ARN,
        RoleSessionName=identity["session_id"],
        SourceIdentity=identity["agent_id"],
        Policy=policy,
        DurationSeconds=900,
        **({"Tags": tags} if tags else {}),
    )
    return response["Credentials"], {
        "session_policy_characters": len(policy),
        "packed_policy_size_percent": response.get("PackedPolicySize"),
        "session_token_utilization": response.get("SessionTokenUtilization")}


def session_client(service: str, credentials: dict):
    return boto3.client(
        service,
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        **AWS,
    )


def visible(text: str) -> str:
    """A text without the characters that a reader does not see: format characters (zero-width space, word
    joiner, soft hyphen, direction marks, tag characters) and control characters. Line breaks and tabs stay."""
    return "".join(character for character in text
                   if character in "\n\t" or unicodedata.category(character) not in ("Cf", "Cc"))


def without_identity_keys(text, own_issue: int | None = None) -> str:
    """The text of the agent without the lines of the broker (identity lines, the issue line), without an
    identity key and its value in a line, and without a word that closes an issue of the repository.

    NFKC changes full-width letters and special hyphens to the plain characters before the search, and the
    characters that a reader does not see are removed (`visible`): a zero-width space inside a key does not
    hide it from the search. A word that closes the issue of the session itself (`own_issue`) stays: the merge
    then closes that issue.
    """
    def closing_word(match: re.Match) -> str:
        reference = match.string[match.end():].lstrip(" \t:")
        if own_issue and re.match(rf"#{own_issue}(?!\d)", reference):
            return match.group(0)
        return "See"

    plain = visible(unicodedata.normalize("NFKC", str(text or "")))
    lines = [CLOSING_WORD.sub(closing_word, IDENTITY_KEY.sub("", line)).rstrip() for line in plain.splitlines()
             if not IDENTITY_LINE.match(line) and not ISSUE_LINE.match(line)]
    return "\n".join(lines).strip()


def signed_text(text, identity: dict) -> str:
    """The text of the agent without the keys and the lines of the broker, and at its end the lines of the
    broker: the issue of the session, if an issue started it, and the two identity lines."""
    issue = identity.get("issue_number")
    body = without_identity_keys(text, issue) or "(no text)"
    lines = ([f"Issue: #{issue}"] if issue else []) + [f"Agent-Id: {identity['agent_id']}", f"Session-Id: {identity['session_id']}"]
    return body + "\n\n" + "\n".join(lines)


def pull_request_text(text, identity: dict) -> str:
    """The description of a pull request: the text of the agent as plain text, then the lines of the broker.

    The repository host shows a description as Markdown. An image in it makes the browser of a reader, or the
    image proxy of the host, send a request to an address that the agent chose, with data in the address. A
    link, a mention and HTML are text of the agent that looks like text of the host. So the text of the agent
    is in a code block: the host shows it character by character. The fence of the block is longer than every
    run of backticks in the text, so the text cannot end the block.

    A word of the agent that closes the issue of the session has no effect inside a code block: the broker
    writes the closing line itself.
    """
    issue = identity.get("issue_number")
    body = without_identity_keys(text, issue) or "(no text)"
    fence = "`" * max(4, 1 + max((len(run) for run in re.findall(r"`+", body)), default=0))
    closes = bool(issue) and re.search(OWN_CLOSING_WORD.format(issue=issue), body, re.IGNORECASE)
    lines = ([f"Issue: #{issue}"] if issue else []) + [f"Agent-Id: {identity['agent_id']}", f"Session-Id: {identity['session_id']}"]
    return "\n".join(["Text of the agent, shown as plain text:", "", f"{fence}text", body, fence, "",
                      *([f"Closes #{issue}", ""] if closes else []), *lines])


def readable(content: bytes) -> tuple[str | None, str]:
    """The text of a file that a session can read: (text, "") or (None, the reason). A session gets a complete
    file or no file. A part of a file would come back in a commit as the complete file."""
    if len(content) > MAX_FILE_BYTES:
        return None, f"the file has more than {MAX_FILE_CHARACTERS} characters"
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None, "the file is not a text file in UTF-8"
    if "\x00" in text:
        return None, "the file is not a text file in UTF-8"
    if len(text) > MAX_FILE_CHARACTERS:
        return None, f"the file has more than {MAX_FILE_CHARACTERS} characters"
    return text, ""


def _file_answer(branch: str, commit_id: str, path: str, content: bytes) -> dict:
    """The answer of `read_file`: the complete content, or `not_readable` with the reason and no content."""
    text, reason = readable(content)
    answer = {"branch": branch, "commit_id": commit_id, "path": path}
    return {**answer, "content": text} if text is not None else {**answer, "not_readable": reason}


def _files(arguments: dict) -> list[dict]:
    """The files of a commit request: [{"path", "content"}] for a file to create or replace, and
    [{"path", "delete": True}] for a file to delete."""
    files = arguments.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise Invalid(f"files must be a list of 1 to {MAX_FILES} files")
    checked = []
    for entry in files:
        path, content = (entry.get("path"), entry.get("content")) if isinstance(entry, dict) else (None, None)
        delete = isinstance(entry, dict) and entry.get("delete") is True
        if not isinstance(path, str) or not path.strip() or not (delete or isinstance(content, str)):
            raise Invalid("every file needs a path, and a content or the mark of a deletion")
        if not delete and len(content) > MAX_FILE_CHARACTERS:
            raise Invalid(f"the content of {path} is too large")
        # One plain relative path, not in a folder of the configuration of the repository host, and not a file
        # that changes how the repository host shows a change.
        if (path != posixpath.normpath(path) or path.startswith("/") or "\\" in path or path.startswith("..")
                or path.split("/", 1)[0].lower() in REFUSED_FOLDERS
                or posixpath.basename(path).lower() in REFUSED_NAMES):
            raise Invalid(f"a session cannot write the path {path}")
        checked.append({"path": path, "delete": True} if delete else {"path": path, "content": content})
    if len({file["path"] for file in checked}) != len(checked):
        raise Invalid("a path is in the files of the commit more than one time")
    return checked


# ---- The repository on GitHub, with the GitHub App of the agent

def _github_app(credentials: dict, agent_id: str) -> tuple[dict, object]:
    """The GitHub App of an agent: its IDs, and the function that signs its JWT (None: the private key of the
    secret signs). For an agent with a KMS key the IDs are two SSM parameters and AWS KMS signs: the broker asks
    for both with the credentials of the session, reads no secret and never has the key."""
    if agent_id not in GITHUB_APP_KMS_AGENTS:
        app = json.loads(session_client("secretsmanager", credentials).get_secret_value(
            SecretId=f"{GITHUB_APP_SECRET_PREFIX}{agent_id}")["SecretString"])
        if not app.get("private_key"):
            raise Invalid("the secret of the GitHub App has no private key, and the agent has no KMS key")
        return app, None
    names = {f"{GITHUB_APP_SETTINGS_PREFIX}{agent_id}/{name}": name for name in GITHUB_APP_ID_NAMES}
    found = session_client("ssm", credentials).get_parameters(Names=sorted(names))["Parameters"]
    app = {names[parameter["Name"]]: parameter["Value"] for parameter in found if parameter["Name"] in names}
    if set(app) != set(GITHUB_APP_ID_NAMES) or not all(GITHUB_APP_ID.fullmatch(value) for value in app.values()):
        raise Invalid("the IDs of the GitHub App of the agent are not set")
    kms = session_client("kms", credentials)

    def sign(message: bytes) -> bytes:
        return kms.sign(KeyId=f"{GITHUB_APP_KEY_ALIAS_PREFIX}{agent_id}", Message=message, MessageType="RAW",
                        SigningAlgorithm=GITHUB_APP_SIGNING_ALGORITHM)["Signature"]
    return app, sign


def _github_repository(credentials: dict, identity: dict) -> github.Repository:
    """The repository of the session on GitHub, with an installation access token of the App of the agent."""
    app, sign = _github_app(credentials, identity["agent_id"])
    token = github.installation_token(app, identity["repository"], sign=sign)
    repository = github.Repository(GITHUB_OWNER, identity["repository"], token)
    repository.app_key = "kms" if sign else "secret"   # for the log of the broker: where the key of the App is
    return repository


def _github_source(repository: github.Repository, identity: dict) -> tuple[str, str]:
    branch = session_branch(identity["session_id"])
    commit_id = repository.head(branch)
    return (branch, commit_id) if commit_id else (MAIN_BRANCH, repository.head(MAIN_BRANCH))


def _github_session_commit(repository: github.Repository, identity: dict) -> str:
    """The head commit of the session branch. It must be a commit that the broker wrote for this session."""
    commit_id = repository.head(session_branch(identity["session_id"]))
    if not commit_id or not repository.commit_message(commit_id).rstrip().endswith(f"Session-Id: {identity['session_id']}"):
        raise Invalid("the session has no commit: use commit_changes first")
    return commit_id


def _github_tool(tool: str, repository: github.Repository, identity: dict, arguments: dict) -> dict:
    branch = session_branch(identity["session_id"])
    if tool == "list_files":
        source, commit_id = _github_source(repository, identity)
        return {"branch": source, "commit_id": commit_id, "files": repository.files(commit_id)}
    if tool == "read_file":
        path = arguments.get("path")
        if not isinstance(path, str) or not path.strip():
            raise Invalid("path is missing")
        source, commit_id = _github_source(repository, identity)
        return _file_answer(source, commit_id, path, repository.content(commit_id, path, limit=MAX_FILE_BYTES))
    if tool == "commit_changes":
        files = _files(arguments)
        parent = repository.head(branch)
        branch_created = not parent
        if not parent:   # the first commit of the session: the session branch starts at the head of main
            parent = repository.head(MAIN_BRANCH)
        commit_id = repository.commit(branch, parent, files, signed_text(arguments.get("message"), identity), branch_created)
        return {"branch": branch, "branch_created": branch_created, "commit_id": commit_id, "parent_commit_id": parent,
                "author": repository.commit_author(commit_id)}
    if tool == "open_pull_request":
        commit_id = _github_session_commit(repository, identity)
        title = " ".join(without_identity_keys(arguments.get("title"), identity.get("issue_number")).split())[:150] \
            or f"Changes of session {identity['session_id']}"
        pull = repository.pull_request(branch, MAIN_BRANCH, title, pull_request_text(arguments.get("description"), identity))
        return {"pull_request_id": str(pull["number"]), "source_branch": branch, "destination_branch": MAIN_BRANCH,
                "source_commit_id": commit_id, "title": pull["title"], "url": pull["url"], "author": pull["author"]}
    raise Invalid(f"no such repository tool: {tool}")


def _github_template(repository: github.Repository, identity: dict) -> tuple[str, str]:
    commit_id = _github_session_commit(repository, identity)
    return commit_id, repository.file(commit_id, TEMPLATE_PATH)


def _deploy_stack(template: tuple[str, str], cloudformation, identity: dict, stack_name: str) -> dict:
    """`template`: the head commit of the session and its template."""
    commit_id, template_body = template
    arguments = dict(
        StackName=stack_name, TemplateBody=template_body,
        Parameters=[{"ParameterKey": "SessionId", "ParameterValue": identity["session_id"]}],
        Tags=[{"Key": "commit_id", "Value": commit_id}, {"Key": "agent_id", "Value": identity["agent_id"]},
              {"Key": "session_id", "Value": identity["session_id"]},
              {"Key": "repository", "Value": identity["repository"]}],
    )
    try:
        stack_id, operation = cloudformation.create_stack(**arguments)["StackId"], "create"
    except cloudformation.exceptions.AlreadyExistsException:
        stack_id, operation = cloudformation.update_stack(**arguments)["StackId"], "update"
    return {"operation": operation, "stack_id": stack_id, "commit_id": commit_id, "template_path": TEMPLATE_PATH}


REPOSITORY_TOOLS = ("list_files", "read_file", "commit_changes", "open_pull_request")


def run_tool(tool: str, arguments: dict, credentials, identity: dict, stack_name: str, app_keys: list | None = None) -> dict:
    """`credentials(purpose)`: credentials of the session for one purpose. The repository and the stack never
    get the same credentials: the template of the agent runs with credentials that cannot read the repository
    or the secret of the GitHub App."""
    app_keys = app_keys if app_keys is not None else []   # where the key of the GitHub App was: for the log
    if tool in REPOSITORY_TOOLS + ("deploy_stack",):
        repository = _github_repository(credentials(REPOSITORY_ACCESS), identity)
        app_keys.append(getattr(repository, "app_key", "secret"))
        if tool != "deploy_stack":
            return _github_tool(tool, repository, identity, arguments)
        template = _github_template(repository, identity)
        return _deploy_stack(template, session_client("cloudformation", credentials(STACK_ACCESS)), identity, stack_name)
    if tool == "describe_stack":
        stack = session_client("cloudformation", credentials(STACK_ACCESS)).describe_stacks(StackName=stack_name)["Stacks"][0]
        return {"status": stack["StackStatus"], "status_reason": stack.get("StackStatusReason", "")}
    if tool == "get_stack_events":
        events = session_client("cloudformation", credentials(STACK_ACCESS)).describe_stack_events(
            StackName=stack_name)["StackEvents"][:20]
        return {"events": [{
            "time": e["Timestamp"].isoformat(), "resource": e["LogicalResourceId"],
            "type": e["ResourceType"], "status": e["ResourceStatus"],
            "reason": e.get("ResourceStatusReason", ""),
        } for e in events]}
    return {"error": "unknown_tool", "reason": tool}


def handler(event, context):
    custom = (context.client_context.custom if context.client_context else None) or {}
    tool = custom.get("bedrockAgentCoreToolName", "").split("___", 1)[-1]
    request_id = context.aws_request_id

    token = ""
    try:
        token = _bearer_token(custom)
        identity = verified_identity(token)
        facts = session_facts(check_active(identity))
    except Rejected as rejected:
        _log("rejected", tool, request_id, reason=str(rejected), **unverified_ids(token))
        return {"error": "rejected", "reason": str(rejected)}
    del identity["jti"], identity["exp"]
    identity.update(facts)   # not claims of the JWT: values of the session table, for the texts and the session tags

    if tool == "end_session":   # no AWS action of the session: no credentials of the agent role
        result = end_session(identity)
        _log("accepted", tool, request_id, outcome="done", reason="the session ended its work", **identity)
        return {**identity, **result}

    # Temporary credentials for this tool request only, one set for each purpose that the tool needs.
    policy_size, purposes = {}, []

    def credentials(purpose: str) -> dict:
        made, size = session_credentials(identity, purpose)
        policy_size.update(size)
        purposes.append(purpose)
        return made

    stack_name = f"{STACK_PREFIX}{identity['session_id']}"
    app_keys = []
    try:
        result = run_tool(tool, event if isinstance(event, dict) else {}, credentials, identity, stack_name, app_keys)
    except (Invalid, github.Refused) as invalid:
        result = {"error": "invalid_request", "reason": str(invalid)}
    except ClientError as error:
        result = {"error": "aws_error", "code": error.response["Error"]["Code"],
                  "reason": error.response["Error"]["Message"]}
    except github.GitHubError as error:
        result = {"error": "github_error", "code": error.status, "reason": error.message}
    outcome = result.get("error", "done")
    _log("accepted", tool, request_id, outcome=outcome,
         reason=result.get("reason", "the JWT is valid, the role of the agent requested it, the session is active"),
         **identity, stack_name=stack_name, credentials_for=purposes, **policy_size,
         **({"github_app_key": app_keys[0]} if app_keys else {}),
         **{k: result[k] for k in ("commit_id", "pull_request_id", "branch") if k in result})
    return {**identity, "stack_name": stack_name, **result}
