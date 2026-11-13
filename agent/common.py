"""Shared helpers of the agent container."""
import json

MAX_LOG_FIELD = 3000   # one log line stays small enough for one CloudWatch log event


def log(event: str, **fields) -> None:
    """One JSON line per step. The log of the agent runtime is the transcript of the session."""
    def short(value):
        text = value if isinstance(value, str) else json.dumps(value)
        return value if len(text) <= MAX_LOG_FIELD else text[:MAX_LOG_FIELD] + " ...[truncated]"
    print(json.dumps({"event": event, **{k: short(v) for k, v in fields.items()}}), flush=True)
