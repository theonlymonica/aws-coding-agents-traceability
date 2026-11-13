"""The loop of an LLM agent with the Amazon Bedrock Converse API and tool use.

The caller gives the tools of the model and one function that runs a tool. The loop sends the task
to the model, runs the tools that the model asks for, and ends when the model ends its answer.
"""
import json

from botocore.exceptions import ClientError

MAX_TURNS = 60


def run(bedrock, model_id: str, system: str, task: str, tools: list[dict], call_tool, log,
        max_turns: int = MAX_TURNS) -> dict:
    """`tools`: [{"name", "description", "inputSchema"}]. `call_tool(name, arguments)` returns (status, text).

    Return {"stop_reason", "turns", "final_text"}. `stop_reason` is the stop reason of the last answer of
    the model, `model_error` or `turn limit reached`.
    """
    tool_config = {"tools": [{"toolSpec": {"name": t["name"], "description": t["description"],
                                           "inputSchema": {"json": t["inputSchema"]}}} for t in tools]}
    messages = [{"role": "user", "content": [{"text": f"Your task:\n\n{task}"}]}]
    final_text = ""
    for turn in range(1, max_turns + 1):
        try:
            response = bedrock.converse(modelId=model_id, system=[{"text": system}], messages=messages,
                                        toolConfig=tool_config, inferenceConfig={"maxTokens": 4096})
        except ClientError as error:
            log("model_error", turn=turn, detail=str(error)[:600])
            return {"stop_reason": "model_error", "turns": turn, "final_text": final_text}
        message, stop_reason = response["output"]["message"], response["stopReason"]
        messages.append(message)   # unchanged, including reasoning blocks
        for block in message["content"]:
            if block.get("text", "").strip():
                final_text = block["text"]
                log("model_text", turn=turn, text=block["text"])
        if stop_reason != "tool_use":
            log("session_end", turn=turn, stop_reason=stop_reason, usage=response.get("usage"))
            return {"stop_reason": stop_reason, "turns": turn, "final_text": final_text}
        results = []
        for block in message["content"]:
            if "toolUse" not in block:
                continue
            call = block["toolUse"]
            name, arguments = call["name"], call.get("input") or {}
            log("tool_call", turn=turn, tool=name, arguments=arguments)
            try:
                status, output = call_tool(name, arguments)
            except (KeyError, TypeError, ValueError) as error:
                status, output = "error", f"{type(error).__name__}: {error}"
            log("tool_result", turn=turn, tool=name, status=status, output=output)
            results.append({"toolResult": {"toolUseId": call["toolUseId"], "status": status,
                                           "content": [{"text": output or "(empty)"}]}})
        messages.append({"role": "user", "content": results})
    log("session_end", turn=max_turns, stop_reason="turn limit reached")
    return {"stop_reason": "turn limit reached", "turns": max_turns, "final_text": final_text}


def as_text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value)
