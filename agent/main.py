"""Entry point of the agent container: the HTTP server of an agent runtime of Amazon Bedrock AgentCore Runtime
(runtime_server.py).

For an agent with a box of Strands Box in the mode `agent` (boxed.py), `box run` takes the place of this process
and starts main.py again inside the box.
"""
import os
import sys


def main() -> int:
    import boxed
    if boxed.mode() == "agent" and not os.environ.get("AGENT_IN_BOX"):
        # The complete agent inside a box: `box run` takes the place of this process and starts main.py again.
        boxed.start_agent(os.environ.get("AGENTFARM_REGION") or os.environ.get("AWS_REGION", ""))
    import runtime_server
    return runtime_server.serve()


if __name__ == "__main__":
    sys.exit(main())
