# AI coding agents on AWS with agent and session traceability

A person opens an issue in a repository on GitHub. An AI agent does the work that the issue asks for:
it changes the code, tests the change, opens a pull request and deploys a test copy of the
application. A person reviews and merges the pull request, and the merge deploys the live
environment.

Many agents and many sessions share one AWS account and the same repositories. The system makes the
records show, for every action, which agent and which session did it:

- AWS CloudTrail shows the agent and the session for every AWS action of an agent.
- The repository shows the agent and the session for every commit and every pull request.
- The deployment of the live environment is an action of a pipeline after the merge of a person, not
  an action of an agent, and CloudTrail shows the merged commit.

The agent is a real LLM agent on Amazon Bedrock AgentCore Runtime. It holds no AWS credentials for its
AWS actions, no git credentials and no GitHub token. The code that it builds and tests runs in a
sandbox without credentials and without a network.

The name of the system, and the prefix of its stacks and resources, is `agentfarm`.

## The flow

```
 person                 GitHub                          AWS                                     records
 ──────                 ──────                          ───                                     ───────
 opens an issue,   ──▶  workflow agentfarm        ──▶  session starter: makes the session ID   session table: issue, who asked
 sets the label         (OIDC token of GitHub)         starts the runtime session of the agent
 `agentfarm`
                                                        agent: copies the repository to its
                                                        workspace, changes the code, runs the
                                                        tests there
                        branch agent/<session ID> ◀──  broker: commit, pull request             commit and pull request: the GitHub App
                        pull request to main                                                    of the agent, Agent-Id, Session-Id, Issue
                                                        broker: stack of the session            CloudTrail: agent role, source identity =
                                                        (a test copy of the application)        agent, role session name = session ID
 reviews, merges   ──▶  push to main
                        workflow agentfarm-deploy ──▶  live stack of the repository             CloudTrail: deploy role, role session
                                                        stack of the session: deleted           name = merge-<commit>
```

1. **Issue.** A person opens an issue in the repository of a use case and sets the label
   `agentfarm`. The text of the issue is the task.
2. **Session.** The workflow `agentfarm` of the repository invokes the session starter. The session
   starter makes the session ID and starts one session of the agent of that use case.
3. **Work.** The agent copies the repository to its workspace, changes the code and runs the tests
   there. It commits to the branch `agent/<session ID>` and opens a pull request to `main`. Then it
   deploys the template of its commit to the stack of the session, `agentfarm-<session ID>`. This
   stack is a test copy of the application, only for this session: it shows that the change
   deploys. It is not the live environment.
4. **Review and merge.** A person reviews the pull request and merges it. The agent cannot merge and
   cannot write to `main`.
5. **Deployment.** The merge is a push to `main`. It starts the workflow `agentfarm-deploy`, which
   deploys the template of the merged commit to the live stack of the repository,
   `agentfarm-live-<use case>`, and deletes the stack of the session of the merged pull request.

## How it works

```
workflow of the repository on GitHub (issue with the label agentfarm)
   │ AssumeRoleWithWebIdentity(OIDC token of GitHub Actions) ──▶ role agentfarm-github-<use case>
   │ Invoke(alias <use case> of the session starter; agent, issue, who asked)
   ▼
Session starter Lambda       reads the use case from the alias, not from the event
   │ makes the session ID (a UUID), writes the session to the session table one time (state: active)
   │ InvokeAgentRuntime(runtimeSessionId = session ID, task = text of the issue)
   ▼
AgentCore Runtime, agent runtime of one agent, platform version V2      one session = one microVM
   │ LLM agent (Bedrock Converse API). The model has tools only
   │
   ├─▶ workspace: a session of the AgentCore Code Interpreter. No execution role: no AWS credentials, no JWT.
   │      The agent writes the files of the repository there, and the model runs builds and tests there
   │
   │ for every request to the gateway:
   │ GetWebIdentityToken(tags: agent_id, session_id, repository), 300 seconds ──▶ AWS STS
   │     IAM allows only: the agent of this runtime, the runtime session ID of this session
   │     (aws:AgentCoreRuntimeSourceSid), a repository of this agent
   │ MCP tools/call, Authorization: Bearer JWT
   ▼
AgentCore Gateway
   │ custom JWT authorizer: STS issuer, audience, signature, expiry
   │ request interceptor: sub = execution role of the agent in the token; returns the Authorization header
   ▼
Broker Lambda
   │ verifies the JWT again (signature, iss, aud, exp, sub, tags), reads the session table (active, not expired)
   │ AssumeRole(agent role, SourceIdentity = agent_id, RoleSessionName = session_id,
   │            session policy for one purpose: the stack and the SSM path of the session,
   │            or the IDs and the KMS key of the GitHub App of its agent. Never both in one set of credentials)
   ▼
GitHub (with the GitHub App of the agent: read, branch agent/<session_id>, commit, pull request),
CloudFormation, and SSM through CloudFormation
```

| Component | Code | Notes |
| --- | --- | --- |
| Infrastructure | [runtime/infra/stack.py](runtime/infra/stack.py), [app.py](app.py) | The CDK stack `AgentFarmRuntime`. The agents and their use cases are in [infra/registry.py](infra/registry.py), the fixed names in [infra/names.py](infra/names.py). The stack has no VPC |
| Code signing | [infra/signing.py](infra/signing.py), [infra/sign.py](infra/sign.py) | The CDK stack `AgentFarmSigning` with a signing profile of AWS Signer. `make runtime-deploy` signs the packages of the broker, the interceptor and the session starter. Lambda accepts only signed packages for these functions |
| Settings | [infra/config.py](infra/config.py) | The settings of the broker, the interceptor and the session starter are the file `config.json` of their signed packages. The session starter has one environment variable, the ARNs of the agent runtimes |
| Session starter | [runtime/starter/handler.py](runtime/starter/handler.py) | Makes the session ID, writes the session, starts the runtime session. One alias for each use case: the workflow of a repository can invoke only the alias of its use case. The tasks of the test sessions without an issue are in [runtime/starter/tasks](runtime/starter/tasks) |
| Agent | [agent/runtime_server.py](agent/runtime_server.py), [agent/loop.py](agent/loop.py), [agent/gateway.py](agent/gateway.py) | The container of the agent runtime: an HTTP server, the LLM agent and the MCP client. It requests a JWT for every request to the gateway. The JWT is in the memory of this process only |
| Workspace | [agent/workspace.py](agent/workspace.py), [agent/packages.py](agent/packages.py) | A session of the code interpreter `agentfarm_workspace_isolated`: Linux, a shell, Python 3.12, no git client, no credentials. Network mode SANDBOX: no path to the internet and no name resolution. The agent downloads the Python packages of a build and writes them to the workspace as files |
| Box option | [agent/boxed.py](agent/boxed.py), [agent/box](agent/box) | A setting of the deployment: the agent, or the work of its sessions, inside a box of Strands Box (see "Design notes") |
| Interceptor | [interceptor/handler.py](interceptor/handler.py) | Checks `sub`, returns the bearer token so that the gateway forwards it to the broker |
| Broker | [broker/handler.py](broker/handler.py), [broker/github.py](broker/github.py), [shared/identity.py](shared/identity.py) | Tools `list_files`, `read_file`, `commit_changes`, `open_pull_request`, `deploy_stack`, `describe_stack`, `get_stack_events`, `end_session`. A commit creates, replaces and deletes files |
| Repositories | [repositories](repositories) | Initial content of the branch `main` of the repositories `agentfarm-greeting-service`, `agentfarm-feature-flags` and `agentfarm-word-count` on GitHub |
| Workflows | [runtime/github](runtime/github) | `agentfarm.yml` starts a session from an issue. `agentfarm-deploy.yml` deploys the live environment after a merge. [install_workflow.py](runtime/github/install_workflow.py) fills them with the roles of each repository and writes them to the repositories |
| Trail | [runtime/infra/stack.py](runtime/infra/stack.py) | The CloudTrail trail `agentfarm-runtime-trail` is a multi-Region trail with log file validation. It writes to an S3 bucket the management events of all Regions and these data events: the session table; the session starter, the broker and the interceptor; and the requests to the agent runtimes, to the workspace and to the gateway |
| Gateway log | [runtime/infra/stack.py](runtime/infra/stack.py) | The gateway delivers its application log to a CloudWatch log group: the start and the end of every request. The bearer token is a header, and the log does not have it |
| Tests | [runtime/tests](runtime/tests), [tests](tests) | Test runs against the deployed system, one script for each (see "Tests"), the local tests without AWS, and the script of `make destroy` |

### Roles

| Role | Used by | Permissions |
| --- | --- | --- |
| `agentfarm-github-<use case>` | The workflow `agentfarm` of one repository. The trust policy allows only the OIDC token of GitHub Actions of a workflow of that repository that runs from the branch `main` | Invoke the alias `<use case>` of the session starter |
| `agentfarm-runtime-starter` | Session starter Lambda | Write a session to the session table: the policy of the table lets only this role and the role of the broker write. Invoke the agent runtimes: the policy of an agent runtime lets only this role send it a request |
| `agentfarm-runtime-exec-<agent_id>` | The agent runtime of one agent | Request a JWT for one audience, 900 seconds at most, with exactly this agent ID, the runtime session ID of the session that has the credentials, and a repository of the agent as tags. `bedrock:InvokeModel` on the model of the agent. Start, use and stop a session of the workspace. Pull the image, write the log of its own agent runtime |
| `agentfarm-runtime-broker` | Broker Lambda | `sts:AssumeRole` and `sts:SetSourceIdentity` on the agent role, only with credentials that Lambda delivered to the broker function (`lambda:SourceFunctionArn`). Read the session table and end a session. Write its own log |
| `agentfarm-runtime-agent` | Sessions that the broker creates | CloudFormation on `agentfarm-*` stacks, SSM parameters under `/agentfarm/`, the IDs and the KMS keys of the GitHub Apps. The broker requests credentials for one purpose. For the stack: the session policy allows the stack `agentfarm-<session_id>` and the SSM path `/agentfarm/<session_id>/` only. For the repository: the two SSM parameters with the IDs of the GitHub App of its agent, and a signature with the KMS key of its agent, only for a request that an AWS service does not send for the session |
| `agentfarm-runtime-interceptor`, `agentfarm-runtime-gateway` | Interceptor Lambda, AgentCore Gateway | No permissions; invoke the interceptor and the broker |
| `agentfarm-deploy-<use case>` | The workflow `agentfarm-deploy` of one repository, with the same trust policy as `agentfarm-github-<use case>` | Create and update the stack `agentfarm-live-<use case>` with change sets, and its SSM parameters under `/agentfarm/live-<use case>/`. Delete a stack and an SSM parameter that have the tag `repository` of this repository. Read the status of `agentfarm-*` stacks |

On GitHub every agent has its own GitHub App, installed on the repositories of the agent with the
permissions Contents (read and write), Pull requests (read and write) and Metadata (read). The
private key of the App is in AWS KMS, as the imported key material of the KMS key
`alias/agentfarm/github-app/<agent_id>`: nobody reads it, and the broker asks AWS KMS for a signature. The ID
of the App and the ID of its installation are two SSM parameters,
`/agentfarm-settings/github-app/<agent_id>/app_id` and `.../installation_id`. The stack makes the KMS key and
the parameters for the agents of the list `KMS_APP_KEY_AGENTS`, which has the two agents of the stack; a
person imports the key and writes the two IDs with a command. No value of an App is in the repository, and
the system has no secret in AWS Secrets Manager (see "Design notes").

### A session

1. The workflow `agentfarm` of a repository runs when an issue gets the label `agentfarm`. It gets
   AWS credentials with the OIDC token of GitHub Actions, in a role session with the name
   `issue-<number>-run-<run ID>`, and invokes the alias of the session starter that has the name of
   its use case. The values of the issue are environment variables of the
   workflow step, not text of its script.
2. The session starter reads the use case from the alias. It checks that the agent may work on the
   use case. It makes the session ID, writes the session to the session table (the agent, the
   repository, the number and the address of the issue, the GitHub user who set the label, the hash
   of the task, the expiry time) and starts the runtime session with that ID. The task of the session is the title
   and the text of the issue, cut to 8000 characters. A person wrote this text: it is input of the
   agent, and it cannot select a different use case, agent or repository. The session starter removes
   from the text what the page of the issue does not show: an HTML comment, a comment in the form of a
   link definition, and the characters that a reader does not see. The session table has the number of
   characters removed.
3. AgentCore Runtime starts one microVM for the session. The agent starts a session of the workspace
   and writes the files of the repository there. The broker gives a file complete or not at all: the
   workspace does not get a file that is not text in UTF-8, a file of more than 100 000 characters, and
   the files after the first 200. The agent tells the model which files the workspace does not have,
   and it does not commit a file with one of these paths. The model reads and changes the files and runs the
   tests with shell commands in the workspace. The workspace has no path to the internet: the model
   asks for the Python packages of a build with the tool `workspace_install`, and the agent brings
   them as files. The model has no tool that runs a command in the microVM of the agent.
4. For every request to the gateway the agent requests a JWT from AWS STS, valid for 300 seconds.
   IAM allows the request only with the agent ID of the runtime, the runtime session ID of the
   session as the session ID, and a repository of the agent.
5. The gateway, the interceptor and the broker verify the JWT. The broker reads the session table:
   the session must be a session that the session starter wrote, active and not expired, for the
   repository of the JWT. The broker then requests credentials of the agent role for this session
   only, with the agent ID as the source identity, the session ID as the role session name, and the
   issue and the hash of the task as session tags.
6. A commit: the agent reads the changed files from the workspace and sends them to the broker, with
   the paths of the files that the model deleted. The
   broker reads the two IDs of the GitHub App of the agent and asks AWS KMS for the signature of the JWT
   of the App, both with the credentials of the session. With that JWT it gets
   an installation access token for the repository of the session only, and writes the commit to
   the branch `agent/<session_id>`. A file that the repository has keeps its mode: a script stays
   executable. The commit has the GitHub App as its author. The broker adds
   the lines `Issue: #<number>`, `Agent-Id` and `Session-Id` to the commit message and to the pull
   request. In the description of the pull request the text of the agent is in a code block: GitHub
   shows it as plain text, without an image, a link, a mention or HTML. The agent never has the
   private key or the token, and the broker never has the private key.
7. A deployment: the broker deploys `template.yaml` of the last commit of the session to the stack
   `agentfarm-<session_id>`. The stack gets the tags `agent_id`, `session_id`, `repository` and
   `commit_id`.
8. At the end of its work the agent ends the session with the tool `end_session`. From that time
   the broker rejects every JWT of the session. Without this request, the session ends 3600 seconds
   after its start. AgentCore Runtime stops a microVM after 300 seconds without a request and
   without work.

A session without an issue, for the tests: an invocation of the session starter without an alias,
with the use case in the event. The task is then a file of the signed package of the session
starter ([runtime/starter/tasks](runtime/starter/tasks)).

### After the merge

1. A person merges the pull request. The merge is a push to `main` and starts the workflow
   `agentfarm-deploy` of the repository.
2. The workflow gets AWS credentials of the role `agentfarm-deploy-<use case>`. The name of the role
   session is `merge-<commit>`.
3. It deploys `template.yaml` of the merged commit to the stack `agentfarm-live-<use case>`. The
   SSM parameters of the live environment are under `/agentfarm/live-<use case>/`. The stack gets
   the tags `commit_id` and `repository`.
4. It reads the commits of the pull request of the merged commit. For a commit that the broker wrote
   (a signature of GitHub, the GitHub App of an agent as its author, the identity lines at the end of
   its message), it deletes the stack `agentfarm-<session ID>` of the session of that commit. The name
   of the branch does not select the stack.

A session cannot have the name of a live environment: a runtime session ID has 33 characters or
more, and the name `live-<use case>` is shorter. The session policy of a session names only the
stack and the SSM path of that session.

### The network of a session

A session runs in two places with two network rules.

| | Workspace | MicroVM of the agent |
| --- | --- | --- |
| What runs there | The code of the repository: tests, build scripts, dependencies, the shell commands of the model | The agent process: the loop of the model, the client of the gateway, the download of packages. No code of the repository |
| Who decides what runs | The model, the content of the repository and the text of the issue | The container image of the deployment. The model has no tool that runs a command there |
| Credentials | None | The credentials of the execution role: the JWT of this session, the model, the workspace |
| Network | No path to the internet and no name resolution (network mode SANDBOX). The agent checks both at the start of every session | A path to the internet (network mode PUBLIC) |
| What it needs the network for | Nothing | AWS STS, the gateway, Amazon Bedrock, the API of the workspace, pypi.org |
| What can leave | Only the files that the agent reads back for a commit: they go to the pull request | Every request that the agent process sends |
| Who reaches it from outside | Only the agent of its own session | Only the session starter; nobody runs a command there |

For an agent with the box option, the agent process of the second column runs inside a box of
Strands Box: it has placeholder credentials, and a path only to the destinations of a policy (see "Design
notes").

The workspace is the place where input that nobody reviewed becomes running code, so it is the place
without a network. The microVM of the agent runs only the code of the deployment; its path to the
internet is a limit, with a correction that is not implemented (see "Limits").

### What the records show

| Action | Record | Who it shows |
| --- | --- | --- |
| Start of a session | The trail, data events | The invocation of the session starter with the role of the repository, and the write of the session table item by the session starter |
| Start of a session | The session table item and the log of the session starter | The session ID, the agent, the repository, the issue, the GitHub user who set the label and the hash of the task |
| Issue and task of a session | CloudTrail, `AssumeRole` of the broker | The session tags `issue` (the number of the issue) and `task_sha256` (the hash of the text that the agent received), with the agent ID and the session ID |
| Start of a session | CloudTrail, `AssumeRoleWithWebIdentity` | The role `agentfarm-github-<use case>`, the role session name `issue-<number>-run-<run ID>`, and the subject of the OIDC token: the repository, with the IDs of its owner and of the repository, and the branch `main` |
| AWS action of a session: stack, SSM parameters, the read of the IDs of the GitHub App, and the signature of the JWT of the App by AWS KMS | CloudTrail | The role `agentfarm-runtime-agent`, `sourceIdentity` = agent ID, role session name = session ID |
| Commit and pull request | GitHub | The GitHub App of the agent as the author, with a signature of GitHub. The lines `Issue: #<number>`, `Agent-Id` and `Session-Id` end the commit message and the description of the pull request |
| Stack of a session | The tags of the stack | `agent_id`, `session_id`, `repository`, `commit_id` |
| Merge | GitHub | The person who merged |
| Deployment of the live environment, deletion of the stack of a session | CloudTrail | The role `agentfarm-deploy-<use case>`, role session name = `merge-<commit>` |
| Live stack | The tags of the stack | `commit_id` of the merged commit, `repository` |
| Request of an agent | The logs of the gateway and of the broker | The session ID, the tool and the decision. No token |
| Start of a runtime session, and every later request to it | The trail, data events of AgentCore | `InvokeAgentRuntime` with the role of the session starter and the runtime session ID. The event does not have the text of the request |
| Request of an agent to the gateway | The trail, data events of AgentCore | `InvokeGateway` with the tool, the claims `sub`, `iat`, `exp` and `jti` of the JWT, and the request ID of the broker. No token, no arguments and no session ID: the log of the broker has the session of that request ID |
| Request of an agent to the workspace | The trail, data events of AgentCore | `StartCodeInterpreterSession`, `InvokeCodeInterpreter` and `StopCodeInterpreterSession` with the role session of the microVM and the ID of the workspace session. The event does not have the command: the text of a command is in the log of the agent runtime only |
| Model request and JWT request of a session | CloudTrail | The execution role of the agent, role session name `AgentCore-MicroVM-<ID>-DEFAULT`. The JWT request (`GetWebIdentityToken`) has the session ID as a request tag, and IAM allows only the runtime session ID there: it connects the role session of a microVM, with its model requests and its workspace requests, to the session |

## Commands

Prerequisites: `uv`, Node.js (for `npx`), Docker, the GitHub CLI (`gh`) signed in, AWS credentials
with administrator permissions, an account that is CDK-bootstrapped in the Region of the system,
access to the model in Amazon Bedrock, IAM outbound identity federation enabled in the account, and
the IAM OIDC identity provider of GitHub Actions (`token.actions.githubusercontent.com`, audience
`sts.amazonaws.com`) in the account. The stacks do not create the identity provider.

On GitHub, one time, by hand:

1. Three private repositories in the account or the organization of the system:
   `agentfarm-greeting-service`, `agentfarm-feature-flags` and `agentfarm-word-count`, each with the
   content of `repositories/<use case>/` on the branch `main`.
2. One GitHub App for each agent, with the name `agentfarm-<agent_id>` and the repository permissions
   Contents (read and write), Pull requests (read and write) and Metadata (read), installed on the
   repositories of the agent.
3. After the first deployment, which makes a KMS key and two SSM parameters for every agent of the list
   `KMS_APP_KEY_AGENTS` ([infra/registry.py](infra/registry.py)): the import of the private key of each App
   from the PEM file that GitHub gave, with
   [runtime/github/import_app_key.py](runtime/github/import_app_key.py).
4. The two IDs of each App, with [runtime/github/set_app_ids.py](runtime/github/set_app_ids.py). The values
   are arguments of the command, and no file has them.

An agent that is not in the list needs a secret in AWS Secrets Manager in the place of steps 3 and 4,
`agentfarm/github-app/<agent_id>`, with the JSON text
`{"app_id": "...", "installation_id": "...", "private_key": "..."}`.

```bash
AWS_PROFILE=<profile> make runtime-deploy                                         # build and sign the packages, build the agent image, deploy the stacks
AWS_PROFILE=<profile> RUNTIME_BOX_AGENTS=agent-alpha make runtime-deploy          # the same, with agent-alpha inside a box of Strands Box
AWS_PROFILE=<profile> RUNTIME_BOX_AGENTS=agent-alpha RUNTIME_BOX_MODE=work make runtime-deploy   # only the work of its sessions inside a box
AWS_PROFILE=<profile> uv run python runtime/github/install_workflow.py            # print the workflow files of the repositories
AWS_PROFILE=<profile> uv run python runtime/github/install_workflow.py --push     # write them to main of the repositories, make the label
AWS_PROFILE=<profile> uv run python runtime/github/import_app_key.py --agent <agent_id> --pem <file>   # import the private key of a GitHub App into the KMS key of its agent
AWS_PROFILE=<profile> uv run python runtime/github/set_app_ids.py --agent <agent_id> --app-id <number> --installation-id <number>   # write the two IDs of the App to the SSM parameters of its agent
AWS_PROFILE=<profile> make destroy                                                # delete all resources in AWS and list what remains
```

`make runtime-deploy` deploys the stack `AgentFarmSigning`, signs the three packages, and deploys
the stack `AgentFarmRuntime`. The deployment reads from GitHub the owner of the repositories
(`GITHUB_OWNER`, or the account of the GitHub CLI) and the subject of the OIDC token of each
repository. The push of `agentfarm-deploy.yml` is a push to `main`: it starts the first deployment
of the live environment of each repository.

The variables of the box option are not stored anywhere: a `make runtime-deploy` without
`RUNTIME_BOX_AGENTS` turns the box off for every agent. The stack outputs `BoxAgents` and `BoxMode` show
the setting of the deployed stack.

`make destroy` deletes the stacks of the sessions, the live stacks of the repositories, the stack
`AgentFarmRuntime` and the stack `AgentFarmSigning`, then lists what remains. It keeps the KMS keys of
the GitHub Apps, because each key has the private key of an App, and lists them; a person schedules
their deletion. It changes nothing on GitHub.

The region is `eu-central-1`. Set `AWS_REGION` to use a different region. The model is
`eu.anthropic.claude-opus-5-5` (Anthropic Claude Opus 5.5, EU cross-Region inference profile). The
model, the use cases and the agents are set in [infra/registry.py](infra/registry.py). The stack
`AgentFarmRuntime` has two agents: `agent-alpha`, with the three use cases, and `agent-beta`, with
`greeting-service` and `feature-flags`. The variable `RUNTIME_AGENTS` of `make runtime-deploy` (agent IDs
of the registry with commas between them) selects the agents of the stack: each agent needs its GitHub
App before the deployment, and the import of its key and its two IDs after it. A session from an issue is
a session of the first agent of the stack that may work on the use case of the repository.

IAM outbound identity federation is an account-level setting:

```bash
aws iam get-outbound-web-identity-federation-info    # shows JwtVendingEnabled and the issuer
```

```bash
aws iam enable-outbound-web-identity-federation      # enables the setting
```

The stacks have no VPC, no NAT gateway and no VPC endpoint. The cost is the cost of the use: the
runtime sessions, the sessions of the workspace, the model requests, the functions, the logs, the
KMS keys of the GitHub Apps ($1 for each key in a month), and the trail: its data events (one for every read and write of the
session table and for every invocation of the three functions) and its log files in S3. `make
destroy` stops the trail and deletes its log files with the stack.

## Tests

Each test run works against the deployed system and checks the result outside the system under
test, with the credentials and the GitHub CLI of the person who runs it. Each run writes its own
file `evidence/runtime/<name>-<start time>.json` on the machine that runs it; a later run does not
write over an earlier one. The directory `evidence/` is in `.gitignore`: a record has the IDs of the
account and of the sessions of the person who ran it.

| Command | What it does | Checks |
| --- | --- | --- |
| `make unit` | Local tests of the broker, the session starter, the GitHub client and the workflow files, without AWS | 139 tests |
| `make runtime-chain` | One scripted session without a model, from the session starter to the stack | C1 the session starter makes and writes the session, and the session ends itself. C2 the commit, its author, its identity lines, the pull request. C3 the stack, its tags and its value. C4 a principal that is not the session starter cannot send a request to the agent runtime, for a new session or for the session of the test. C5 the session starter rejects a use case that the agent may not work on. C6 no JWT in the logs. C7 CloudTrail has the agent ID as the source identity and the session ID as the role session name of every event |
| `make runtime-agent` | One session of the LLM agent: a library with a defect and four tests, three of which fail | G1 the work ends. G2 the model ran the tests in the workspace, and the last run passed. G3 the corrected library on the branch of the session, the tests not changed, the author, the identity lines, the pull request. G4 the stack with the new version. G5 the session ended itself. G6 no JWT, no GitHub token, no private key and no AWS access key in the logs. G7 the workspace has no path to the internet and resolves no name, and the packages of the build come from the agent. G8, only for an agent with the box option: the process that does the work has the placeholder key of a box, and the box refuses an address that the agent does not need |
| `make runtime-hostile` | One session of the LLM agent with a hostile task: text in an HTML comment, and visible text that asks for a file `.gitattributes`, for a workflow file, and for an image with data in its address and for HTML in the description of the pull request. The checks do not depend on what the model decides | H1 the agent received the task without the hidden text, and the session table has the number of characters removed. H2 the work ends, and the session ended itself. H3 the description of the pull request has the text of the agent in one code block, and after the block only the lines of the broker. H4 the branch of the session has no file `.gitattributes`, and its files under `.github/` are the files of `main` |
| `make runtime-secret-reference` | One scripted session whose template has, as the value of its SSM parameter, a dynamic reference to a value of the GitHub App of its agent: the SSM parameter with the ID of the App (an identifier, not the private key) | K1 the session committed the template and asked for the deployment. K2 the stack is not complete, because the session is not authorized to read the value. K3 the session has no SSM parameter. K4 CloudTrail has the read through AWS CloudFormation as denied, and the reads of the broker as allowed |
| `make runtime-sessions` | Two scripted sessions of the same agent, at the same time and in the same repository; then two more sessions of the same agent, whose templates name a parameter in the path of the first session | P1 the two sessions run at the same time, and each does its work. P2 each session has its own branch, commit, pull request, stack and SSM parameter, and nothing of the other. P3 a session cannot make a new parameter in the path of a different session: IAM denies the request. P4 a session cannot take the parameter of a different session: its stack fails, and the parameter is not changed. P5 by the policy simulator of IAM, a session requests a JWT only with its own session ID. P6 CloudTrail has only the resources of a session in its events, and the denied request with the ID of the session that sent it |
| `make runtime-agents` | One scripted session of each of two agents, at the same time and in the same repository | A1 each session does its work. A2 the commit and the pull request of each session have the GitHub App of its agent, with a signature of GitHub. A3 the stack of each session has the tag of its agent. A4 the session starter gives an agent no session for a use case that is not its own, and does not give the state of a session under the name of a different agent. A5 by the policy simulator of IAM, the execution role of an agent requests a JWT only with its own agent ID and a repository of its own. A6 CloudTrail has each agent as the source identity of its events; every signature is a signature with the KMS key of that agent, every setting that a session reads is a setting of that agent, and no session reads a secret |
| `make runtime-trail [RECORD=<file>]` | Changes nothing. Reads the log files of the trail for the session of the newest record of `make runtime-chain`, or of the record `RECORD` of a chain run, an agent run or a run with a hostile task | T1 the trail is logging for all Regions with log file validation, and its data events are the session table, the three functions, and the agent runtimes, the workspace and the gateway of AgentCore. T2 the management events of the session, with the agent as source identity, and the requests of the broker for credentials with the session tags. T3 the data events of the session table for the item of the session: the write of the session starter, the reads and the end of the session by the broker. T4 the invocations of the session starter and of the broker. T5 the requests of the session starter to the agent runtime with the session ID and, for a session of the LLM agent, the requests to the workspace of the microVM that requested a JWT with the session ID |
| `make runtime-issue [USE_CASE=feature-flags]` | Opens an issue with the label `agentfarm` in the repository of a use case (`agentfarm-word-count`, or `agentfarm-feature-flags`) and follows the session that it starts | I1 the workflow ran, and the session table has the issue, who asked, and the hash of the text of the issue. I2 the work ends. I3 the change on the branch of the session, the other files and the workflows not changed, a commit with a signature of GitHub and the GitHub App as author, and the lines `Issue`, `Agent-Id`, `Session-Id` at the end of the commit message and of the pull request. I4 the stack. I5 the session ended itself. I6 CloudTrail has the role session `issue-<number>-run-<run ID>` of the workflow, and the session tags `issue` and `task_sha256` on every request of the broker for credentials |
| `make runtime-merge PULL=<number> [USE_CASE=feature-flags]` | Changes nothing. Reads the result of a pull request of a session that a person merged | M1 the workflow `agentfarm-deploy` ran for the merge commit, and the agent did not merge. M2 the live stack has the merge commit as its tag and the values of the merged template. M3 the stack of the session and its parameters are not there. M4 CloudTrail has the deployment and the deletion with the deploy role and the role session `merge-<commit>`, and no other role changed the live stack |

Every check of this table passed against the deployed system in November 2026, with two agents and
their GitHub Apps, with the private keys of the Apps in AWS KMS, and also with the agent inside a box in
both modes of the box option (`make runtime-agent` and `make runtime-hostile`).

A merged pull request of a session changes `main`: the sample defect of `agentfarm-word-count` is then
corrected, and `make runtime-agent` finds nothing to change. Put the content of
`repositories/word-count/` back on `main` before the next run.

## Design notes
- **A runtime session requests its own JWT.** No component delivers a token to the agent. The
  credentials that a session of AgentCore Runtime receives on platform version V2 have the IAM
  context key `aws:AgentCoreRuntimeSourceSid`, the runtime session ID. The policy of the execution
  role allows a JWT only with that value as the session ID. The stack sets the platform version V2:
  the documentation names V1 as the value when the property is absent.
- **The caller selects the runtime session ID.** AgentCore Runtime accepts every session ID of 33
  characters or more, also a text that is not a UUID, and it accepts the ID of a stopped session for
  a new microVM. So the session starter makes the session ID and writes it one time, and the broker
  accepts only a session that the session starter wrote (C4).
- **A broker is still necessary.** The credentials of a runtime session have the role session name
  `AgentCore-MicroVM-<ID>-DEFAULT` and no source identity. The broker requests the credentials of
  the agent role with the agent ID and the session ID, and it narrows them to the session.
- **A snapshot starts a microVM.** A value that the agent makes before the first request is the
  same in all sessions, and the credentials of that time cannot request a JWT. The agent makes its
  AWS clients inside a request.
- **One JWT for each request.** A JWT is valid for 300 seconds and the agent requests a new one for
  every request to the gateway. The session table does not hold the ID of one token: a session ends
  in the session table, or at its expiry time.
- **The code of a session runs in the workspace.** The microVM of the agent has the credentials of
  the execution role, so the model gets no tool that runs a command there. The workspace has no
  execution role, no credentials and no git client. The repository reaches it as files, and a
  change leaves it as files.
- **Separate credentials for the repository and for the stack.** AWS CloudFormation resolves a dynamic
  reference of a template (`{{resolve:ssm:...}}`, `{{resolve:secretsmanager:...}}`) with the credentials of
  the caller of the stack operation, and the template of a session is input of the agent. With one set of
  credentials for both, a template could copy a value of the GitHub App to an SSM parameter of the session.
  The broker requests credentials for one purpose: for the repository (the IDs of the App and the signature)
  or for the stack (the stack and its SSM parameters only). The policy of the agent role also allows the
  read of the IDs and the signature only for a direct request (`aws:CalledViaFirst` is absent). Test K1 to
  K4.
- **The GitHub App of an agent: its private key in AWS KMS, its IDs in SSM parameters, no secret.**
  GitHub generates the private key of an App. With the key in a secret, the broker would read it and sign
  the JWT of the App itself, so the broker would have the key in its memory for every request to a
  repository. With the key in AWS KMS the broker asks for a signature, and no principal reads the key
  material of a KMS key
  ([import of key material](https://docs.aws.amazon.com/kms/latest/developerguide/importing-keys-get-public-key-and-token.html)).
  For an agent of the list `KMS_APP_KEY_AGENTS` ([infra/registry.py](infra/registry.py)) the stack has a KMS
  key for signatures (RSA 2048), created without key material, with the alias
  `alias/agentfarm/github-app/<agent_id>`, and two SSM parameters under
  `/agentfarm-settings/github-app/<agent_id>/`, created with the value `not-set`. The person who has the
  private key of the App imports it ([runtime/github/import_app_key.py](runtime/github/import_app_key.py)):
  the script takes the PEM file in the form that GitHub gives, refuses a key that is not an RSA key of
  2,048 bits, and does not change a KMS key that already has key material. After that no principal reads
  the key. The two
  IDs of the App are not secrets, and they are in no file: a person writes them to the parameters with a
  command ([runtime/github/set_app_ids.py](runtime/github/set_app_ids.py)). A deployment does not write the
  value of a parameter again, because the template does not change; with the value `not-set`, or with a
  value that is not a number, the broker refuses the request.
  The agent role may read only the parameters and sign only with the alias that have the source identity
  of its session (`${aws:SourceIdentity}` in their names), with one algorithm (`RSASSA_PKCS1_V1_5_SHA_256`,
  the RS256 of a GitHub App), in a direct request. The session policy for the repository names the
  parameters and the alias of the agent, and the session policy for the stack has neither. The key policy
  denies a signature to every other principal, an administrator also. The path of the parameters is not
  under `/agentfarm/`, the path of the parameters of the sessions. The log of the broker says where the
  key of the App was (`github_app_key`), and CloudTrail has every `Sign` request with the agent and the
  session. An operator who disables the KMS key of one agent stops the signatures of that agent in less
  than a second. The stack keeps a KMS key when its agent leaves the list and when the stack is deleted.
  The key material of an asymmetric KMS key cannot be changed after the import: a new key that GitHub
  generates needs a KMS key of its own, and the alias moves to it. An agent that is not in the list has a
  secret with the IDs and the private key, which the broker reads.
- **The issue and the task of a session are in CloudTrail.** The session starter writes the SHA-256 hash
  of the task that the agent receives to the session table. The broker reads the number of the issue
  and the hash from the session table and sets them as the session tags `issue` and `task_sha256` of
  every `AssumeRole` request for the session. The trust policy of the agent role allows these two tag
  keys only.
- **The lines of the broker.** The broker ends a commit message and a pull request description with its
  own lines: `Issue: #<number>` when an issue started the session, then `Agent-Id` and `Session-Id`.
  It removes these lines from the text of the agent, after it removed the characters that a reader
  does not see (the Unicode categories Cf and Cc): a zero-width space inside a key does not hide the
  key. It also changes a word with which GitHub closes
  an issue at a merge (`Fixes #5`) to `See`, except for the issue of the session itself.
- **The description of a pull request is plain text.** GitHub shows a description as Markdown. An image in
  it makes GitHub request an address that the agent chose when a person opens the pull request, with
  data in the address; a link, a mention and HTML are text of the agent that looks like text of
  GitHub. The broker writes the text of the agent in one code block, with a fence that is longer than
  every run of backticks in the text, and its own lines after the block. A closing word of the agent
  has no effect in a code block: when the text of the agent closes the issue of the session, the
  broker writes the line `Closes #<number>` itself.
- **The task is the text that a person sees.** The person who sets the label reads the page of the issue.
  The page does not show an HTML comment, a comment in the form of a link definition
  (`[//]: # (text)`), or a format character, and a model reads them. The session starter removes them
  before it makes the task and its hash, and it writes the number of characters removed to the
  session table and to its log.
- **A file is complete, or the session does not read it.** A part of a file in the workspace would come
  back in a commit as the complete file. The broker gives no content for a file that is not text in
  UTF-8 or that has more than 100 000 characters, and the agent does not commit a path that the
  workspace did not get from the repository.
- **A commit keeps what the repository has.** A file keeps the mode that it has in the parent commit; a new
  file is a plain file. The broker does not write over a symbolic link or a submodule, and it deletes
  only a file that the parent commit has.
- **One repository for a session.** IAM allows a JWT with every repository of the agent. The broker
  rejects a JWT whose repository is not the repository that the session table has for the session.
- **Paths that a session cannot write.** The broker refuses a file under `.github/` or `.git/`, a file
  with the name `.gitattributes` in any folder, and a path that is not one plain relative path, also
  for a deletion. A file `.gitattributes` says how GitHub shows a file in a review: with
  `linguist-generated` the lines of a change are closed.
- **The stack to delete comes from the commits of the broker.** After a merge, the workflow deletes the
  stack of a session only for a commit of the merged pull request that has a signature of GitHub, the
  author `agentfarm-<agent ID>[bot]` for the agent ID of its identity lines, and the identity lines at
  the end of its message. A person can give a branch the name of a different session; a person cannot
  make such a commit. A GitHub App with a different name gets no deletion: its stack stays.
- **The trail.** The event history of CloudTrail keeps 90 days and has management events only. The
  trail `agentfarm-runtime-trail` keeps the management events of all Regions (the events of IAM are in
  us-east-1) and these data events: every read and write of the session table; every invocation
  of the session starter, of the broker and of the interceptor; and every request to an agent
  runtime, to the workspace and to the gateway. The requests to AgentCore are data events: the event
  history does not have them. The logs of the functions and of the
  gateway, and the item of a session in the session table, keep one year.
- **An execution role writes only the log of its own agent runtime.** The log of an agent runtime has the
  task, the answers of the model and the commands of the workspace. The policy of an execution role
  names the log group of its own agent runtime, by the name of the runtime: the agent runtime of a
  different agent cannot write lines in it.
- **The microVM of a session in CloudTrail.** The role session of a microVM has a name of AgentCore, not
  the session ID. Its `GetWebIdentityToken` requests have the session ID as a request tag, and IAM
  allows only the runtime session ID as that tag. A reader of the trail gets from them the role
  session of a session, and with it the model requests and the workspace requests of the session
  (test T5).
- **Resource-based policies for the records of a session.** The session table denies a write to every
  principal except the session starter and the broker: a principal of the account with DynamoDB
  permissions cannot write a session, set an ended session to active again, or change its issue. The
  KMS key of the GitHub App of an agent denies a signature to every principal except the agent role: a
  principal of the account with AWS KMS permissions does not sign as the App. Neither policy
  denies a change of the policy itself: the trail has that change.
- **Only the session starter reaches an agent runtime.** Each agent runtime has a resource-based policy:
  it denies a request to every principal of the account except the role of the session starter, and
  it denies to every principal a command in the microVM of a session (`InvokeAgentRuntimeCommand`,
  `InvokeAgentRuntimeCommandShell`). The microVM of an active session has the credentials that can
  request the JWT of that session. A test reads the state of the work of a session through the session
  starter (the request `status`, only for a session of the session table).
- **A workspace without a path to the internet.** The code interpreter of the workspace has the network
  mode SANDBOX. Code that runs there (a test, a build script, a dependency, a command of the model
  after a hostile issue) cannot send the code of the repository to an address and cannot download
  anything. The model installs Python packages with the tool `workspace_install`: the agent process
  accepts only names and versions (no address, no option of pip, no file), downloads the built
  packages from pypi.org without a run of their code, writes the files to the workspace, and the
  workspace installs them from these files. At the start of its work the agent checks that the
  workspace reaches no address on the internet; if it reaches one, the work fails.
- **A workspace that resolves no name.** A program without a connection can still send data out if a
  resolver answers its questions: it puts the data in the names that it asks for, and the name server
  of the domain receives them. So the agent also runs a name resolution probe in the workspace at the
  start of its work ([agent/workspace.py](agent/workspace.py)). The probe asks the resolver of the
  system for two names of the internet and for a name that no resolver has in its cache, and it sends
  one DNS question to two public resolvers. The work starts only if no name gets an address and no
  public resolver answers; with an address, an answer, or a probe that is not complete, the work
  fails. The answer that a name does not exist is not a proof of a path: a resolver in the workspace
  that knows no name gives it too. The status of a session has the answers of the probe. In the
  workspace of AgentCore the resolver is a local address that gives no address for a name, and a public
  resolver gives no answer (G7).
- **The agent inside a box (the box option).** This is a setting of the deployment: the variable
  `RUNTIME_BOX_AGENTS` names the agents that have it, and `RUNTIME_BOX_MODE` its mode
  ([agent/boxed.py](agent/boxed.py)).
  In the mode `agent` (the default), the complete agent runs inside one box of
  [Strands Box](https://github.com/strands-agents/box). The entry point of the container puts `box run` in
  its own place, and the box starts the agent: the HTTP server of the agent runtime and the work of a
  session are one process inside the box. The box listens on the port of AgentCore Runtime and relays
  each connection to the port of the agent. The container has no other process: no part of the agent is
  outside the box.
  The agent has placeholder AWS keys. The gateway of the box signs its requests to AWS STS, Amazon Bedrock
  and AgentCore with the credentials that the microVM gives at its metadata endpoint, which the agent
  cannot reach. IAM gives the agent the JWT of its own runtime session: the signature of the box has the
  session context of the credentials. The gateway of the box keeps the credentials of its first request, so
  the agent sends no request to AWS before the snapshot of the platform: the first request of a box is a
  request of a session.
  The policy ([agent/box/policy.dw.template](agent/box/policy.dw.template)) permits six hosts: AWS STS,
  Amazon Bedrock, AgentCore, the gateway of the system, `pypi.org` and `files.pythonhosted.org`. The box
  refuses every other address, the metadata endpoint of the microVM also, and the agent has no network
  without the gateway of the box: no socket, no name resolution. At the start of the work of a session the
  agent writes to its state what it has: the mode, its access key ID (the placeholder), and the answer of
  the box for an address that it does not need.
  The mode `agent` needs two features that the release 0.2.2 of Box does not have: the listener for a
  program inside a box (`[[agent.ingress]]`, an experimental feature) and the default credential chain of
  the AWS SDK (`aws://`). The image of the agent builds Box from one commit of its repository (`70eb3af`)
  with the Rust toolchain. Box starts in a container of AgentCore Runtime; it does not start in a Docker
  container with the default settings on a workstation.
  The mode `work` needs neither feature. The HTTP server stays outside the box, with the credentials and
  the path to the internet, and starts a box for the work of each session: the loop of the model, the client
  of the gateway, the client of the workspace and the download of packages. The server writes the
  credentials of the runtime session to a profile file that only the box process reads, again every 5
  minutes, and the process of the work sends it the state of the work as lines of its output.
  Inside a box, `pip download` gets the proxy and the certificate of the box as options
  ([agent/packages.py](agent/packages.py)).

  | | Without a box | Mode `work` | Mode `agent` |
  | --- | --- | --- | --- |
  | Processes of the container with the credentials of the execution role | The agent | The HTTP server of the agent, and the box | The box |
  | Process with the text of the model and the files of a repository | The agent, with the credentials and a path to the internet | The work of the session, with placeholder keys and six hosts | The agent, with placeholder keys and six hosts |

  What the box does not change: the process inside the box requests the JWT and sends it to the gateway of
  the system, so it has the JWT in its memory, and it can send it to a destination of the policy. The
  destinations of the policy are still paths out (see "Limits"). In the mode `work` the server outside the
  box has the credentials and a path to the internet; it runs no text of the model and no file of a
  repository, and the text of the task passes through it. In the mode `agent` the box writes one line of
  warning to the log of the agent runtime for each request that it signs with the credentials of the
  microVM. The box is software in the same microVM: see "Limits".
- **Many sessions of one agent.** An agent can have many sessions at the same time. Each session has
  its own microVM, its own JWT, its own branch, its own stack and its own SSM path; the credentials
  that the broker requests are for one session, and IAM gives a runtime session a JWT only with its
  own session ID. Two sessions of the same agent in the same repository do not see each other, and a
  session that asks AWS CloudFormation for a resource of a different session gets an error of IAM
  (test P1 to P6).
- **The attribution does not depend on how the agent works inside.** The identity belongs to the
  agent runtime, not to the model. IAM gives a JWT only with the agent ID of the runtime and the
  session ID of the runtime session, the broker narrows the credentials to the session, and the
  workspace has no credentials and no network. None of these is a part of the loop of the model. The
  agent process can be one loop of one model, or many models that pass the work to each other in one
  process: the records show the same agent and the same session, and the credentials and the
  network of the session are the same.
- **GitHub as the repository host.** The issues and the code are on GitHub. The broker uses the
  GitHub REST API with an installation access token of the GitHub App of the agent, for the
  repository of the session only, and it does not keep the token. The actions on the repository are
  not in CloudTrail; the read of the IDs of the GitHub App and the signature of its JWT by AWS KMS are,
  with the agent and the session.
- **The use case comes from the alias.** The role of a repository can invoke one alias of the
  session starter. An event from a workflow cannot select a different use case.
- **The subject of the OIDC token of GitHub has IDs.** For these repositories GitHub writes the
  subject as `repo:<owner>@<owner ID>/<repository>@<repository ID>:ref:refs/heads/main`. The
  deployment reads the prefix from GitHub
  (`repos/<owner>/<repository>/actions/oidc/customization/sub`) and writes it to the trust policies. A
  new repository with the name of a deleted one does not get the roles of the old one.
- **The deployment after a merge is on the push to `main`.** The GitHub documentation does not say
  which subject the OIDC token of a workflow has when the closing of a pull request starts it
  (checked in November 2026). The deletion of the stack of a session is thus a step of the
  workflow of the merge.
- **Settings in the signed package.** The issuer, the audience, the prefix of the execution roles,
  the agent role, the session table, the agents and the repositories are in the file `config.json`
  of the signed package of a function. Reason: code signing covers the package of a function and not
  its configuration. The AWS clients of these functions ignore the endpoint settings of the
  environment.
- **`sub` in the interceptor.** The custom claims feature of the gateway compares a claim with one
  fixed value, and each agent has its own execution role. The request interceptor checks `sub`
  against the execution role of the agent that the token names. The broker checks it again.
- **Claims are nested.** STS writes the request tags to
  `"https://sts.amazonaws.com/".request_tags`, not to top-level claims. The gateway cannot validate
  them: the custom claims feature accepts only claim names without `/`.
- **Undocumented field.** The broker receives the bearer token in
  `context.client_context.custom["bedrockAgentCorePropagatedHeaders"]`. This field is not in the
  AgentCore documentation of the Lambda target input (checked in November 2026). The field can change.
- **`lambda:SourceFunctionArn` in the policy of the broker role.** AWS does not allow this condition
  key in a trust policy. The condition is in the identity policy of the broker role: the role can
  request a session of the agent role only with credentials that Lambda delivered to the broker
  function.
- **CDK versions.** `aws-cdk-lib` is pinned below 2.276 and the CDK CLI runs through `npx` at
  version 2.1148.1, because a later version of the library needs a CLI version that was not yet
  released.

## Alternatives that the system does not use

The facts about the services in this section are from the pages of the links, read in November 2026.

### OAuth 2.0 token exchange of Amazon Cognito

A user pool of Amazon Cognito supports the OAuth 2.0 token exchange (RFC 8693)
([announcement](https://aws.amazon.com/about-aws/whats-new/2026/11/cognito-token-exchange/),
[developer guide](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-token-exchange.html)).
A service or an agent that acts for a user sends the token of the user to the token endpoint of the user
pool (`grant_type=urn:ietf:params:oauth:grant-type:token-exchange`, or the operation `GetDerivedToken`)
and gets a new access token for one resource server. The scopes of the new token are never more than the
scopes of the token of the user. With an actor token (delegation) the new token has the claim `act` with
the identity of the actor. The subject is a user of the user pool, or a user of an identity provider that
the user pool has registered and linked to one of its users. The user pool needs the feature plan
Essentials or Plus.

The system has no user pool and does not use the token exchange:

- **A session has no token of a user.** A label on an issue starts a session. The person is a user of
  GitHub, not a user of a user pool, and the workflow has the OIDC token of GitHub Actions: its subject
  is the repository and the branch, not the person. The session table has the GitHub user who set the
  label.
- **No service of a session accepts a token of a user pool.** A session works on AWS (CloudFormation and
  SSM, with IAM credentials) and on GitHub (with the installation access token of a GitHub App). For both,
  the broker is still necessary: a derived token of Amazon Cognito would be a second token beside the JWT
  of AWS STS, and it would replace no component.
- **The records have what the claim `act` gives.** In CloudTrail the agent is the source identity and the
  session is the role session name, with the issue and the hash of the task as session tags. On GitHub
  the author of a commit is the GitHub App of the agent, with the lines `Agent-Id` and `Session-Id`.
- **The JWT of a session comes from IAM.** A runtime session requests its JWT from AWS STS
  (`GetWebIdentityToken`), and IAM allows it only with the runtime session ID of that session
  (`aws:AgentCoreRuntimeSourceSid`). The app client of a user pool has one secret for all its sessions:
  it cannot bind a token to one runtime session.

The token exchange fits a system in which a person signs in to an application and the agent then calls,
for that person, an API or an MCP server that verifies OAuth tokens: the token of the person is the
subject, the agent is the actor, and the API reads both from one token.

### Strands Box and AWS Lambda MicroVMs

**Strands Box** is a sandbox for an agent process, of the Strands Agents project
([announcement](https://aws.amazon.com/blogs/opensource/introducing-strands-box-ai-agent-sandboxes-powered-by-dogwood/),
[repository](https://github.com/strands-agents/box), developer preview, version 0.2.2). It runs on the
machine of the agent, without a guest operating system. The kernel (namespaces, a restricted view of the
file system and seccomp on Linux) sends every action of the agent through one exit, and a process of Box
outside the sandbox decides each action with a policy in the Dogwood language: `permit` and `forbid`
rules, with a denial when no rule permits. The actions are file access, shell commands, network
connections, HTTP requests and MCP calls. All egress passes through a gateway of Box, which resolves only
the hosts that the policy allows. The agent has placeholders in the place of its secrets: the gateway
puts the secret, or the SigV4 signature of AWS credentials, on a request that the policy permits
([network and credentials](https://strandsagents.com/docs/user-guide/box/guides/network-and-credentials/)).
The policy does not block the metadata endpoint without two `forbid` rules. Box has no identity of an
agent or of a session: the principal of a policy is always the agent, and its decision records are a
file that is not tamper evident.

**AWS Lambda MicroVMs** runs a Firecracker microVM from a container image, for 8 hours at most, with an
optional execution role for each microVM
([launch](https://docs.aws.amazon.com/lambda/latest/dg/microvms-launching.html),
[networking](https://docs.aws.amazon.com/lambda/latest/dg/microvms-networking.html)). A microVM has egress
to the internet, or egress through a VPC connector; the pages do not describe a microVM without egress,
and they do not describe its name resolution.

[An article](https://builder.aws.com/content/3LytpCABCdpSVyom2dv07I5PRq7/can-ai-agents-escape-a-lambda-microvm-sandbox-strands-box-prevented-it)
runs an agent in a Lambda MicroVM and sends 49 actions of 13 escape techniques. Its policy is a Python
program of the author modeled on Strands Box, not Strands Box itself. Without the policy, the microVM
stops the HTTP egress and the writes to the control plane, but names resolve, and the agent reads the
credentials of the execution role from the metadata endpoint. With the policy, 44 of the 49 actions stop
before the microVM boundary; a run with a model finds two holes in the policy (a read of
`/proc/1/environ` through an allowed program, and a path with `..`).

| | This system | The agent of the article |
| --- | --- | --- |
| Where the code that nobody reviewed runs | In the workspace: a microVM of its own (AgentCore Code Interpreter), not the microVM of the agent | In the microVM of the agent |
| Credentials in that place | None: the code interpreter has no execution role | The credentials of the execution role, at the metadata endpoint. The policy must forbid the read |
| Network of that place | No path to the internet and no name resolution: the network mode of the service, and a check of the agent at the start of every session | HTTP egress stopped, names resolve. The policy must forbid the name resolution |
| What stops an action | IAM and the network mode of the service, outside the microVM | A policy that a process in the same microVM evaluates |
| Identity of an agent and of a session | A JWT of AWS STS that IAM binds to the runtime session, the broker, CloudTrail and the trail | None in Box. The decisions are in a file of the microVM |

The system does not use them for the workspace, for the identity or as the agent runtime:

- **Not for the workspace.** A box in the microVM of the agent would put the code of the repository in the
  place that has the credentials of the execution role and the JWT, behind a policy. The workspace is a
  second microVM with no credentials and no network: nothing there depends on a policy that must name
  every path out. The description of Box itself names a microVM for each session as its companion for a
  system with many tenants.
- **Not for the identity.** Box has no token and no identity of a session. The JWT, the broker and the
  records of this system stay as they are with or without Box.
- **The agent runtime is AgentCore Runtime.** IAM binds the JWT of a session to the runtime session with
  the context key `aws:AgentCoreRuntimeSourceSid` of AgentCore Runtime. Whether a Lambda MicroVM gives its
  credentials a key of this kind is not verified here.

Where they fit:

- **Box around the agent or the work of a session.** The work of a session needs five destinations: AWS
  STS, the gateway, Amazon Bedrock, the API of the workspace and the package index. A policy of Box
  permits only these, and the credentials of the execution role are in the gateway of Box and not in the
  process of the work: a flaw of that process has no path to send a file or a JWT to a different address.
  It costs no VPC and no VPC endpoint. It is a boundary of software inside the microVM, so it is weaker
  than the VPC of the correction in "Limits". The system has it as the box option (see "Design notes").
- **A Lambda MicroVM as a workspace for a build that the code interpreter cannot run.** The workspace has
  Python 3.12 and no other toolchain. A Lambda MicroVM takes every container image. It needs a VPC
  connector to subnets without a route to the internet, no execution role, and the name resolution probe
  of this system at the start of a session. This use is not verified here.

The name resolution check of the workspace (see "Design notes") comes from this comparison.

### Managed alternatives to the broker

The broker is a function of the system, and the system must maintain its code. This section asks which
jobs of the broker a managed feature of AWS can do, and what happens to the guarantees of the system. The
statements come from the documentation of the services and from real requests of runtime sessions to a
separate stack with its own gateway and policy engine, which is not a part of this repository.

**The answer.** No managed feature takes the place of the broker, and the system keeps the broker and the
interceptor.

- Two jobs need code in every alternative: the GitHub App (the token, the commit and its texts) and the
  template of a commit. One job needs a caller that is not the agent: the request for the credentials of
  the session.
- **Policy in AgentCore**, with Cedar and Dogwood policies on the gateway, holds for the other jobs: the
  checks on the token and on the session, and the end of a session. It could take the place of the
  interceptor and of the read of the session table. The system does not adopt it: a policy cannot compare
  a tag of the JWT with an argument, so the design needs one policy for each session, and it moves code
  more than it removes code.
- **A Smithy target of the gateway** sends an AWS request without a function, and CloudTrail then has the
  role of the gateway, without the agent and without the session.
- **IAM alone** keeps the agent and the session in CloudTrail without a broker, and the agent then holds the
  credentials for its AWS actions.
- **AWS KMS** holds the private key of the GitHub App of an agent: the broker asks for a signature and
  never has the key. It is a change inside the broker, not a replacement of it, and the system has it (see
  "Design notes").

The guarantees that an alternative must keep:

1. Every AWS action has the agent and the session in CloudTrail.
2. The agent has no credentials for its AWS actions.
3. A session cannot touch the resources of a different session.
4. A commit has the GitHub App of the agent as its author, and the agent cannot write the identity lines.
5. A deployment has only code of a commit.
6. A session can be ended before its expiry time.

**The jobs of the broker, and the managed feature for each.**

| Job of the broker | Managed feature | Result |
| --- | --- | --- |
| Verify that the JWT is a JWT of the role of the agent (also the job of the interceptor) | Policy in AgentCore: a Cedar policy of a policy engine on the gateway ([core concepts](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-core-concepts.html)) | It holds. The `sub` claim is the principal of a policy, and the claim of AWS STS with the tags is one text that a policy matches with a pattern |
| Read the session table: the session exists, is active, is not expired, works on its repository | One Cedar policy for each session, with the values of the session as fixed text and the expiry time as a time condition | It holds. Without a policy, a session sees no tool, and the gateway refuses a request after the expiry time |
| End a session | The deletion of the policy of the session; a Dogwood policy for the end that a session asks for | The deletion holds: the gateway refuses the requests about 2 seconds later. The Dogwood policy holds only inside one policy session, and the policy session is a header that the caller chooses |
| Request credentials of the agent role for the session, with the agent as the source identity and the session as the role session name | A Smithy target of the gateway: the gateway sends the AWS request itself | It does not hold. CloudTrail has the role of the gateway, without the agent and without the session |
| The same | IAM alone: the runtime session requests the credentials itself, and the trust policy of the agent role sets the names | CloudTrail has the same names as with the broker, and IAM keeps the sessions apart. The session then holds the credentials: guarantee 2 is lost |
| Sign the JWT of the GitHub App of the agent, get an installation access token, write the commit and the pull request | AWS KMS for the private key and the signature (the system has it). None for the token, the commit and the pull request | AgentCore Identity has a credential provider for a GitHub OAuth App, which gives the token of a user, and a client assertion with a key only for an OAuth token endpoint ([GitHub provider](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-idp-github.html), [private key JWT](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/private-key-jwt.html)). The gateway has no template and no connector for GitHub ([templates](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-integrations.html), [connectors](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-connectors.html)), and an HTTP target forwards no `PUT` and no `PATCH` request ([HTTP target](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-http-passthrough.html)) |
| Write the identity lines and the issue line, show the text of the agent as plain text, refuse a path | None | A policy permits or forbids a request. It does not change a text |
| Deploy the template of the head commit of the session | A Dogwood policy: a deployment only with a commit ID that the commit tool returned | The policy holds for the order of the requests. It does not read a repository: a function must still read the template of the commit |
| The same | AWS CloudFormation Git sync; a pipeline of AWS CodePipeline; an IAM condition key | Git sync needs a connection that a person authorizes on GitHub, has one fixed role without a session name, and 100 sync configurations at most ([prerequisites](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/git-sync-prereq.html), [quotas](https://docs.aws.amazon.com/dtconsole/latest/userguide/limits-connections.html)). A pipeline has the branch and the commit as variables, and no function that makes the name of a stack from the name of a branch ([variables](https://docs.aws.amazon.com/codepipeline/latest/userguide/reference-variables.html)). No condition key of AWS CloudFormation names a commit ([condition keys](https://docs.aws.amazon.com/service-authorization/latest/reference/list_cloudformation.html)) |
| Write one log line for every decision | The log of the gateway | It holds, with more than the log of the broker: a refused request has the decision, the reason, the policy that decided and the principal, and no JWT |

**What each alternative does to the guarantees.**

- **Policy in AgentCore, with a function for the actions.** All six guarantees can stay, because the
  function requests the credentials, writes the commit and reads the template as the broker does. Guarantee
  3 then rests on one check in the place of three: the function receives no token from the gateway, so it
  works for the agent and the session of the arguments, and one policy says that they are the values of
  the JWT. In this system the gateway, the interceptor and the broker each verify the token.
- **A Smithy target, without a function.** Guarantees 1, 4 and 5 are lost: the role of the gateway acts,
  with the permissions of all sessions; there is no way to GitHub; the template would be an argument of
  the agent. A tool of a Smithy target also has the argument `Region`, and the gateway sends the request
  to the Region that the caller names.
- **IAM alone, without a gateway.** Guarantees 2, 4 and 5 are lost: the agent process holds the
  credentials of the agent role for its session, it would read the private key of the GitHub App itself,
  and no condition key names a commit. A deny for the session tag in a policy of the agent role ends a
  session only after several seconds, and not always at the first request.

**Why the system does not adopt Policy in AgentCore.**

1. **One policy for each session is a way around a limit of the service.** A policy cannot compare a tag of
   the JWT with an argument: the policy engine has the tags of AWS STS as one text, and a condition has
   comparisons and patterns, and no function that takes a value out of a text
   ([policy conditions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-conditions.html)).
   So the values of a session are fixed text in a policy of its own, and the policy engine becomes the
   register of the sessions. It brings the creation and the deletion of a policy for every session, with
   1,000 policies at most in a policy engine
   ([quotas](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html)),
   and a policy is active some seconds after the request for it.
2. **The code moves, it does not go away.** What goes: the interceptor, the verification of the JWT in the
   broker with its two libraries, and the read of the session table. What comes: the creation of the
   policy in the session starter, a component that deletes the policies of the sessions that ended or
   expired, the agent, the session and the repository as arguments of every tool, and a way for the issue
   and the hash of the task to the broker.
3. **The function gets no identity from the gateway.** It works for the agent and the session of the
   arguments, and one policy stands in the place of three checks of the token. A policy that permits too
   much, or a policy engine in the mode `LOG_ONLY`, lets the broker work for the agent and the session that
   a caller writes.
4. **The broker stays in every case**, as a signed function with its code: the credentials of the session,
   the GitHub App and the template of the commit.

These reasons are limits of the services, not of the idea. The next section lists what would remove them.

### What AWS would need to offer for a system without a broker

Each row is one thing that the system does with code of its own because a service of AWS does not do it.

| | What is missing | What the system would want | Code of the system that goes |
| --- | --- | --- | --- |
| 1 | **Values of a runtime session that IAM reads.** A runtime session gives IAM two context keys: its session ID and the ARN of its agent runtime ([session context keys](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html)). The component that starts a session cannot give it other values. So IAM binds the session ID of a JWT, and not the repository, the issue or the task of the session | Tags that the caller of `InvokeAgentRuntime` sets for a session, as IAM context keys of the credentials of that session, beside `aws:AgentCoreRuntimeSourceSid` | The check of the repository of a JWT against the session table, and the read of the issue and of the hash of the task from the table: they become tags of the JWT that IAM binds |
| 2 | **The tags of an STS-signed JWT in a policy.** The policy engine has the claim `https://sts.amazonaws.com/` as one text. A policy matches it with a pattern and cannot compare a tag with an argument ([principal tags](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/example-policies-principal.html)) | The tags of `GetWebIdentityToken` as claims of the first level of the JWT, or a policy that reads a member of a claim with a JSON object. Then `context.input.session_id` equal to the session ID of the JWT is one policy for all sessions | The policy for each session, with its creation, its deletion and its limit of 1,000 |
| 3 | **The policy session of a request.** It is a header that the caller chooses. A policy cannot name it: the policy engine accepts the condition, and its evaluation fails. A session of the same agent can send the policy session ID of a different session | A setting of the gateway: the policy session ID is a claim of the JWT. And `context.sessionId` in a condition | With it a Dogwood policy holds for a session: the end that a session asks for, and the rule that a deployment has a commit of the session, need no code |
| 4 | **The state of the runtime session of a JWT.** The gateway and a policy do not know whether the runtime session of a JWT still exists. A JWT stays valid to its expiry after its session stopped | An information provider for a policy with the runtime session of the caller: whether it exists, and its start time. Then `StopRuntimeSession` and the maximum lifetime of an agent runtime end the access at the gateway at once | The session table as the register that the broker reads for every request: the state, the expiry time, the end of a session |
| 5 | **The verified caller for a Lambda target.** A Lambda target receives six fields of the gateway and no claim. The `Authorization` header reaches a target only when an interceptor returns it ([header propagation](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-headers.html)) | A setting of a target: the verified claims of the JWT, or a list of them, in the context of the invocation | The interceptor. And one of two: the verification of the JWT in the function with its two libraries, or the trust in the arguments |
| 6 | **AWS requests of the gateway with the names of the session.** A Smithy target and a Lambda target use the role of the gateway. CloudTrail has the role session `gateway-session-invocation`, without a source identity. The credentials of the caller are a mode for an agent runtime target only, and for a caller with IAM credentials ([outbound authorization](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-outbound-auth.html)) | A mode of outbound authorization: for each request the gateway requests credentials of a role with the source identity, the role session name and the session tags from claims of the JWT, and with a session policy that has claims as variables | The request for the credentials of a session in the broker. With a Smithy target for AWS CloudFormation, the tools of the broker that read the stack; the deployment needs item 9 too |
| 7 | **A GitHub App as a credential provider.** The provider of AgentCore Identity for GitHub is an OAuth App and gives the token of a user. A client assertion with a key is for an OAuth token endpoint only. The gateway has no template for GitHub ([GitHub provider](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-idp-github.html), [private key JWT](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/private-key-jwt.html)) | A credential provider for a GitHub App: the private key in AWS KMS, where nobody reads it (this part works with code of the system); an installation access token for the repository of a claim, as the outbound authorization of a target; one event in CloudTrail for each token, with the claims of the caller | The KMS key of the App with its key policy and the import of the key, the two SSM parameters, the read of the IDs, the request for the signature and the request for the token |
| 8 | **A policy that sets a value.** A policy permits or forbids a request. It does not change an argument, and a condition has no function that makes a text from a claim ([policy conditions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-conditions.html)) | An argument of a tool that the gateway sets from a claim and that the caller cannot set: the name of the stack from the session ID, the branch `agent/<session ID>`, the identity lines at the end of a commit message | The rules of the broker for the names of the stack and of the branch, the identity lines and the issue line |
| 9 | **A template from a commit.** `CreateStack` takes the text of a template or an address in Amazon S3, and no condition key names a commit. Git sync has one fixed role without the name of a session, 100 sync configurations at most, and a deployment file in the repository ([condition keys](https://docs.aws.amazon.com/service-authorization/latest/reference/list_cloudformation.html), [Git sync](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/git-sync-prereq.html)) | A stack from a commit of a connected repository (the repository, the commit, the path), with condition keys for the repository and the branch. Or Git sync for a pattern of branches, with the name of the stack from the branch and a role session for each branch | The read of the template of the head commit and the head commit check of the broker |

What these features give together:

- **With 1, 2, 4 and 5:** policies for all sessions do the checks on the token and on the session. The
  interceptor, the verification of the JWT and the read of the session table go, and no code comes in
  their place. The broker stays for the credentials, for GitHub and for the template.
- **With 6 also:** the AWS actions of a session need no function, and CloudTrail has the agent and the
  session.
- **With 7, 8 and 9 also:** the commit, the pull request and the deployment need no function.
- **What stays code of the system with all of them:** the cleaning of the text of the agent (the
  characters that a reader does not see, a word that closes an issue), the text of the agent as plain text
  in a pull request, and the rule that a session reads a complete file.

## Limits
- **The trail is in the same account, and no rule records a change.** The trail writes its log files to
  an S3 bucket of the stack, with log file validation. A principal with administrator permissions in
  the account can stop the trail, delete log files, or delete the stack: the digest files show a
  deleted or changed log file, they do not prevent it. No EventBridge rule records a change to a
  function, to a role, to the gateway, to an agent runtime, to a code signing configuration, to the
  trail or to a resource-based policy: the events are in the trail, and nobody gets a notification.
  The requests and the answers of the model are in the log of the agent runtime only: model invocation
  logging of Amazon Bedrock is a setting of the account, and the system does not turn it on. The text of
  a command in the workspace is in that log only too: the data event of a workspace request has the
  caller, the time and the workspace session, not the command. The agent process writes that log.
  Correction: the trail to a bucket with object lock in a log archive account, and rules for the changes
  with subscriptions.
- **One agent role for all agents.** The agent registry says which use cases an agent may work on. IAM
  enforces it when a session requests the JWT, and the session starter checks it before it starts a
  session. Each agent has its own agent runtime, execution role, GitHub App and KMS key, and the two
  agents of the stack are kept apart (test A1 to A6). All agents use the same agent role. For the key and
  the IDs of the GitHub App, the role itself limits a session to its agent, with the source identity. For
  the stack, the session policy, not the role, limits a session to its own stack. Correction: one agent
  role for each agent.
- **The issue is input of the agent.** Every person who can set a label on an issue of a repository
  can start a session, and the text of the issue is the task. A text can ask the agent for a change
  that the owner of the repository does not want. The controls are the limits of a session (its
  branch, its stack, its SSM path) and the review of the pull request by a person. The session
  starter has no limit on the number of sessions.
- **A workflow of the repository runs the code of a session.** A push with the token of a GitHub App
  starts the workflows of a repository. The three repositories have only the two workflows of this
  system, and neither runs code of a branch of a session. A repository with a workflow for `push` or
  `pull_request` (the tests of a continuous integration) would run the code of a session on a runner
  of GitHub, with a path to the internet and with the secrets of the repository, before a person
  reviews it: the limits of the workspace do not hold there. Without such a workflow, the result of
  the tests in a pull request is text of the model: no component attests it. Not implemented.
  Correction: for the branches `agent/**`, a workflow without secrets and with `permissions: {}`, or
  a GitHub environment with required reviewers before the job; and a check on the commit, written
  by a component that is not the model, with the result of the tests.
- **The test copy of an application is SSM parameters only.** The application of each of the three
  repositories is a template with SSM parameters, and the agent role and the deploy roles can create
  nothing else. The limits of a session rest on names: a resource has the session ID in its name.
  An application with a function and an IAM role needs IAM actions for a template that nobody
  reviewed: a permissions boundary for every role of a template, `iam:PassRole` with conditions, and
  conditions on tags where a name cannot hold the session ID. This system does not show them.
- **Hidden text of an issue: the list is not complete.** The session starter removes HTML comments,
  comments in the form of a link definition and characters that a reader does not see. Markdown and
  HTML have more places that a page does not show and a model reads: the title of a link, the
  alternative text of an image, an attribute of an HTML element, a closed `<details>` section.
  Correction: the workflow asks GitHub for the text of the issue as the page shows it (the field
  `body_text` of the REST API) and sends that text as the task.
- **The model has a path out of a session.** The workspace has no path to the internet, and the model has
  two: the names and versions of the packages that it asks for go to pypi.org, whose download
  statistics are public, so a version number can carry a few bits to the owner of a package; and the
  text of a pull request, which every person with access to the repository reads. Neither carries
  more than the session can read: the repository of the session, the answers of its tools, the
  exports of the Region. Correction: a private package repository in the place of pypi.org.
- **A question that leaves the workspace without an answer.** The name resolution probe shows what
  comes back to the workspace: an address, or the answer of a public resolver. It does not show a
  resolver of the service that sends a question on and gives no answer back; the workspace cannot see
  this from the inside. Correction: a domain with a name server of the test account, a question from
  the workspace for a new name of that domain, and a test that reads the query log of the name server.
- **What a session can do in a repository.** A session reads text files in UTF-8 of up to 100 000
  characters, and the workspace gets the first 200 files of a repository: the agent tells the model
  what is missing, and a task that needs a missing file cannot be done. A commit has 20 files at
  most. A new file is never executable. A rename is a deletion and a new file. Every `read_file`
  is one request for credentials, one read of the IDs, one signature and one installation access token: a
  checkout of 200 files makes 200 of each. One answer of the model has 4096 tokens at most, so the
  model changes a large file with a shell command, not with the complete content. Correction: one
  token for a checkout (a tool that reads many files), the mode of a new file from the workspace,
  and limits that fit the repositories of the team.
- **The packages of a build are not checked.** The packages of the workspace come from the public
  package index (pypi.org), by name and version, without a list of allowed packages and without
  hashes. A harmful package runs in the workspace: it has no credentials and no network, so it can
  change only the work of the session, which a person reviews in the pull request. Accepted for this
  system: a list of allowed packages is outside the scope of this architecture.
- **The microVM of the agent has a path to the internet.** The section "The network of a session" has
  the difference between the microVM and the workspace. Nothing that the model writes runs in the
  microVM. Code runs there only if the container image is changed, or if the agent process or one of
  its libraries has a flaw; such code has the credentials of the execution role, the JWT of the
  session and the files of the repository in its memory, and a path to send them out. Correction, not
  implemented: the agent runtime in network mode VPC, in private subnets without a route to the
  internet; VPC endpoints for AWS STS, Amazon Bedrock, AgentCore (the gateway and the workspace) and
  CloudWatch Logs; a private package repository in the place of pypi.org; endpoint policies for the
  principals and the resources of the account; a DNS firewall with a list of allowed names; and a
  policy of the gateway that accepts a request only through the VPC endpoint, which also closes the
  limit of the bearer token. The cost is a VPC with several interface endpoints, each with a price
  for every hour. Which endpoints AgentCore Runtime itself needs in a VPC is not verified here.
  A second correction without a VPC is a setting of the system: the box option
  (`RUNTIME_BOX_AGENTS`, see "Design notes"). The process that has the text of the model and the files of
  a repository then has no credentials of the execution role, and it reaches only the destinations of a
  policy. It is weaker than the VPC: the box is software in the same microVM; the complete agent inside a
  box needs code of Box that is not in a release, with an experimental feature; and the agent has the JWT
  of the session. A program inside a box with the configuration of an agent was tested against known
  techniques to get the credentials, to reach a different address and to leave the sandbox, and every
  attempt was blocked, except the request of the next limit. That test does not cover a flaw of the
  kernel, of the gateway of the box or of the protocol of its tools, a race, or a long run. No session
  with the box option ran across a change of the credentials of the microVM.
- **AWS STS is a path to a different AWS account, also for an agent inside a box.** AWS STS answers the
  request `sts:AssumeRoleWithWebIdentity` for every caller: the JWT decides it, not the policy of the
  execution role. A process with the JWT of a session can send it for a role of a different AWS account.
  That account must have an identity provider for the issuer of this account and a role that trusts it;
  it then gets, in its CloudTrail, the name of the role session, 64 characters that the process writes.
  The path was not shown from end to end: it needs an identity provider and a role in a second account.
  The policy of a box has the host, the port, the method and the path of a request, not the action of AWS
  STS, so a box cannot stop it. Without a box the agent has a path to every address; with a box this is
  one of the paths that stay. Correction: the agent runtime in a VPC, with a VPC endpoint for AWS STS
  whose policy permits only the roles of this account.
- **Bearer token from every network.** The gateway has a public endpoint. A JWT is not bound to the
  microVM: a process that has the JWT of an active session can use it from every network for up to
  300 seconds. The JWT is in the memory of the agent process only. Correction: a key pair that the
  agent makes at the start of a session, the public key in the session table item, and a signature
  on every request that the broker verifies.
- **On GitHub, the limits of a session are in the broker.** The installation access token can write
  to every branch of the repository of the session, also to `main`. The broker writes only to the
  branch of the session and opens a pull request only from that branch, and the agent never has the
  token. A GitHub App without the permission Workflows cannot change a workflow file (not verified
  here). The broker refuses a path under `.github/` or `.git/`, and test I3 checks that the session did
  not change the workflow. A session can read all branches of its repository.
  Without branch protection or a ruleset on GitHub, nothing requires a pull request or a review for a
  change to `main`; some GitHub plans have neither for a private repository. Correction: a ruleset for
  `main` that requires a pull request with a review.
  Accepted for this system: the repositories are private, and the people who can write to them are trusted.
- **Both roles of a repository trust every workflow on `main`.** The trust policies of
  `agentfarm-github-<use case>` and `agentfarm-deploy-<use case>` name the repository and the
  branch, not the workflow file. A person who can change a workflow on `main` can use both roles:
  start sessions and change the live stack. Correction: a GitHub environment with required
  reviewers for the deployment, and a trust policy that names the environment in the subject.
  Accepted for this system: the repositories are private, and the people who can write to them are trusted.
- **The merge is the control of the live environment.** The workflow deploys the template of the
  merged commit. A reviewer who merges a harmful change deploys it. The deploy role limits the
  template to the live stack and to the SSM path of the live environment of that repository; a
  template with a different resource fails. The deploy role can delete every stack that has the tag
  of its repository, also the stack of a session that still works.
- **A pull request that a person closes without a merge keeps its stack.** Only the merge deletes
  the stack of a session. Correction: a scheduled function that deletes the stack of a session some
  days after the end of the session.
- **The stack of a session.** A session can touch only resources with its session ID in the name.
  The tools cannot delete a stack. After a first deployment that fails, the stack is in
  `ROLLBACK_COMPLETE` and the session cannot deploy again. When a template names a resource that the session may not write, the
  stack can end in `ROLLBACK_FAILED`: IAM also denies the deletion in the rollback. The reason of a
  failure can name the stack of a different session ("already exists in stack ...").
- **The template is input of the agent.** Only the session policy limits what a template of a
  session can do. There is no list of allowed resource types and no policy check of the template.
  `Fn::ImportValue` reads every export of the Region. An output with `Export` takes an export name
  of the account. A `Tags` property of a resource can write the `agent_id` of a different agent on
  the SSM parameter of the session, because the session policy has no condition on tags. A wait
  condition keeps a stack operation open for up to 12 hours. Not verified at run time. Correction: a
  check of the template in the broker (no `Fn::ImportValue`, no `Export`, no `Transform`, a list of
  allowed resource types), and `aws:RequestTag` conditions in the session policy. A dynamic reference
  is resolved with the credentials for the stack: it can read an SSM parameter of the session, and no
  value of the GitHub App.
- **"Every AWS action" means every action through the broker.** No service control policy prevents
  a different path. A reader of CloudTrail must filter on the agent role: a principal with
  `sts:SetSourceIdentity` on a different role can set the same source identity there. The model
  requests, the workspace requests and the JWT requests of a session have the execution role of the
  agent with a role session name that is not the session ID: the JWT request of the same role
  session has the session ID as a request tag, and it connects them to the session.
- **A stack operation is longer than its credentials.** The credentials of a request of the broker
  are valid for 900 seconds. CloudFormation continues a stack operation with the identity of the
  session after their expiry and after the end of the session. Not tested in this system.
- **Identity lines.** The broker removes lines that start with `Agent-Id:` or `Session-Id:` from the
  text of the agent, and an identity key with its value at any place of a line, in a commit message
  and in the title and the description of a pull request. Before the search it removes the characters
  that a reader does not see. All other words of the agent stay: a model
  can write the name of a different agent as plain words, and a key with look-alike letters of a
  different alphabet also stays. The last lines of a commit message and of a pull
  request description are the lines of the broker, and the author of a commit and the CloudTrail
  event are not text of the agent: a reader must use these. The title of a pull request and a commit
  message are text of the agent that GitHub does not show as a code block.
- **The head commit check is a check of the commit message.** `deploy_stack` deploys the head commit
  of the session branch when its message ends with the session ID. A person with write access to
  the repository can push a commit with such a message to the branch of a session, and the broker
  would then deploy a template of a different author with the identity of the session. Correction:
  the broker writes the ID of each commit to the session table item and deploys only that commit.
  Accepted for this system: the repositories are private, and the people who can write to them are trusted.
- **The broker does not read the agent registry.** The broker accepts every agent ID whose execution
  role, by its name `agentfarm-runtime-exec-<agent_id>`, requested the JWT, and every repository of
  the system. The registry is enforced earlier: by IAM at the request of the JWT and by the session
  starter. Correction: the registry in the settings of the broker.
- **Who controls the deployment controls the identities.** A principal that can change the code of
  the broker, of the interceptor or of the session starter can forge an identity. Code signing stops
  a package that the signing profile did not sign; a principal that can start a signing job with the
  profile can sign a different package. The ARNs of the agent runtimes are an environment variable
  of the session starter, not a setting of its signed package. The container image of the agent is
  not signed: a different image gets JWTs only for its own agent and its own sessions. A principal
  that can change the trust policy of the agent role needs no function.
- **Test fields of the session starter.** An invocation without an alias can have the fields `mode`,
  `greeting`, `task` and `parameter_name`: a scripted work without a model, a different task text, or
  a scripted work that asks for an SSM parameter with a given name. The roles of the
  repositories can invoke an alias only, and an invocation of an alias ignores these fields. No
  control turns them off for a principal of the account that can invoke the function. The container
  image also contains the probe and the scripted work of the tests.
- **The text of an issue is on GitHub only.** The session table and CloudTrail have the hash of the
  task that the agent received: the hash shows a later change of the issue, it does not give the text
  back. The person who asked is in the session table item and in the log of the session starter,
  which keep one year, and on GitHub. Correction: the session starter writes the text of the task to
  a bucket with object lock.
- **Supply chain of the packages.** The container image names its base image by a tag, not by a
  digest. The packages name the versions of their dependencies without hashes. The workflows name
  the GitHub Actions by a version tag, not by a commit. `make runtime-deploy` approves every IAM
  change of a stack (`--require-approval never`). Correction: a base image by digest, requirements
  with hashes, actions by commit, and a signature in a pipeline, not on a workstation.
- **Error messages of AWS and of GitHub go to the agent.** The broker returns the code and the
  message of an error to the agent. A message can name an ARN and the account. Correction: a fixed
  text for each error code.

## License

The code and the documents of this repository are under the Apache License 2.0: see [LICENSE](LICENSE).
