"""What a test reads from the repository of a session, outside the system under test.

The tests read with the access of the person who runs them: the GitHub CLI (`gh`). The answers:

  head(branch)           the ID of the head commit of a branch, or None
  commit(commit_id)      {"author", "message"}
  file(commit_id, path)  the content of a file
  files(commit_id)       {path: ID of the content} of all files of a commit
  pull_request(branch)   {"id", "title", "description", "source_commit", "author"} of the open pull request of a
                         branch, or {}
  author_of(agent_id)    True if an author is the author that GitHub writes for a commit of this agent
"""
import json
import os
import subprocess


class GitHubRepository:
    host = "GitHub"

    def __init__(self, name: str) -> None:
        self.name = name
        self.owner = os.environ.get("GITHUB_OWNER") or self._gh("user", jq=".login")
        self._base = f"repos/{self.owner}/{name}"

    @staticmethod
    def _gh(path: str, jq: str | None = None, raw: bool = False) -> str:
        command = ["gh", "api", path] + (["--jq", jq] if jq else [])
        if raw:
            command += ["-H", "Accept: application/vnd.github.raw+json"]
        done = subprocess.run(command, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise LookupError(done.stderr.strip()[:200])
        return done.stdout if raw else done.stdout.strip()

    def head(self, branch: str) -> str | None:
        try:
            return self._gh(f"{self._base}/git/ref/heads/{branch}", jq=".object.sha") or None
        except LookupError:
            return None

    def commit(self, commit_id: str) -> dict:
        commit = json.loads(self._gh(f"{self._base}/commits/{commit_id}"))
        return {"author": (commit.get("author") or {}).get("login") or commit["commit"]["author"]["name"],
                "message": commit["commit"]["message"]}

    def file(self, commit_id: str, path: str) -> str:
        return self._gh(f"{self._base}/contents/{path}?ref={commit_id}", raw=True)

    def files(self, commit_id: str) -> dict:
        tree = json.loads(self._gh(f"{self._base}/git/trees/{commit_id}?recursive=1"))
        return {entry["path"]: entry["sha"] for entry in tree.get("tree", []) if entry.get("type") == "blob"}

    def pull_request(self, branch: str) -> dict:
        pulls = json.loads(self._gh(f"{self._base}/pulls?state=open&head={self.owner}:{branch}"))
        if not pulls:
            return {}
        pull = pulls[0]
        return {"id": str(pull["number"]), "title": pull["title"], "description": pull.get("body"),
                "source_commit": pull["head"]["sha"], "author": pull["user"]["login"]}

    def author_of(self, agent_id: str, author: str) -> bool:
        return author.endswith("[bot]")   # GitHub writes the GitHub App of the agent as the author


def repository(config: dict, name: str, region: str):
    """The repository of a session on GitHub."""
    return GitHubRepository(name)
