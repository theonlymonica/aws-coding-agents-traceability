"""Write the two IDs of the GitHub App of an agent to the SSM parameters of the agent.

The stack makes the parameters `/agentfarm-settings/github-app/<agent_id>/app_id` and
`/agentfarm-settings/github-app/<agent_id>/installation_id` for an agent of the list KMS_APP_KEY_AGENTS
(infra/registry.py), with a value that says "not set". This command writes the two values. They are arguments
of the command: no file of the repository has them.

Where the values are on GitHub: the App ID in the settings of the App ("General"); the installation ID at the
end of the address of the page of the installation (Settings, Applications, Installed GitHub Apps, Configure).

Usage: AWS_PROFILE=<profile> uv run python runtime/github/set_app_ids.py --agent <agent_id> --app-id <number> --installation-id <number>
"""
import argparse
import os
import re
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from infra.names import GITHUB_APP_ID_NAMES, GITHUB_APP_SETTINGS_PREFIX  # noqa: E402

REGION = os.environ.get("AWS_REGION", "eu-central-1")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--agent", required=True, help="the agent ID, for example agent-alpha")
    parser.add_argument("--app-id", required=True, help="the ID of the GitHub App: a number")
    parser.add_argument("--installation-id", required=True, help="the ID of the installation of the App: a number")
    arguments = parser.parse_args()
    values = dict(zip(GITHUB_APP_ID_NAMES, (arguments.app_id, arguments.installation_id)))
    if not all(re.fullmatch(r"[0-9]{1,20}", value) for value in values.values()):
        print("Not written: an ID of GitHub is a number.")
        return 1
    ssm = boto3.client("ssm", region_name=REGION)
    names = {f"{GITHUB_APP_SETTINGS_PREFIX}{arguments.agent}/{name}": value for name, value in values.items()}
    try:
        existing = {parameter["Name"] for parameter in ssm.get_parameters(Names=sorted(names))["Parameters"]}
        if existing != set(names):
            print(f"Not written: the stack has no parameters for {arguments.agent}. The agent must be in the list "
                  "KMS_APP_KEY_AGENTS of the deployed stack.")
            return 1
        for name, value in names.items():
            version = ssm.put_parameter(Name=name, Value=value, Type="String", Overwrite=True)["Version"]
            print(f"{name}: written, version {version}")
    except ClientError as error:
        print(f"Not written: {error.response['Error']['Code']}: {error.response['Error']['Message']}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
