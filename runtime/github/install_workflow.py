"""Put the workflows of the system into the repositories on GitHub.

Each repository gets two workflows:

  agentfarm.yml          an issue with the label `agentfarm` starts a session of the agent
  agentfarm-deploy.yml   a merge to the branch main deploys the live environment of the repository and
                         deletes the stack of the session of the merged pull request

For every use case of the deployed stack AgentFarmRuntime, the script fills the workflow templates
(runtime/github/) with the roles of the repository, the Region, the session starter and the agent of the
use case. Without `--push` it prints the workflow files and changes nothing. With `--push` it writes the
files to `.github/workflows/` on the branch main of each repository and makes the label `agentfarm`, with
the GitHub CLI (`gh`) of the person who runs it. The push of agentfarm-deploy.yml is a push to main: it
starts the first deployment of the live environment.

Usage: python runtime/github/install_workflow.py [--push]
"""
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from infra.names import GITHUB_APP_NAME_PREFIX, RUNTIME_STACK  # noqa: E402

REGION = os.environ.get("AWS_REGION", "eu-central-1")
TEMPLATES = ("agentfarm.yml", "agentfarm-deploy.yml")
WORKFLOW_DIRECTORY = ".github/workflows"
LABEL = "agentfarm"
MESSAGES = {"agentfarm.yml": "Start a session of the agent from an issue with the label agentfarm",
            "agentfarm-deploy.yml": "Deploy the live environment after a merge to main"}


def gh(*arguments: str, data: dict | None = None) -> tuple[int, str]:
    done = subprocess.run(["gh", "api", *arguments] + (["--input", "-"] if data is not None else []),
                          input=json.dumps(data) if data is not None else None, capture_output=True, text=True, check=False)
    return done.returncode, (done.stdout or done.stderr).strip()


def main() -> int:
    stack = boto3.client("cloudformation", region_name=REGION).describe_stacks(StackName=RUNTIME_STACK)["Stacks"][0]
    config = {output["OutputKey"]: output["OutputValue"] for output in stack["Outputs"]}
    roles, repositories = json.loads(config["GitHubRoles"]), json.loads(config["Repositories"])
    agents, deploy_roles = json.loads(config["Agents"]), json.loads(config.get("GitHubDeployRoles", "{}"))
    if not roles:
        print("The stack has no roles for GitHub: deploy it with GitHub as the repository host.")
        return 1
    owner = os.environ.get("GITHUB_OWNER") or gh("user", "--jq", ".login")[1]
    push = "--push" in sys.argv
    for use_case, role_arn in roles.items():
        # The agent of a use case: the first agent of the stack that may work on it.
        agent_id = next((agent for agent, use_cases in agents.items() if use_case in use_cases), None)
        if not agent_id:
            print(f"{use_case}: no agent of the stack may work on this use case, no workflow")
            continue
        repository = f"{owner}/{repositories[use_case]}"
        values = {"__ROLE_ARN__": role_arn, "__REGION__": REGION, "__AGENT_ID__": agent_id,
                  "__STARTER_FUNCTION__": config["StarterFunctionName"], "__USE_CASE__": use_case,
                  "__STACK_PREFIX__": config["StackPrefix"], "__REPOSITORY__": repositories[use_case],
                  "__ENVIRONMENT__": f"{config['LiveEnvironmentPrefix']}{use_case}",
                  "__APP_PREFIX__": GITHUB_APP_NAME_PREFIX}
        for template in TEMPLATES:
            if template == "agentfarm-deploy.yml":
                values["__ROLE_ARN__"] = deploy_roles[use_case]
            workflow, path = (Path(__file__).parent / template).read_text(), f"{WORKFLOW_DIRECTORY}/{template}"
            for key, value in values.items():
                workflow = workflow.replace(key, value)
            if not push:
                print(f"----- {repositories[use_case]}: {path}\n{workflow}")
                continue
            code, existing = gh(f"repos/{repository}/contents/{path}", "--jq", ".sha")
            if code == 0 and existing and gh(f"repos/{repository}/contents/{path}", "-H", "Accept: application/vnd.github.raw+json")[1] == workflow.strip():
                print(f"{repositories[use_case]}: {template} is there already, not written")   # no commit, so no deployment
                continue
            body = {"message": MESSAGES[template], "content": base64.b64encode(workflow.encode()).decode(), "branch": "main"}
            if code == 0 and existing:
                body["sha"] = existing
            code, answer = gh("-X", "PUT", f"repos/{repository}/contents/{path}", data=body)
            print(f"{repositories[use_case]}: {template} {'written' if code == 0 else 'NOT written: ' + answer[:200]}")
        if not push:
            continue
        code, answer = gh("-X", "POST", f"repos/{repository}/labels", data={
            "name": LABEL, "color": "5319e7", "description": "Start a session of the agent for this issue"})
        print(f"{repositories[use_case]}: label {'made' if code == 0 else 'exists' if 'already_exists' in answer else 'NOT made: ' + answer[:120]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
