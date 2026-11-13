"""What the tests ask about a session of the deployed stack AgentFarmRuntime.

The policy of an agent runtime denies a request of every principal except the session starter. A test reads
the state of the work of a session through the session starter (the request `status`).
"""
import json


def session_status(functions, config: dict, session_id: str, agent_id: str | None = None) -> dict:
    """The state of the work of a session: {"state", "steps", ...}, or {"state": "unknown", "error": ...}.
    `functions`: an AWS Lambda client. `config`: the outputs of the stack. `agent_id`: the agent of the session,
    or the first agent of the stack."""
    response = functions.invoke(
        FunctionName=config["StarterFunctionName"],
        Payload=json.dumps({"action": "status", "agent_id": agent_id or config["AgentId"], "session_id": session_id}).encode())
    answer = json.loads(response["Payload"].read() or b"{}")
    if not isinstance(answer, dict) or "state" not in answer:
        return {"state": "unknown", "error": json.dumps(answer)[:300]}
    return answer
