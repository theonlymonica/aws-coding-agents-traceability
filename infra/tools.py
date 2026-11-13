"""The tools of the broker, as the tool schema of the Lambda target of the AgentCore Gateway.

The broker (broker/handler.py) answers these tools.
"""
from aws_cdk import aws_bedrockagentcore as agentcore

T = agentcore.CfnGatewayTarget
S = T.SchemaDefinitionProperty


def tool(name: str, description: str, input_schema=None):
    return T.ToolDefinitionProperty(name=name, description=description, input_schema=input_schema or S(type="object"))


def broker_tools() -> list:
    """The tools of the repository and of the stack, and `end_session`, with which a session ends itself."""
    return _tools() + [tool(
        "end_session", "End this session. After this request the system accepts no more requests of the session. "
        "The agent sends it at the end of its work.")]


def _tools() -> list:
    return [
        tool("list_files",
             "List all files of the repository of this session. The files come from the branch "
             "of this session if it exists, and from the branch main in the other case."),
        tool("read_file",
             "Read one text file of the repository of this session. The answer has the complete content, or "
             "`not_readable` with the reason and no content: a file that is not text in UTF-8, or a file that is too large.",
             S(type="object", required=["path"], properties={
                 "path": S(type="string", description="Path of the file in the repository.")})),
        tool("commit_changes",
             "Commit files to the branch of this session. The first commit creates the branch "
             "from the branch main. The repository, the branch and the author are fixed by the session.",
             S(type="object", required=["files", "message"], properties={
                 "files": S(type="array", description="The files to create, to replace or to delete.",
                            items=S(type="object", required=["path"], properties={
                                "path": S(type="string", description="Path of the file in the repository."),
                                "content": S(type="string", description="The complete content of a file to create or replace."),
                                "delete": S(type="boolean", description="true: delete this file. Then no content.")})),
                 "message": S(type="string", description="The commit message.")})),
        tool("open_pull_request",
             "Open a pull request from the branch of this session to the branch main.",
             S(type="object", required=["title"], properties={
                 "title": S(type="string", description="Title of the pull request."),
                 "description": S(type="string", description="Description of the pull request.")})),
        tool("deploy_stack",
             "Create or update the CloudFormation stack of this session from the file template.yaml "
             "of the last commit of this session. The stack name is fixed by the session. The "
             "template must declare a parameter SessionId of type String, and the broker sets its "
             "value. SSM parameter names must start with /agentfarm/<SessionId>/."),
        tool("describe_stack", "Return the status of the CloudFormation stack of this session."),
        tool("get_stack_events",
             "Return the most recent events of the CloudFormation stack of this session, with the "
             "reason of each failure."),
    ]
