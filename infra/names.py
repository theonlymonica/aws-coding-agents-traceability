"""Fixed names of the system.

The stacks, the settings of the signed packages (infra/config.py) and the tests use the same names.
A name here is not an output of CloudFormation: it is known before a stack exists, so a signed
package can contain it.
"""
AUDIENCE = "agentfarm-gateway"
# Stack of a session: <STACK_PREFIX><session_id>. SSM parameters of a session: /agentfarm/<session_id>/...
STACK_PREFIX = "agentfarm-"
REPOSITORY_PREFIX = "agentfarm-"
TARGET_NAME = "broker"
TOKEN_LIFETIME_SECONDS = 900
JWT_TAGS = ["agent_id", "session_id", "repository"]

# The stack AgentFarmRuntime (runtime/infra/stack.py): the agents in Amazon Bedrock AgentCore Runtime.
RUNTIME_PREFIX = "agentfarm-runtime"
# The execution role of the agent runtime of an agent requests the JWT of its sessions: <prefix><agent_id>.
RUNTIME_TOKEN_ROLE_PREFIX = "agentfarm-runtime-exec-"
RUNTIME_AGENT_ROLE = "agentfarm-runtime-agent"
RUNTIME_SESSION_TABLE = "agentfarm-runtime-sessions"
# For an agent that is not in the list KMS_APP_KEY_AGENTS (infra/registry.py): the secret <prefix><agent_id> in
# AWS Secrets Manager has the ID, the installation ID and the private key of the GitHub App of the agent. A person
# creates the secret.
GITHUB_APP_SECRET_PREFIX = "agentfarm/github-app/"
# For an agent of the list KMS_APP_KEY_AGENTS (infra/registry.py) the stack has a KMS key with this alias and the
# agent ID. A person imports the private key of the GitHub App into it (runtime/github/import_app_key.py), and the
# broker then asks AWS KMS for the signature of the JWT of the App: RS256, the algorithm of a GitHub App.
GITHUB_APP_KEY_ALIAS_PREFIX = "alias/agentfarm/github-app/"
GITHUB_APP_SIGNING_ALGORITHM = "RSASSA_PKCS1_V1_5_SHA_256"
# The ID of the GitHub App of such an agent and the ID of its installation are two SSM parameters,
# <prefix><agent_id>/app_id and <prefix><agent_id>/installation_id. The stack makes them with the value
# GITHUB_APP_ID_NOT_SET, and a person writes the values with a command (runtime/github/set_app_ids.py): the
# values are in no file of the repository. The path is not under /agentfarm/, the path of the SSM parameters of
# the sessions and of the live environments.
GITHUB_APP_SETTINGS_PREFIX = "/agentfarm-settings/github-app/"
GITHUB_APP_ID_NAMES = ("app_id", "installation_id")
GITHUB_APP_ID_NOT_SET = "not-set"
# The name of the GitHub App of an agent is this prefix and the agent ID. GitHub writes `<name>[bot]` as the author
# of a commit of the App.
GITHUB_APP_NAME_PREFIX = "agentfarm-"
# The session tags that the broker sets on a session of the agent role.
SESSION_TAGS = ["issue", "task_sha256"]
# A repository on GitHub starts a session with the role <prefix><use case> (runtime/github/agentfarm.yml).
GITHUB_ROLE_PREFIX = "agentfarm-github-"
# After a merge to the branch main, a workflow of the repository deploys the live environment of its use case.
# The stack of the live environment is `<STACK_PREFIX>live-<use case>`, and its SSM parameters start with
# `/agentfarm/live-<use case>/`. A session ID has 33 characters or more, so the stack and the parameters of a
# session never have these names.
GITHUB_DEPLOY_ROLE_PREFIX = "agentfarm-deploy-"
LIVE_ENVIRONMENT_PREFIX = "live-"
GITHUB_OIDC_HOST = "token.actions.githubusercontent.com"
RUNTIME_SESSION_SECONDS = 3600   # a session expires this time after its start, if it did not end before

SIGNING_STACK = "AgentFarmSigning"
RUNTIME_STACK = "AgentFarmRuntime"


def role_arn(account: str, name: str) -> str:
    return f"arn:aws:iam::{account}:role/{name}"


def function_arn(account: str, region: str, name: str) -> str:
    return f"arn:aws:lambda:{region}:{account}:function:{name}"
