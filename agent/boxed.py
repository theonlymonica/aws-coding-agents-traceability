"""The agent inside a box of Strands Box (the setting AGENT_BOX of an agent runtime).

Without this setting the HTTP server of the agent runtime runs the work of a session in a thread of its
own process, which has the credentials of the execution role and a path to the internet.

AGENT_BOX=agent: the complete agent runs inside one box. The entry point of the container starts `box run`
in its own place, and the box starts the agent: the HTTP server and the work of a session are one process
inside the box. The box listens on the port of AgentCore Runtime and relays the requests to the server
(`[[agent.ingress]]`), and its gateway signs with the credentials that the microVM gives (`aws://`, the
default credential chain). No process outside the box has a part of the agent. Both features are in the
code of Box after the release 0.2.2.

AGENT_BOX=work: the server stays outside the box, starts `box run` for the work of a session, and the work
runs as a second process inside the box. This mode needs no feature after the release 0.2.2:

  - the process of the work has placeholder AWS keys. The gateway of the box signs its requests to AWS
    STS, Amazon Bedrock and AgentCore with the credentials of the runtime session;
  - the process reaches the destinations of the policy (box/policy.dw.template) and no other address;
  - the server stays outside the box: it answers AgentCore Runtime, and it runs no text of the model and
    no file of a repository.

The release of Box signs only with the keys of a named AWS profile. The server writes the credentials of
the runtime session to a profile file that only the box process reads, and writes it again every 5 minutes.

The process of the work prints its state as lines `WORK-STATE <JSON>`; every other line is a line of its
log, and the server prints it to the log of the agent runtime.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse

import boto3

HERE = Path(__file__).resolve().parent
BOX = Path(os.environ.get("BOX_BINARY", "/opt/box/box"))
PROFILE = "runtime-session"
STATE_MARK = "WORK-STATE "
REFRESH_SECONDS = 300
AGENT_PORT = 8081   # the port of the agent inside the box of the mode `agent`; the box listens on 8080
# The settings of the agent that the process of the work needs.
SETTINGS = ("AGENT_HOST", "AGENTFARM_REGION", "AGENT_ID", "AUDIENCE", "REPOSITORY", "GATEWAY_URL", "MODEL_ID",
            "CODE_INTERPRETER_ID", "WORKSPACE_NETWORK", "AGENT_BOX")


def mode() -> str:
    """The setting of the agent runtime: `agent`, `work`, or no box."""
    setting = os.environ.get("AGENT_BOX", "")
    return setting if setting in ("agent", "work") else ""


def enabled() -> bool:
    """True in the server that starts a box for the work of a session."""
    return mode() == "work" and not os.environ.get("AGENT_IN_BOX")


def inside() -> bool:
    """True in the process of the work of the mode `work`, inside the box."""
    return os.environ.get("AGENT_IN_BOX") == "work"


def describe() -> dict:
    """What the process that does the work of a session has: for the answer to `status`. An access key ID is
    not a secret; the placeholder of a box starts with AKIAIOSFODNN."""
    key = os.environ.get("AWS_ACCESS_KEY_ID", "")
    return {"mode": os.environ.get("AGENT_IN_BOX") or "none", "access_key_id_of_the_process": key[:12] or None,
            "proxy_of_the_box": bool(os.environ.get("HTTPS_PROXY"))}


def _values(run_directory: Path, region: str, env: dict) -> dict:
    return {"__REGION__": region, "__RUN__": str(run_directory), "__APP__": str(HERE),
            "__GATEWAY_HOST__": urlparse(os.environ.get("GATEWAY_URL", "")).hostname or "no-gateway.invalid",
            "__PYTHON__": os.path.realpath(sys.executable), "__PREFIX__": sys.base_prefix, "__PROFILE__": PROFILE,
            "__ENV__": ", ".join(f"{name} = {json.dumps(value)}" for name, value in env.items())}


def _render(private: Path, values: dict, configuration: str) -> None:
    for template, target in (("policy.dw.template", "policy.dw"), (configuration, "box.toml")):
        text = (HERE / "box" / template).read_text()
        for key, value in values.items():
            text = text.replace(key, value)
        (private / target).write_text(text)


def _environment(run_directory: Path, region: str, inside_as: str) -> dict:
    """The variables of the process inside a box: the settings of the agent, and no credential."""
    return {**{name: os.environ.get(name, "") for name in SETTINGS},
            "AGENT_IN_BOX": inside_as, "AWS_REGION": region, "AWS_DEFAULT_REGION": region,
            "AWS_EC2_METADATA_DISABLED": "true", "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": f"{run_directory}/home", "TMPDIR": f"{run_directory}/tmp",
            "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"}


def start_agent(region: str) -> None:
    """The mode `agent`: put `box run` in the place of this process. The box starts the agent (main.py) inside
    it, listens on port 8080 and relays the requests to the port of the agent."""
    run_directory = Path(tempfile.mkdtemp(prefix="bx", dir="/tmp"))   # a short path: the socket of the box is under it
    private = run_directory / "private"
    private.mkdir(mode=0o700)
    for name in ("work", "tmp", "home"):
        (run_directory / name).mkdir()
    env = {**_environment(run_directory, region, "agent"), "PORT": str(AGENT_PORT)}
    _render(private, _values(run_directory, region, env), "agent.toml.template")
    os.chdir(run_directory)
    # The box process reads the credentials of the microVM (the default credential chain). Its home directory has
    # no AWS files, and the agent inside the box cannot write there.
    # A microVM of AgentCore Runtime gives its credentials at the metadata endpoint, and the container has no
    # AWS key in a variable. A container on a workstation has them in variables: the box process gets those.
    keys = {name: os.environ[name] for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
            if os.environ.get(name)}
    os.execve(str(BOX), [str(BOX), "run", "--config", str(private / "box.toml")],
              {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(private), "EXPERIMENTAL_LINUX_SUPPORT": "TRUE",
               "AWS_REGION": region, "AWS_DEFAULT_REGION": region, **keys})


def emit(state: dict) -> None:
    """The process of the work tells the server its state."""
    print(STATE_MARK + json.dumps(state, default=str), flush=True)


def _write_credentials(private: Path, region: str) -> bool:
    """The credentials of the runtime session, as a profile file for the box process. A boto3 session of this
    call: the credentials of a process that started before the snapshot have no session context."""
    credentials = boto3.Session().get_credentials()
    if credentials is None:
        return False
    frozen = credentials.get_frozen_credentials()
    text = (f"[{PROFILE}]\naws_access_key_id = {frozen.access_key}\naws_secret_access_key = {frozen.secret_key}\n"
            + (f"aws_session_token = {frozen.token}\n" if frozen.token else ""))
    new = private / "credentials.new"
    new.touch(mode=0o600)
    new.write_text(text)
    os.replace(new, private / "credentials")   # the box process reads the file for every request
    (private / "config").write_text(f"[profile {PROFILE}]\nregion = {region}\n")
    return True


def run(request: dict, region: str, apply_state, log, timeout_seconds: int) -> int | None:
    """Run the work of a session inside a box. `request`: the arguments of the work. `apply_state`: called with
    every state that the process of the work prints. Return the exit code of `box run`, or None."""
    run_directory = Path(tempfile.mkdtemp(prefix="bx", dir="/tmp"))   # a short path: the socket of the box is under it
    private = run_directory / "private"
    private.mkdir(mode=0o700)
    for name in ("work", "tmp", "home"):
        (run_directory / name).mkdir()
    stop = threading.Event()
    try:
        if not _write_credentials(private, region):
            log("box_error", detail="the runtime session has no credentials")
            return None

        def refresh() -> None:
            while not stop.wait(REFRESH_SECONDS):
                try:
                    _write_credentials(private, region)
                except Exception as error:   # the next turn writes the file again
                    log("box_credentials_error", error=type(error).__name__)
        threading.Thread(target=refresh, daemon=True).start()

        (run_directory / "work" / "request.json").write_text(json.dumps(request))
        _render(private, _values(run_directory, region, _environment(run_directory, region, "work")), "box.toml.template")

        process = subprocess.Popen(
            [str(BOX), "run", "--config", str(private / "box.toml")], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=str(run_directory),
            # The box process reads the profile file. The process of the work gets the variables of box.toml only.
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(private), "EXPERIMENTAL_LINUX_SUPPORT": "TRUE",
                 "AWS_SHARED_CREDENTIALS_FILE": str(private / "credentials"), "AWS_CONFIG_FILE": str(private / "config")})

        def errors() -> None:
            lines = [line.rstrip() for line in process.stderr]
            log("box_run", lines=lines[-60:])
        reader = threading.Thread(target=errors, daemon=True)
        reader.start()
        timer = threading.Timer(timeout_seconds, process.kill)
        timer.start()
        try:
            for line in process.stdout:
                if line.startswith(STATE_MARK):
                    try:
                        apply_state(json.loads(line[len(STATE_MARK):]))
                    except ValueError:
                        pass
                else:
                    sys.stdout.write(line)
                    sys.stdout.flush()
            code = process.wait()
        finally:
            timer.cancel()
        reader.join(timeout=5)
        log("box_end", exit_code=code)
        return code
    finally:
        stop.set()
        shutil.rmtree(run_directory, ignore_errors=True)
