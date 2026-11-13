# agentfarm: AI coding agents on AWS, with agent and session traceability in AWS CloudTrail and on GitHub.
# Credentials come from the environment, for example: AWS_PROFILE=my-profile make runtime-deploy
AWS_REGION ?= eu-central-1
export AWS_REGION
export AWS_DEFAULT_REGION = $(AWS_REGION)
# The agents of the stack AgentFarmRuntime: agent IDs of infra/registry.py, with commas between them. Every agent
# needs its GitHub App.
RUNTIME_AGENTS ?= agent-alpha,agent-beta
export RUNTIME_AGENTS
# The agents of that list that have a box of Strands Box (agent/boxed.py). Empty: none. RUNTIME_BOX_MODE: `agent`
# puts the complete agent inside a box, `work` only the work of a session, with the HTTP server outside the box.
# The stack does not store these values: pass them again on every deployment.
RUNTIME_BOX_AGENTS ?=
RUNTIME_BOX_MODE ?= agent
export RUNTIME_BOX_AGENTS
export RUNTIME_BOX_MODE
# The use case of the tests that start from an issue or check a merge: word-count or feature-flags.
USE_CASE ?= word-count
export JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION = 1
export PYTHONUNBUFFERED = 1

# npx fetches a CDK CLI that matches aws-cdk-lib, independent of a global installation.
CDK = npx --yes aws-cdk@2.1148.1

.PHONY: build unit destroy runtime-deploy runtime-chain runtime-agent runtime-hostile runtime-issue runtime-merge runtime-secret-reference runtime-trail runtime-agents runtime-sessions

# Packages of the Lambda functions. No Docker, no installation at run time.
#   broker       handler, GitHub client and JWT verification, plus PyJWT and cryptography as Linux wheels
#   interceptor  handler
#   starter      handler and the task texts of the tests
# `infra/sign.py` adds the settings (config.json) to each package.
build:
	rm -rf .build/broker .build/interceptor .build/starter
	mkdir -p .build/broker .build/interceptor .build/starter
	uv pip install --quiet --target .build/broker --python-platform x86_64-manylinux_2_28 \
		--python-version 3.13 --only-binary :all: -r broker/requirements.txt
	cp broker/handler.py broker/github.py shared/identity.py .build/broker/
	cp interceptor/handler.py .build/interceptor/
	cp runtime/starter/handler.py .build/starter/
	cp -R runtime/starter/tasks .build/starter/tasks
	find .build -name __pycache__ -type d -prune -exec rm -rf {} +

# Local tests of the broker, the session starter, the GitHub client, the workspace and the workflow files,
# without AWS. Each test runs in its own process: each one imports a package with its own settings.
unit:
	uv run python -m tests.test_broker; broker=$$?; \
	uv run python -m tests.test_runtime; runtime=$$?; \
	uv run python -m tests.test_github; github=$$?; \
	test $$broker -eq 0 -a $$runtime -eq 0 -a $$github -eq 0

# Order: the signing stack; the signature of the packages of the broker, of the interceptor and of the session
# starter with their settings; the stack AgentFarmRuntime. The CDK builds the container image of the agent with
# Docker.
runtime-deploy: build
	uv sync --quiet
	$(CDK) deploy AgentFarmSigning --exclusively --require-approval never
	uv run python -m infra.sign broker interceptor starter
	$(CDK) deploy AgentFarmRuntime --exclusively --require-approval never

# The chain of a session: session starter, runtime session, JWT, gateway, broker, repository and
# stack, and what CloudTrail shows. Writes evidence/runtime/chain-<start time>.json.
runtime-chain:
	uv run python runtime/tests/run_chain.py

# One session of the LLM agent: a library with a defect and failing tests. The agent
# runs the tests in its workspace, corrects the library, commits, opens a pull request and deploys.
# Writes evidence/runtime/agent-<start time>.json.
runtime-agent:
	uv run python runtime/tests/run_agent.py

# One session of the LLM agent with a hostile task: hidden text, a file that hides a change in the review, a
# workflow file, an image and HTML in the description of the pull request. The result must be the same for a
# model that follows the text and for a model that refuses it. Writes evidence/runtime/hostile-<start time>.json.
runtime-hostile:
	uv run python runtime/tests/run_hostile.py

# Deletes the stacks of the sessions and of the live environments, the stack AgentFarmRuntime and the signing
# stack, then lists what remains. It keeps the KMS keys of the GitHub Apps and changes nothing on GitHub.
destroy:
	uv run python -m tests.destroy

# A session that an issue on GitHub starts: the script opens an issue with the label agentfarm in the
# repository of the use case (USE_CASE). Writes evidence/runtime/issue-<start time>.json.
runtime-issue:
	uv run python runtime/tests/run_issue.py --use-case $(USE_CASE)

# What a merge did: the live environment of the repository and the stack of the session, after a person
# merged the pull request of a session. Writes evidence/runtime/merge-<start time>.json.
#   make runtime-merge PULL=4 [USE_CASE=feature-flags]
runtime-merge:
	uv run python runtime/tests/run_merge.py --pull $(PULL) --use-case $(USE_CASE)

# A scripted session whose template asks AWS CloudFormation for a field of the secret of the GitHub App: the
# read must be denied. Writes evidence/runtime/secret-reference-<start time>.json.
runtime-secret-reference:
	uv run python runtime/tests/run_secret_reference.py

# The trail of the stack: the log files in S3 have the management events and the data events of the session of
# the newest record of `make runtime-chain`, or of the record RECORD (a chain run, an agent run or a run with a
# hostile task). Changes nothing. Writes evidence/runtime/trail-<start time>.json.
#   make runtime-trail [RECORD=evidence/runtime/agent-<start time>.json]
runtime-trail:
	uv run python runtime/tests/run_trail.py $(if $(RECORD),--record $(RECORD))

# Two agents of the stack: one scripted session of each, at the same time and in the same repository. Each agent
# must be itself, and only itself. Writes evidence/runtime/agents-<start time>.json.
runtime-agents:
	uv run python runtime/tests/run_agents.py

# Many sessions of one agent: two scripted sessions at the same time in the same repository, and a third one that
# asks for the SSM parameter of the first. Writes evidence/runtime/sessions-<start time>.json.
runtime-sessions:
	uv run python runtime/tests/run_sessions.py
