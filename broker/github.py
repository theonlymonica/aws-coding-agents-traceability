"""GitHub as the repository host of the broker: a client for the GitHub REST API.

Every agent has its own GitHub App. The App is installed on the repositories of the agent, and its
private key is a secret in AWS Secrets Manager. For a tool request the broker reads the secret with
the credentials of the session, signs a JWT of the App with the private key, and gets an
installation access token for the repository of the session only. For an agent whose private key is
in AWS KMS, the agent has no secret: the IDs of the App are SSM parameters, and AWS KMS signs the JWT.
The broker never has the key. The token is valid for one hour;
the broker uses it for the request and does not keep it. The agent never has the private key or the
token.

On GitHub a commit of the broker has the App of the agent as its author (`<app>[bot]`). The lines
`Agent-Id` and `Session-Id` end the commit message, as with every repository host.
"""
import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import jwt

API = "https://api.github.com"
API_VERSION = "2022-11-28"


class GitHubError(Exception):
    """An answer of GitHub that is not a success. `status` is the HTTP status."""
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitHub HTTP {status}: {message}")
        self.status, self.message = status, message


class Refused(Exception):
    """A change that the broker does not write to the repository."""


# The modes of a file in a tree of git. A session writes files only: not a symbolic link, not a submodule.
FILE_MODES = ("100644", "100755")


def _request(method: str, url: str, token: str, body: dict | None = None, raw: bool = False, limit: int | None = None):
    """One request to the GitHub REST API. Return the parsed JSON answer, or the bytes of a raw answer.
    `limit`: read no more than this number of bytes and one more, so that the caller sees a larger answer."""
    headers = {"Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
               "Authorization": f"Bearer {token}", "X-GitHub-Api-Version": API_VERSION, "User-Agent": "agentfarm-broker"}
    data = json.dumps(body).encode() if body is not None else None
    if data:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data, headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = response.read(limit + 1) if limit is not None else response.read()
    except urllib.error.HTTPError as error:
        try:
            message = json.loads(error.read()).get("message", "")
        except (json.JSONDecodeError, AttributeError):
            message = ""
        raise GitHubError(error.code, str(message)[:300]) from None
    if raw:
        return payload
    return json.loads(payload) if payload else {}


def _url64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_token(app: dict, sign=None) -> str:
    """The JWT of the App of an agent: RS256, valid for nine minutes, with the ID of the App as its issuer.

    Without `sign` the private key of the secret signs it. With `sign` (a function from the bytes to sign to the
    signature) a key that the broker does not have signs it: the KMS key of the agent.
    """
    now = int(time.time())
    claims = {"iat": now - 60, "exp": now + 540, "iss": str(app["app_id"])}
    if sign is None:
        return jwt.encode(claims, app["private_key"], algorithm="RS256")
    signing_input = ".".join(_url64(json.dumps(part, separators=(",", ":")).encode())
                             for part in ({"alg": "RS256", "typ": "JWT"}, claims))
    return f"{signing_input}.{_url64(sign(signing_input.encode()))}"


def installation_token(app: dict, repository: str, request=_request, sign=None) -> str:
    """An installation access token of the App of an agent, for one repository.

    `app`: {"app_id", "installation_id", "private_key"}, the secret of the App of the agent. With `sign`
    (see `app_token`) the secret needs no private key.
    """
    answer = request("POST", f"{API}/app/installations/{app['installation_id']}/access_tokens", app_token(app, sign), {
        "repositories": [repository],
        "permissions": {"contents": "write", "pull_requests": "write", "metadata": "read"}})
    return answer["token"]


class Repository:
    """One repository on GitHub, with an installation access token of the App of an agent."""

    def __init__(self, owner: str, name: str, token: str, request=_request) -> None:
        self._base, self._owner, self._token, self._request = f"{API}/repos/{owner}/{name}", owner, token, request

    def _call(self, method: str, path: str, body: dict | None = None, raw: bool = False, **options):
        return self._request(method, f"{self._base}{path}", self._token, body, raw, **options)

    def head(self, branch: str) -> str | None:
        """The ID of the head commit of a branch, or None if the branch does not exist."""
        try:
            return self._call("GET", f"/git/ref/heads/{urllib.parse.quote(branch)}")["object"]["sha"]
        except GitHubError as error:
            if error.status == 404:
                return None
            raise

    def commit_message(self, commit_id: str) -> str:
        return self._call("GET", f"/git/commits/{commit_id}").get("message", "")

    def entries(self, commit_id: str) -> dict[str, dict]:
        """All entries of the tree of a commit: {path: {"mode", "type"}}. GitHub gives an incomplete answer for
        a very large tree: the broker then has no complete view of the repository, and the request fails."""
        tree = self._call("GET", f"/git/trees/{commit_id}?recursive=1")
        if tree.get("truncated"):
            raise Refused("the repository has more files than GitHub lists in one answer")
        return {entry["path"]: {"mode": entry.get("mode", "100644"), "type": entry.get("type")}
                for entry in tree.get("tree", [])}

    def files(self, commit_id: str) -> list[str]:
        """The paths of all files of a commit, with a `/` at the start, as the other repository host gives them."""
        return sorted(f"/{path}" for path, entry in self.entries(commit_id).items() if entry["type"] == "blob")

    def content(self, commit_id: str, path: str, limit: int | None = None) -> bytes:
        """The bytes of a file of a commit. `limit`: no more than this number of bytes and one more."""
        quoted = urllib.parse.quote(path.lstrip("/"))
        answer = self._call("GET", f"/contents/{quoted}?ref={commit_id}", raw=True, **({"limit": limit} if limit is not None else {}))
        return answer if isinstance(answer, bytes) else str(answer).encode()

    def file(self, commit_id: str, path: str) -> str:
        return self.content(commit_id, path).decode("utf-8", errors="replace")

    def commit(self, branch: str, parent: str, files: list[dict], message: str, create_branch: bool) -> str:
        """One commit with the files on a branch. `files`: [{"path", "content"}] for a file to create or
        replace, [{"path", "delete": True}] for a file to delete. Return the ID of the commit.

        A file that the parent commit has keeps its mode: a script stays executable. A new file is a plain
        file. A session does not write over a symbolic link, a submodule or a folder, and it deletes only a
        file that the parent commit has.

        The request names no author: GitHub writes the App of the installation access token as the author.
        """
        base_tree = self._call("GET", f"/git/commits/{parent}")["tree"]["sha"]
        existing, changes = self.entries(parent), []
        for file in files:
            path = file["path"].lstrip("/")
            entry = existing.get(path)
            if entry and (entry["type"] != "blob" or entry["mode"] not in FILE_MODES):
                raise Refused(f"{path} is a symbolic link, a submodule or a folder of the repository: a session cannot write it")
            if file.get("delete"):
                if not entry:
                    raise Refused(f"the repository has no file {path} to delete")
                changes.append({"path": path, "mode": entry["mode"], "type": "blob", "sha": None})
            else:
                changes.append({"path": path, "mode": entry["mode"] if entry else "100644", "type": "blob",
                                "content": file["content"]})
        tree = self._call("POST", "/git/trees", {"base_tree": base_tree, "tree": changes})["sha"]
        commit_id = self._call("POST", "/git/commits", {"message": message, "tree": tree, "parents": [parent]})["sha"]
        if create_branch:
            self._call("POST", "/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit_id})
        else:   # no force: the branch moves only from its head to a commit that has the head as its parent
            self._call("PATCH", f"/git/refs/heads/{urllib.parse.quote(branch)}", {"sha": commit_id, "force": False})
        return commit_id

    def commit_author(self, commit_id: str) -> dict:
        commit = self._call("GET", f"/commits/{commit_id}")
        return {"login": (commit.get("author") or {}).get("login"), "name": commit["commit"]["author"]["name"]}

    def pull_request(self, branch: str, base: str, title: str, body: str) -> dict:
        """Open a pull request from a branch. One pull request for a branch: a second request returns the first."""
        try:
            pull = self._call("POST", "/pulls", {"title": title, "body": body, "head": branch, "base": base})
        except GitHubError as error:
            if error.status != 422:
                raise
            existing = self._call("GET", f"/pulls?state=open&head={urllib.parse.quote(f'{self._owner}:{branch}')}")
            if not existing:
                raise
            pull = existing[0]
        return {"number": pull["number"], "title": pull["title"], "url": pull.get("html_url"),
                "author": (pull.get("user") or {}).get("login")}


def decoded(text: str) -> str:
    """The content of a file that the contents API returned as base64 (an answer without the raw media type)."""
    return base64.b64decode(text).decode("utf-8", errors="replace")
