"""The agent registry: the model, the use cases and the agents of the system."""

# The model of the agent: Anthropic Claude Opus 5.5 through the EU cross-Region inference profile.
MODEL_ID = "eu.anthropic.claude-opus-5-5"
# The use cases. Each use case has a repository and a task text for the tests (runtime/starter/tasks).
USE_CASES = ["greeting-service", "feature-flags", "word-count"]
# The agents, and the use cases that each agent may work on. Each agent of the stack gets its own agent runtime,
# execution role and GitHub App.
AGENTS = {
    "agent-alpha": ["greeting-service", "feature-flags", "word-count"],
    "agent-beta": ["greeting-service", "feature-flags"],
}

# The agents whose GitHub App has its private key in AWS KMS. The stack AgentFarmRuntime makes, for each
# agent of this list, a KMS key without key material and two SSM parameters for the IDs of the App. A person
# imports the private key of the App into the KMS key and writes the two IDs with a command: no value of an App
# is in this repository. The broker asks AWS KMS for the signature of the JWT of the App of such an agent, and
# reads no secret. An agent that leaves the list keeps its KMS key: the stack does not delete a key with the
# private key of an App. An agent that is not in the list needs a secret with the IDs and the private key.
KMS_APP_KEY_AGENTS: list[str] = ["agent-alpha", "agent-beta"]
