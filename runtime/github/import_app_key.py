"""Import the private key of the GitHub App of an agent into the KMS key of the agent.

The person who has the private key of the App runs this script. The key is the PEM file that GitHub gave
when the key was generated (the settings of the App, "Private keys"). The script reads the file, imports the
key into the KMS key with the alias `alias/agentfarm/github-app/<agent_id>`, and checks that the public key of
the KMS key is the public key of the file. It prints the fingerprint of the key: the settings of the App on
GitHub show the same fingerprint for the key. It never prints the key and writes it nowhere.

The agent must be in the list KMS_APP_KEY_AGENTS (infra/registry.py) of the deployed stack: the stack makes
the KMS key. A KMS key that has key material already is not changed.

The broker signs with this KMS key for every request of the agent to a repository. It needs the two IDs of
the App too: write them with runtime/github/set_app_ids.py.

Usage: AWS_PROFILE=<profile> uv run python runtime/github/import_app_key.py --agent <agent_id> --pem <file>
"""
import argparse
import json
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from infra.app_key import NotImported, import_private_key  # noqa: E402
from infra.names import GITHUB_APP_KEY_ALIAS_PREFIX  # noqa: E402

REGION = os.environ.get("AWS_REGION", "eu-central-1")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--agent", required=True, help="the agent ID, for example agent-alpha")
    parser.add_argument("--pem", required=True, help="the PEM file with the private key that GitHub gave")
    parser.add_argument("--key", help="a KMS key ID or alias, in the place of the alias of the agent (for a test)")
    arguments = parser.parse_args()
    key_id = arguments.key or f"{GITHUB_APP_KEY_ALIAS_PREFIX}{arguments.agent}"
    try:
        answer = import_private_key(boto3.client("kms", region_name=REGION), key_id,
                                    Path(arguments.pem).expanduser().read_bytes())
    except (NotImported, OSError) as error:
        print(f"Not imported: {error}")
        return 1
    except ClientError as error:
        print(f"Not imported: {error.response['Error']['Code']}: {error.response['Error']['Message']}")
        return 1
    answer.pop("public_key_der")
    print(json.dumps(answer, indent=2))
    if not answer["the_kms_key_has_the_key_of_the_file"]:
        print("\nThe KMS key has a different key. Its key material cannot be replaced: the key of this file needs a "
              "KMS key of its own.")
        return 1
    print(f"""
The KMS key {key_id} has the private key of the file.
Compare the fingerprint with the one in the settings of the App on GitHub ("Private keys").

Write the two IDs of the App, if they are not written yet:

  uv run python runtime/github/set_app_ids.py --agent {arguments.agent} --app-id <App ID> --installation-id <installation ID>

Then run one session (`make runtime-chain`) and check the commit of the App. When the sessions work, delete
the PEM file.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
