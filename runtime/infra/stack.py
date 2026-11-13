"""The stack AgentFarmRuntime: the agents run in Amazon Bedrock AgentCore Runtime.

```
session starter Lambda      makes the session ID, writes the session to the session table one time
   │ InvokeAgentRuntime(runtimeSessionId = session ID)
   ▼
agent runtime of one agent, platform version V2     one runtime session = one microVM = one session
   │ GetWebIdentityToken(tags: agent_id, session_id, repository), for every request ──▶ AWS STS
   │     IAM allows only: the agent of this runtime, the runtime session ID of this session
   │     (`aws:AgentCoreRuntimeSourceSid`), a repository of this agent
   │ MCP tools/call, Authorization: Bearer JWT
   ▼
AgentCore Gateway ──▶ request interceptor ──▶ Broker Lambda ──▶ AssumeRole(agent role, SourceIdentity = agent_id,
                                                                RoleSessionName = session_id, session policy)
```

The stack has no VPC, and no component delivers the JWT. A runtime session requests its own JWT, and IAM binds the JWT to the
runtime session. AgentCore Runtime accepts every session ID of a caller, so the session starter is
the only role that can invoke an agent runtime, and the broker accepts only a session that the
session starter wrote. The agent is an LLM agent (agent/runtime_server.py). The code of a session
runs in the workspace of the session, a session of the AgentCore Code Interpreter without an
execution role: no AWS credentials and no JWT there. The network mode of the code interpreter is
SANDBOX: the workspace has no path to the internet, and the agent writes the packages of a build to it
as files. At the end of its work a session ends itself with
the tool `end_session` of the broker.

The settings of the broker, of the interceptor and of the session starter are files of their signed
packages (infra/config.py). The repositories are on GitHub. A workflow of a repository starts a session
from an issue, and a second workflow deploys the live environment of the repository after a merge: each
has its own role in this stack.
"""
from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_cloudtrail as cloudtrail
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from infra import names
from infra.names import (
    AUDIENCE, JWT_TAGS, REPOSITORY_PREFIX, RUNTIME_PREFIX as PREFIX, STACK_PREFIX, TARGET_NAME,
    TOKEN_LIFETIME_SECONDS,
)
from infra.tools import broker_tools

PLATFORM_VERSION = "V2"   # the session context keys exist only on this platform version
WORKSPACE_NETWORK = "SANDBOX"   # the network mode of the workspace: no path to the internet


class RuntimeStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, issuer: str, agents: dict[str, list[str]],
                 use_cases: list[str], signed: dict, model_id: str, inference_profile_arn: str,
                 foundation_model_arns: list[str], github_owner: str = "",
                 github_subjects: dict[str, str] | None = None, box_agents: list[str] | None = None,
                 box_mode: str = "agent", kms_app_key_agents: list[str] | None = None, **kwargs) -> None:
        """`github_owner`: the account or the organization on GitHub that has the repositories. Every agent has a
        GitHub App (infra/names.py). `github_subjects`: for each repository, the subject of the OIDC token of a
        workflow that runs from the branch main (infra/config.py)."""
        super().__init__(scope, construct_id, **kwargs)

        package_bucket = s3.Bucket.from_bucket_name(self, "PackageBucket", signed["bucket"])
        code_signing = lambda_.CodeSigningConfig.from_code_signing_config_arn(
            self, "CodeSigningConfig", signed["code_signing_config_arn"])

        def signed_function(construct_name: str, name: str, package: str, role: iam.IRole, **settings) -> lambda_.Function:
            """A function with a signed package. Its settings are the file config.json of the package."""
            log_group = logs.LogGroup(
                self, f"{construct_name}LogGroup", log_group_name=f"/aws/lambda/{name}",
                retention=logs.RetentionDays.ONE_YEAR, removal_policy=RemovalPolicy.DESTROY)
            role.add_to_principal_policy(iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"], resources=[f"{log_group.log_group_arn}:*"]))
            return lambda_.Function(
                self, construct_name, function_name=name, runtime=lambda_.Runtime.PYTHON_3_13,
                handler="handler.handler", code=lambda_.Code.from_bucket(package_bucket, signed["packages"][package]["key"]),
                code_signing_config=code_signing, role=role, log_group=log_group, memory_size=256, **settings)

        # --- Repositories: one for each use case, on GitHub, with the sample code of repositories/<use case> on
        # the branch `main`. This stack has no repository.
        repository_names = {use_case: f"{REPOSITORY_PREFIX}{use_case}" for use_case in use_cases}

        # --- Session table: the session starter writes a session here. The broker reads it ----------
        # The table has a resource-based policy: only the session starter and the broker write an item. A
        # different principal of the account with DynamoDB permissions cannot write a session, set an ended
        # session to active again, or change the issue and the hash of the task of a session. The policy does
        # not deny a change of the policy itself: the trail records that change.
        session_writers = [names.role_arn(self.account, f"{PREFIX}-starter"), names.role_arn(self.account, f"{PREFIX}-broker")]
        session_table = dynamodb.TableV2(
            self, "SessionTable", table_name=names.RUNTIME_SESSION_TABLE,
            partition_key=dynamodb.Attribute(name="session", type=dynamodb.AttributeType.STRING),
            time_to_live_attribute="delete_after", removal_policy=RemovalPolicy.DESTROY,
            resource_policy=iam.PolicyDocument(statements=[iam.PolicyStatement(
                sid="OnlyTheSessionStarterAndTheBrokerWrite", effect=iam.Effect.DENY, principals=[iam.AnyPrincipal()],
                actions=["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem",
                         "dynamodb:PartiQLInsert", "dynamodb:PartiQLUpdate", "dynamodb:PartiQLDelete"],
                resources=[f"arn:aws:dynamodb:{self.region}:{self.account}:table/{names.RUNTIME_SESSION_TABLE}"],
                conditions={"ArnNotEquals": {"aws:PrincipalArn": session_writers}})]))

        # --- Broker and agent role -------------------------------------------------------------------
        lambda_service = iam.ServicePrincipal("lambda.amazonaws.com")
        broker_role = iam.Role(self, "BrokerRole", role_name=f"{PREFIX}-broker", assumed_by=lambda_service)
        # The broker reads a session for every request, and ends it when the session asks (tool `end_session`).
        broker_role.add_to_policy(iam.PolicyStatement(
            actions=["dynamodb:GetItem", "dynamodb:UpdateItem"], resources=[session_table.table_arn]))
        broker_principal = iam.ArnPrincipal(broker_role.role_arn)
        # The agent role: the identity that performs the AWS actions. The broker narrows every session of
        # this role to one session ID with a session policy.
        agent_role = iam.Role(
            self, "AgentRole", role_name=names.RUNTIME_AGENT_ROLE,
            assumed_by=broker_principal.with_conditions({"Null": {"sts:SourceIdentity": "false"}}))
        agent_role.assume_role_policy.add_statements(iam.PolicyStatement(
            actions=["sts:SetSourceIdentity"], principals=[broker_principal]))
        # The session tags of a session: the issue that started it and the hash of its task. The AssumeRole
        # event in AWS CloudTrail has them.
        agent_role.assume_role_policy.add_statements(iam.PolicyStatement(
            actions=["sts:TagSession"], principals=[broker_principal],
            conditions={"ForAllValues:StringEquals": {"aws:TagKeys": names.SESSION_TAGS}}))
        for statement in (
            iam.PolicyStatement(
                actions=["cloudformation:CreateStack", "cloudformation:UpdateStack", "cloudformation:TagResource",
                         "cloudformation:UntagResource", "cloudformation:DescribeStacks",
                         "cloudformation:DescribeStackEvents"],
                resources=[f"arn:aws:cloudformation:{self.region}:{self.account}:stack/{STACK_PREFIX}*/*"]),
            # CloudFormation uses the credentials of the agent session for the resources in the template.
            iam.PolicyStatement(
                actions=["ssm:PutParameter", "ssm:AddTagsToResource", "ssm:RemoveTagsFromResource",
                         "ssm:ListTagsForResource", "ssm:GetParameters", "ssm:DeleteParameter"],
                resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter/agentfarm/*"]),
        ):
            agent_role.add_to_policy(statement)
        # The broker reads the secret of the GitHub App of an agent with the credentials of a session. The
        # session policy limits a session to the secret of its agent. Only a direct request can read the
        # secret, not a request that an AWS service sends for the session: AWS CloudFormation resolves a
        # dynamic reference of a template with the credentials of the caller, and the template of a
        # session is input of the agent. The broker also gives AWS CloudFormation credentials whose
        # session policy has no secret (broker/handler.py).
        # Only when an agent of the stack has a secret: an agent with a KMS key has none.
        if any(agent_id not in (kms_app_key_agents or []) for agent_id in agents):
            agent_role.add_to_policy(iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:"
                           f"{names.GITHUB_APP_SECRET_PREFIX}*"],
                conditions={"Null": {"aws:CalledViaFirst": "true"}}))
        # The secret of each agent without a KMS key has a resource-based policy: only a session of the
        # agent role reads the value. A different principal of the account with Secrets Manager permissions
        # does not get the private key of the GitHub App. A person made the secret; this stack sets only its
        # policy. An agent with a KMS key has no secret.
        for agent_id in [agent_id for agent_id in agents if agent_id not in (kms_app_key_agents or [])]:
            secretsmanager.CfnResourcePolicy(
                self, f"GitHubAppSecretPolicy-{agent_id}", secret_id=f"{names.GITHUB_APP_SECRET_PREFIX}{agent_id}",
                block_public_policy=True,
                resource_policy={"Version": "2012-10-17", "Statement": [{
                    "Sid": "OnlyTheAgentRoleReadsTheValue", "Effect": "Deny", "Principal": "*",
                    "Action": "secretsmanager:GetSecretValue", "Resource": "*",
                    "Condition": {"ArnNotEquals": {"aws:PrincipalArn": agent_role.role_arn}}}]})
        # The GitHub App of an agent of `kms_app_key_agents`: its private key in AWS KMS, and its two IDs in SSM
        # parameters. No value of an App is in the stack or in the repository.
        #
        # The stack makes a KMS key without key material; a person imports the private key that GitHub
        # generated for the App (runtime/github/import_app_key.py). Nobody reads the key material of a KMS key:
        # the broker asks for a signature with the credentials of a session. The key policy denies a signature
        # to every principal that is not the agent role. The agent role signs only with the alias of the agent
        # of its session (the source identity), with the algorithm of the JWT of a GitHub App, and only in a
        # request that an AWS service does not send for the session. The stack keeps a key when its agent
        # leaves the list or when the stack is deleted: the key has the private key of an App.
        #
        # The stack makes the two SSM parameters with a value that says "not set"; a person writes the IDs with
        # a command (runtime/github/set_app_ids.py). A deployment does not write the value again, because the
        # template does not change. The agent role reads only the parameters of the agent of its session.
        app_key_ids = {}
        for agent_id in kms_app_key_agents or []:
            app_key = kms.CfnKey(
                self, f"GitHubAppKey-{agent_id}", description=f"The private key of the GitHub App of {agent_id}.",
                origin="EXTERNAL", key_spec="RSA_2048", key_usage="SIGN_VERIFY",
                key_policy={"Version": "2012-10-17", "Statement": [
                    {"Sid": "Account", "Effect": "Allow", "Principal": {"AWS": f"arn:aws:iam::{self.account}:root"},
                     "Action": "kms:*", "Resource": "*"},
                    {"Sid": "OnlyTheAgentRoleSigns", "Effect": "Deny", "Principal": "*", "Action": "kms:Sign",
                     "Resource": "*", "Condition": {"ArnNotEquals": {"aws:PrincipalArn": agent_role.role_arn}}}]})
            app_key.apply_removal_policy(RemovalPolicy.RETAIN)
            alias = kms.CfnAlias(self, f"GitHubAppKeyAlias-{agent_id}", target_key_id=app_key.attr_key_id,
                                 alias_name=f"{names.GITHUB_APP_KEY_ALIAS_PREFIX}{agent_id}")
            alias.apply_removal_policy(RemovalPolicy.RETAIN)
            app_key_ids[agent_id] = app_key.attr_key_id
            for name in names.GITHUB_APP_ID_NAMES:
                ssm.CfnParameter(
                    self, f"GitHubApp-{agent_id}-{name}", type="String", value=names.GITHUB_APP_ID_NOT_SET,
                    name=f"{names.GITHUB_APP_SETTINGS_PREFIX}{agent_id}/{name}",
                    description=f"The {name} of the GitHub App of {agent_id}. Written with runtime/github/set_app_ids.py.")
        if app_key_ids:
            agent_role.add_to_policy(iam.PolicyStatement(
                actions=["kms:Sign"], resources=[f"arn:aws:kms:{self.region}:{self.account}:key/*"],
                conditions={
                    "StringEquals": {
                        "kms:RequestAlias": names.GITHUB_APP_KEY_ALIAS_PREFIX + "${aws:SourceIdentity}",
                        "kms:SigningAlgorithm": names.GITHUB_APP_SIGNING_ALGORITHM, "kms:MessageType": "RAW"},
                    "Null": {"aws:CalledViaFirst": "true"}}))
            agent_role.add_to_policy(iam.PolicyStatement(
                actions=["ssm:GetParameters"],
                resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter"
                           f"{names.GITHUB_APP_SETTINGS_PREFIX}${{aws:SourceIdentity}}/*"],
                conditions={"Null": {"aws:CalledViaFirst": "true"}}))
        # The credentials of the broker role can request a session of the agent role only when Lambda
        # delivered them to the broker function.
        broker_role.add_to_policy(iam.PolicyStatement(
            actions=["sts:AssumeRole", "sts:SetSourceIdentity", "sts:TagSession"], resources=[agent_role.role_arn],
            conditions={"ArnEquals": {"lambda:SourceFunctionArn": names.function_arn(
                self.account, self.region, f"{PREFIX}-broker")}}))
        broker_fn = signed_function("BrokerFunction", f"{PREFIX}-broker", "broker", broker_role,
                                    timeout=Duration.seconds(30))

        # --- Request interceptor: checks the role of the agent, hands the bearer token to the broker -
        interceptor_fn = signed_function(
            "InterceptorFunction", f"{PREFIX}-interceptor", "interceptor",
            iam.Role(self, "InterceptorRole", role_name=f"{PREFIX}-interceptor", assumed_by=lambda_service),
            timeout=Duration.seconds(5))

        # --- AgentCore Gateway: custom JWT authorizer (STS issuer, one audience). No VPC -------------
        gateway_role = iam.Role(
            self, "GatewayRole", role_name=f"{PREFIX}-gateway",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com", conditions={
                "StringEquals": {"aws:SourceAccount": self.account}}))
        broker_fn.grant_invoke(gateway_role)
        interceptor_fn.grant_invoke(gateway_role)
        G = agentcore.CfnGateway
        gateway = G(
            self, "Gateway", name=f"{PREFIX}-gateway", role_arn=gateway_role.role_arn,
            protocol_type="MCP", authorizer_type="CUSTOM_JWT",
            authorizer_configuration=G.AuthorizerConfigurationProperty(
                custom_jwt_authorizer=G.CustomJWTAuthorizerConfigurationProperty(
                    discovery_url=f"{issuer}/.well-known/openid-configuration", allowed_audience=[AUDIENCE])),
            interceptor_configurations=[G.GatewayInterceptorConfigurationProperty(
                interception_points=["REQUEST"],
                interceptor=G.InterceptorConfigurationProperty(
                    lambda_=G.LambdaInterceptorConfigurationProperty(arn=interceptor_fn.function_arn)),
                input_configuration=G.InterceptorInputConfigurationProperty(pass_request_headers=True))])
        gateway.node.add_dependency(gateway_role)
        T = agentcore.CfnGatewayTarget
        target = T(
            self, "BrokerTarget", name=TARGET_NAME, gateway_identifier=gateway.attr_gateway_identifier,
            target_configuration=T.TargetConfigurationProperty(mcp=T.McpTargetConfigurationProperty(
                lambda_=T.McpLambdaTargetConfigurationProperty(
                    lambda_arn=broker_fn.function_arn,
                    tool_schema=T.ToolSchemaProperty(inline_payload=broker_tools())))),
            credential_provider_configurations=[
                T.CredentialProviderConfigurationProperty(credential_provider_type="GATEWAY_IAM_ROLE")])
        target.node.add_dependency(gateway_role)

        # --- Log of the gateway: the start and the end of every request, with the request and the answer.
        # AgentCore configures no log destination for a gateway. The bearer token is a header, not a body.
        gateway_log_group = logs.LogGroup(
            self, "GatewayLogGroup",
            log_group_name=f"/aws/vendedlogs/bedrock-agentcore/gateway/APPLICATION_LOGS/{PREFIX}-gateway",
            retention=logs.RetentionDays.ONE_YEAR, removal_policy=RemovalPolicy.DESTROY)
        log_source = logs.CfnDeliverySource(
            self, "GatewayLogSource", name=f"{PREFIX}-gateway-logs", log_type="APPLICATION_LOGS",
            resource_arn=gateway.attr_gateway_arn)
        log_destination = logs.CfnDeliveryDestination(
            self, "GatewayLogDestination", name=f"{PREFIX}-gateway-logs",
            destination_resource_arn=gateway_log_group.log_group_arn)
        log_delivery = logs.CfnDelivery(
            self, "GatewayLogDelivery", delivery_source_name=log_source.name,
            delivery_destination_arn=log_destination.attr_arn)
        log_delivery.node.add_dependency(log_source)
        log_delivery.node.add_dependency(log_destination)

        # --- The workspace: where the code of a session runs. No execution role: no AWS credentials there.
        # Network mode SANDBOX: no path to the internet. Code that runs there cannot send the code of the
        # repository to an address, and it cannot download anything. The agent gets the packages of a build
        # and writes them to the workspace as files (agent/packages.py).
        sandbox = agentcore.CfnCodeInterpreterCustom(
            self, "Workspace", name="agentfarm_workspace_isolated",
            description="Workspace of a session: builds and tests. No credentials. No path to the internet",
            network_configuration=agentcore.CfnCodeInterpreterCustom.CodeInterpreterNetworkConfigurationProperty(
                network_mode=WORKSPACE_NETWORK))

        # --- One agent runtime and one execution role for each agent ---------------------------------
        image = ecr_assets.DockerImageAsset(self, "AgentImage", directory="agent",
                                            platform=ecr_assets.Platform.LINUX_ARM64)
        runtimes = {}
        for agent_id, agent_use_cases in agents.items():
            # The name of an agent runtime has letters, digits and underscores only. Its ID is the name, a hyphen
            # and a suffix of the service, and its log group has the ID in its name. An execution role writes
            # only the log of its own agent runtime: the log is the record of what the model asked and of what ran
            # in the workspace, and the agent runtime of a different agent must not be able to write in it.
            runtime_name = f"agentfarm-{agent_id}".replace("-", "_")
            logs_arn = (f"arn:aws:logs:{self.region}:{self.account}:log-group:"
                        f"/aws/bedrock-agentcore/runtimes/{runtime_name}-*")
            role = iam.Role(
                self, f"ExecutionRole-{agent_id}", role_name=f"{names.RUNTIME_TOKEN_ROLE_PREFIX}{agent_id}",
                assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com", conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:*"}}))
            for statement in (
                # The JWT of a session: one audience, 900 seconds at most, all three tags.
                iam.PolicyStatement(
                    actions=["sts:GetWebIdentityToken"], resources=["*"],
                    conditions={
                        "ForAllValues:StringEquals": {"sts:IdentityTokenAudience": AUDIENCE},
                        "Null": {"sts:IdentityTokenAudience": "false",
                                 **{f"aws:RequestTag/{tag}": "false" for tag in JWT_TAGS}},
                        "NumericLessThanEquals": {"sts:DurationSeconds": str(TOKEN_LIFETIME_SECONDS)},
                        "StringEquals": {"sts:SigningAlgorithm": "ES384"}}),
                # The tags of the JWT: this agent, a repository of this agent, and the runtime session ID of
                # the session that has the credentials. Credentials without the session context key match no value.
                iam.PolicyStatement(
                    actions=["sts:TagGetWebIdentityToken"], resources=["*"],
                    conditions={
                        "ForAllValues:StringEquals": {"aws:TagKeys": JWT_TAGS},
                        "StringEquals": {
                            "aws:RequestTag/agent_id": agent_id,
                            "aws:RequestTag/repository": [repository_names[u] for u in agent_use_cases],
                            "aws:RequestTag/session_id": "${aws:AgentCoreRuntimeSourceSid}"},
                        "Null": {"aws:AgentCoreRuntimeSourceSid": "false"}}),
                # The model of the agent: one inference profile and the foundation model behind it.
                iam.PolicyStatement(actions=["bedrock:InvokeModel"], resources=[inference_profile_arn]),
                iam.PolicyStatement(
                    actions=["bedrock:InvokeModel"], resources=foundation_model_arns,
                    conditions={"StringEquals": {"bedrock:InferenceProfileArn": inference_profile_arn}}),
                # The workspace of the system only.
                iam.PolicyStatement(
                    actions=["bedrock-agentcore:StartCodeInterpreterSession", "bedrock-agentcore:InvokeCodeInterpreter",
                             "bedrock-agentcore:StopCodeInterpreterSession"],
                    resources=[sandbox.attr_code_interpreter_arn]),
                # What AgentCore Runtime needs to start the container and to write its log.
                iam.PolicyStatement(actions=["ecr:GetAuthorizationToken"], resources=["*"]),
                iam.PolicyStatement(actions=["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
                                    resources=[image.repository.repository_arn]),
                iam.PolicyStatement(actions=["logs:CreateLogGroup", "logs:DescribeLogStreams"], resources=[logs_arn]),
                iam.PolicyStatement(actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                                    resources=[f"{logs_arn}:log-stream:*"]),
                iam.PolicyStatement(actions=["logs:DescribeLogGroups"],
                                    resources=[f"arn:aws:logs:{self.region}:{self.account}:log-group:*"]),
            ):
                role.add_to_policy(statement)
            runtime = agentcore.CfnRuntime(
                self, f"AgentRuntime-{agent_id}",
                agent_runtime_name=runtime_name,
                description=f"Agent {agent_id}. One runtime session is one session of this agent.",
                role_arn=role.role_arn, platform_version=PLATFORM_VERSION, protocol_configuration="HTTP",
                agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                    container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(
                        container_uri=image.image_uri)),
                network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(network_mode="PUBLIC"),
                # A session ends after 5 minutes without a request and without work, and after the session time.
                lifecycle_configuration=agentcore.CfnRuntime.LifecycleConfigurationProperty(
                    idle_runtime_session_timeout=300, max_lifetime=names.RUNTIME_SESSION_SECONDS),
                environment_variables={
                    "AGENT_HOST": "agentcore-runtime", "AGENTFARM_REGION": self.region, "AGENT_ID": agent_id,
                    "AUDIENCE": AUDIENCE, "REPOSITORY": repository_names[agent_use_cases[0]],
                    "GATEWAY_URL": gateway.attr_gateway_url, "MODEL_ID": model_id,
                    "CODE_INTERPRETER_ID": sandbox.attr_code_interpreter_id, "WORKSPACE_NETWORK": WORKSPACE_NETWORK,
                    # A box of Strands Box for this agent (agent/boxed.py): `agent` puts the complete agent inside
                    # a box, `work` the work of a session only. An agent that is not in the list has no box.
                    "AGENT_BOX": box_mode if agent_id in (box_agents or []) else "none"})
            runtime.node.add_dependency(role)
            runtimes[agent_id] = runtime
            # The policy of the agent runtime: only the session starter sends it a request, and nobody runs a
            # command in the microVM of a session. Without it, a principal of the account with AgentCore
            # permissions reaches the microVM of an active session, where the credentials of the session are.
            # The policy does not deny a change of the policy itself: the trail records that change.
            everyone = {"AWS": f"arn:aws:iam::{self.account}:root"}
            agentcore.CfnResourcePolicy(
                self, f"AgentRuntimePolicy-{agent_id}", resource_arn=runtime.attr_agent_runtime_arn,
                policy=self.to_json_string({"Version": "2012-10-17", "Statement": [
                    {"Sid": "OnlyTheSessionStarterInvokes", "Effect": "Deny", "Principal": everyone,
                     "Action": ["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:InvokeAgentRuntimeForUser",
                                "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream",
                                "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStreamForUser"],
                     "Resource": runtime.attr_agent_runtime_arn,
                     "Condition": {"ArnNotEquals": {"aws:PrincipalArn": names.role_arn(self.account, f"{PREFIX}-starter")}}},
                    {"Sid": "NoCommandInTheMicroVMOfASession", "Effect": "Deny", "Principal": everyone,
                     "Action": ["bedrock-agentcore:InvokeAgentRuntimeCommand",
                                "bedrock-agentcore:InvokeAgentRuntimeCommandShell"],
                     "Resource": runtime.attr_agent_runtime_arn}]}))

        # --- Session starter: the only role that can invoke an agent runtime -------------------------
        starter_role = iam.Role(self, "StarterRole", role_name=f"{PREFIX}-starter", assumed_by=lambda_service)
        starter_role.add_to_policy(iam.PolicyStatement(
            actions=["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:GetItem"], resources=[session_table.table_arn]))
        starter_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:InvokeAgentRuntime"],
            resources=[arn for runtime in runtimes.values()
                       for arn in (runtime.attr_agent_runtime_arn, f"{runtime.attr_agent_runtime_arn}/*")]))
        starter_fn = signed_function(
            "StarterFunction", f"{PREFIX}-starter", "starter", starter_role, timeout=Duration.seconds(60),
            # The ARN of an agent runtime has a suffix that CloudFormation makes with this stack.
            environment={"AGENT_RUNTIMES": self.to_json_string(
                {agent_id: runtime.attr_agent_runtime_arn for agent_id, runtime in runtimes.items()})})

        # --- Trail: the events of the account in an S3 bucket, with log file validation ---------------
        # The event history of AWS CloudTrail keeps 90 days and has management events only. The trail keeps
        # the management events of all Regions (IAM is a global service, with its events in us-east-1) and
        # these data events: every read and write of the session table (who writes and ends a session), and
        # every invocation of the functions that decide an identity (who starts a session, who asks the
        # broker). `make destroy` stops the trail and empties the bucket before it deletes the stack.
        trail_bucket = s3.Bucket(
            self, "TrailBucket", bucket_name=f"{PREFIX}-trail-{self.account}-{self.region}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL, enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY)
        trail = cloudtrail.Trail(
            self, "Trail", trail_name=f"{PREFIX}-trail", bucket=trail_bucket,
            enable_file_validation=True, include_global_service_events=True, is_multi_region_trail=True,
            management_events=cloudtrail.ReadWriteType.ALL, send_to_cloud_watch_logs=False)
        # The requests to an agent runtime, to the workspace and to the gateway are data events of AgentCore: the
        # event history does not have them. With them the trail has the start of a runtime session by the session
        # starter, and every request of an agent to its workspace, in a record that the agent does not write.
        # A trail has advanced event selectors or basic ones, not both.
        selector, field = cloudtrail.CfnTrail.AdvancedEventSelectorProperty, cloudtrail.CfnTrail.AdvancedFieldSelectorProperty

        def data_events(name: str, resource_type: str, **resources) -> cloudtrail.CfnTrail.AdvancedEventSelectorProperty:
            return selector(name=name, field_selectors=[
                field(field="eventCategory", equal_to=["Data"]), field(field="resources.type", equal_to=[resource_type]),
                *([field(field="resources.ARN", **resources)] if resources else [])])

        cfn_trail = trail.node.default_child
        cfn_trail.event_selectors = None
        cfn_trail.advanced_event_selectors = [
            selector(name="Management events", field_selectors=[field(field="eventCategory", equal_to=["Management"])]),
            data_events("Session table", "AWS::DynamoDB::Table", equal_to=[session_table.table_arn]),
            # An invocation of an alias has the ARN of the function with the alias at its end.
            data_events("Functions that decide an identity", "AWS::Lambda::Function", starts_with=[
                starter_fn.function_arn, broker_fn.function_arn, interceptor_fn.function_arn]),
            *[data_events(name, f"AWS::BedrockAgentCore::{resource_type}") for name, resource_type in (
                ("Agent runtimes", "Runtime"), ("Endpoints of the agent runtimes", "RuntimeEndpoint"),
                ("Workspace", "CodeInterpreterCustom"), ("Gateway", "Gateway"))],
        ]

        # --- Sessions from issues: a repository on GitHub starts a session of its use case ----------
        # A workflow of the repository gets AWS credentials with the OIDC token of GitHub Actions and invokes
        # the session starter. Each repository has its own role, and the role can invoke only the alias of
        # the session starter that has the name of the use case of the repository: the session starter reads
        # the use case from the alias. Only a workflow that runs from the branch main can use the role, so a
        # workflow file on a branch of a session cannot. The subject has the immutable IDs of the owner and of
        # the repository. The identity provider of GitHub Actions is a resource of the account, not of this
        # stack.
        github_roles, github_deploy_roles = {}, {}
        if github_owner:
            provider = f"arn:aws:iam::{self.account}:oidc-provider/{names.GITHUB_OIDC_HOST}"
            for use_case, repository_name in repository_names.items():
                if repository_name not in (github_subjects or {}):
                    continue
                alias = lambda_.Alias(self, f"StarterAlias-{use_case}", alias_name=use_case,
                                      version=starter_fn.current_version)
                role = iam.Role(
                    self, f"GitHubRole-{use_case}", role_name=f"{names.GITHUB_ROLE_PREFIX}{use_case}",
                    description=f"Workflow of the repository {repository_name} on GitHub: start a session",
                    assumed_by=iam.FederatedPrincipal(provider, {"StringEquals": {
                        f"{names.GITHUB_OIDC_HOST}:aud": "sts.amazonaws.com",
                        f"{names.GITHUB_OIDC_HOST}:sub": github_subjects[repository_name],
                    }}, "sts:AssumeRoleWithWebIdentity"))
                role.add_to_policy(iam.PolicyStatement(
                    actions=["lambda:InvokeFunction"], resources=[alias.function_arn]))
                github_roles[use_case] = role.role_arn

                # After a merge to main, a workflow of the repository deploys the live environment of the use
                # case and deletes the stack of the session of the merged pull request. The agent role cannot
                # do this: a session can touch only its own stack. The role can change one stack, the live
                # stack of its use case, and it can delete only a stack that has the tag of its repository.
                environment = f"{names.LIVE_ENVIRONMENT_PREFIX}{use_case}"
                deploy_role = iam.Role(
                    self, f"GitHubDeployRole-{use_case}", role_name=f"{names.GITHUB_DEPLOY_ROLE_PREFIX}{use_case}",
                    description=f"Workflow of the repository {repository_name} on GitHub: deploy after a merge",
                    assumed_by=iam.FederatedPrincipal(provider, {"StringEquals": {
                        f"{names.GITHUB_OIDC_HOST}:aud": "sts.amazonaws.com",
                        f"{names.GITHUB_OIDC_HOST}:sub": github_subjects[repository_name],
                    }}, "sts:AssumeRoleWithWebIdentity"))
                deploy_role.add_to_policy(iam.PolicyStatement(
                    sid="LiveStack",
                    actions=["cloudformation:CreateChangeSet", "cloudformation:DescribeChangeSet",
                             "cloudformation:ExecuteChangeSet", "cloudformation:DeleteChangeSet",
                             "cloudformation:DescribeStacks", "cloudformation:DescribeStackEvents",
                             "cloudformation:GetTemplateSummary", "cloudformation:TagResource",
                             "cloudformation:UntagResource"],
                    resources=[f"arn:aws:cloudformation:{self.region}:{self.account}:stack/{STACK_PREFIX}{environment}/*"]))
                deploy_role.add_to_policy(iam.PolicyStatement(
                    sid="LiveParameters",
                    actions=["ssm:PutParameter", "ssm:AddTagsToResource", "ssm:RemoveTagsFromResource",
                             "ssm:ListTagsForResource", "ssm:GetParameters", "ssm:DeleteParameter"],
                    resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter/agentfarm/{environment}/*"]))
                of_repository = {"StringEquals": {"aws:ResourceTag/repository": repository_name}}
                deploy_role.add_to_policy(iam.PolicyStatement(
                    sid="DeleteSessionStacks", actions=["cloudformation:DeleteStack"],
                    resources=[f"arn:aws:cloudformation:{self.region}:{self.account}:stack/{STACK_PREFIX}*/*"],
                    conditions=of_repository))
                # The status of a stack has no condition on the tag: after the deletion the stack has no tag
                # any more, and the workflow must see that the stack is not there.
                deploy_role.add_to_policy(iam.PolicyStatement(
                    sid="StatusOfStacks", actions=["cloudformation:DescribeStacks"],
                    resources=[f"arn:aws:cloudformation:{self.region}:{self.account}:stack/{STACK_PREFIX}*/*"]))
                deploy_role.add_to_policy(iam.PolicyStatement(
                    sid="DeleteSessionParameters",
                    actions=["ssm:DeleteParameter", "ssm:RemoveTagsFromResource", "ssm:ListTagsForResource",
                             "ssm:GetParameters"],
                    resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter/agentfarm/*"],
                    conditions=of_repository))
                github_deploy_roles[use_case] = deploy_role.role_arn

        first_agent = next(iter(agents))
        for key, value in {
            "Agents": self.to_json_string(agents),
            "Repositories": self.to_json_string(repository_names),
            "AgentId": first_agent,
            "AgentRuntimeArn": runtimes[first_agent].attr_agent_runtime_arn,
            "AgentRuntimeId": runtimes[first_agent].attr_agent_runtime_id,
            "ExecutionRoleArn": names.role_arn(self.account, f"{names.RUNTIME_TOKEN_ROLE_PREFIX}{first_agent}"),
            "AgentRoleArn": agent_role.role_arn,
            "BrokerRoleArn": broker_role.role_arn,
            "BrokerFunctionName": broker_fn.function_name,
            "BrokerLogGroupName": f"/aws/lambda/{PREFIX}-broker",
            "StarterFunctionName": starter_fn.function_name,
            "StarterLogGroupName": f"/aws/lambda/{PREFIX}-starter",
            "SessionTableName": session_table.table_name,
            "GatewayUrl": gateway.attr_gateway_url,
            "GatewayArn": gateway.attr_gateway_arn,
            "GatewayLogGroupName": gateway_log_group.log_group_name,
            "TrailArn": trail.trail_arn,
            "TrailName": f"{PREFIX}-trail",
            "TrailBucketName": trail_bucket.bucket_name,
            "CodeInterpreterId": sandbox.attr_code_interpreter_id,
            "CodeInterpreterArn": sandbox.attr_code_interpreter_arn,
            "WorkspaceNetworkMode": WORKSPACE_NETWORK,
            "BoxAgents": ",".join(agent for agent in agents if agent in (box_agents or [])) or "none",
            "BoxMode": box_mode if box_agents else "none",
            "GitHubAppKeys": self.to_json_string(app_key_ids) if app_key_ids else "none",
            "GitHubAppSettingsPrefix": names.GITHUB_APP_SETTINGS_PREFIX if app_key_ids else "none",
            "Audience": AUDIENCE,
            "Repository": repository_names[agents[first_agent][0]],
            "StackPrefix": STACK_PREFIX,
            "PlatformVersion": PLATFORM_VERSION,
            "ModelId": model_id,
            "GitHubRoles": self.to_json_string(github_roles),
            "GitHubDeployRoles": self.to_json_string(github_deploy_roles),
            "LiveEnvironmentPrefix": names.LIVE_ENVIRONMENT_PREFIX,
        }.items():
            CfnOutput(self, key, value=value)
