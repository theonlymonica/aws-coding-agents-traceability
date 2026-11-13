"""The workspace of a session: a session of the Amazon Bedrock AgentCore Code Interpreter.

The code of a session runs here, not in the microVM of the agent. The code interpreter of the
system has no execution role, so the workspace has no AWS credentials, and the agent never sends
the JWT of the session to it. The repository reaches the workspace as files that the agent writes,
and a change leaves it as files that the agent reads: the workspace has no git credentials.

The network mode of the code interpreter is SANDBOX: the workspace has no path to the internet. A build gets
its dependencies as files that the agent writes (agent/packages.py).
"""
import json

from botocore.config import Config
from botocore.exceptions import ClientError

DIRECTORY = "repo"            # the directory of the repository in the workspace
PACKAGES = "packages"         # the directory of the package files, beside the repository: not a part of a commit
MAX_OUTPUT = 6000             # characters of the output of a command that the model gets
SESSION_SECONDS = 3600

# The name resolution probe: a program for the Python of the workspace. It prints one line, PROBE_MARK and JSON.
#   names      two names of the internet, asked of the resolver of the system: a list of addresses, "NXDOMAIN"
#              (the resolver answers that the name does not exist) or "NO-ANSWER"
#   new_name   a name that no resolver has in its cache: an address here is a resolver that answers every name
#   resolvers  the name servers of /etc/resolv.conf, each with its answer to one DNS question for example.com
#   public     the same question (UDP, port 53) to two public resolvers, without the resolver of the system
PROBE_MARK = "NAME-RESOLUTION "
PROBE_NAMES = ("pypi.org", "example.com")
NAME_RESOLUTION_PROBE = r"""
import json, socket, threading, uuid

def addresses(name):
    try:
        return sorted({item[4][0] for item in socket.getaddrinfo(name, 443, proto=socket.IPPROTO_TCP)})[:3]
    except socket.gaierror as error:
        return {socket.EAI_NONAME: "NXDOMAIN", socket.EAI_AGAIN: "NO-ANSWER"}.get(error.errno, "ERROR %s" % error.errno)
    except OSError as error:
        return "ERROR " + type(error).__name__

def question(server):
    sent = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.settimeout(5)
            udp.sendto(sent, (server, 53))
            answer = udp.recv(512)
        if len(answer) < 12 or answer[:2] != sent[:2]:
            return "OTHER"
        return "ANSWER rcode %d, %d addresses" % (answer[3] & 15, int.from_bytes(answer[6:8], "big"))
    except OSError as error:
        return "NO-ANSWER " + type(error).__name__

def resolvers():
    try:
        with open("/etc/resolv.conf") as file:
            return [line.split()[1] for line in file if line.startswith("nameserver") and len(line.split()) > 1][:3]
    except OSError:
        return []

NAMES, PUBLIC, LOCAL = ("pypi.org", "example.com"), ("1.1.1.1", "8.8.8.8"), resolvers()
result = {"names": {}, "new_name": "TIMEOUT", "resolvers": {}, "public": {}}
def ask(name):
    result["names"][name] = addresses(name)
def ask_new():
    result["new_name"] = addresses(uuid.uuid4().hex + ".example.com")
def ask_server(group, server):
    result[group][server] = question(server)
threads = [threading.Thread(target=ask, args=(name,), daemon=True) for name in NAMES]
threads += [threading.Thread(target=ask_new, daemon=True)]
threads += [threading.Thread(target=ask_server, args=("resolvers", server), daemon=True) for server in LOCAL]
threads += [threading.Thread(target=ask_server, args=("public", server), daemon=True) for server in PUBLIC]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join(20)
for name in NAMES:
    result["names"].setdefault(name, "TIMEOUT")
for group, servers in (("resolvers", LOCAL), ("public", PUBLIC)):
    for server in servers:
        result[group].setdefault(server, "TIMEOUT")
print("NAME-RESOLUTION " + json.dumps(result))
"""


def resolves_names(probe: dict) -> bool | None:
    """The result of the name resolution probe. True: the workspace got an address for a name, or the answer
    of a public resolver. False: no address for the two names and for the new name, and no answer of a public
    resolver. None: the probe is not complete. The answer NXDOMAIN proves nothing: a resolver in the workspace
    that knows no name gives it too."""
    names, public = probe.get("names") or {}, probe.get("public") or {}
    asked = [names.get(name) for name in PROBE_NAMES] + [probe.get("new_name")]
    if any(isinstance(answer, list) and answer for answer in asked) or any(
            str(answer).startswith(("ANSWER", "OTHER")) for answer in public.values()):
        return True
    complete = all(answer in ("NXDOMAIN", "NO-ANSWER") for answer in asked) and len(public) >= 2 and all(
        str(answer).startswith("NO-ANSWER") for answer in public.values())
    return False if complete else None


class Workspace:
    def __init__(self, session, region: str, code_interpreter_id: str) -> None:
        """`session`: a boto3 session with the credentials of the runtime session."""
        self._client = session.client("bedrock-agentcore", region_name=region, config=Config(
            read_timeout=600, retries={"total_max_attempts": 2}))
        self._id, self._session = code_interpreter_id, None

    def _invoke(self, name: str, **arguments) -> dict:
        if not self._session:
            self._session = self._client.start_code_interpreter_session(
                codeInterpreterIdentifier=self._id, name="workspace", sessionTimeoutSeconds=SESSION_SECONDS)["sessionId"]
        try:
            answer = self._client.invoke_code_interpreter(
                codeInterpreterIdentifier=self._id, sessionId=self._session, name=name, arguments=arguments)
            result = next((event["result"] for event in answer["stream"] if "result" in event), None)
        except ClientError as error:
            return {"error": error.response["Error"]["Code"], "detail": error.response["Error"]["Message"][:400]}
        if result is None:
            return {"error": "no_result"}
        return result

    def run(self, command: str, timeout_seconds: int = 300) -> dict:
        """Run a shell command in the directory of the repository. Return its exit code and its output."""
        result = self._invoke("executeCommand", command=f"mkdir -p {DIRECTORY} && cd {DIRECTORY} && "
                                                        f"timeout {int(timeout_seconds)} sh -c {_quoted(command)} 2>&1")
        if "error" in result:
            return result
        structured = result.get("structuredContent") or {}
        output = structured.get("stdout") or "\n".join(
            c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        if len(output) > MAX_OUTPUT:   # the start and the end of a long output
            output = output[:MAX_OUTPUT // 2] + "\n... [output shortened] ...\n" + output[-MAX_OUTPUT // 2:]
        return {"exit_code": structured.get("exitCode"), "output": output}

    def write(self, files: dict[str, str]) -> dict:
        """Write text files. `files`: {path in the repository: content}."""
        result = self._invoke("writeFiles", content=[
            {"path": f"{DIRECTORY}/{path.lstrip('/')}", "text": content} for path, content in files.items()])
        return {"error": result["error"], "detail": result.get("detail")} if "error" in result else \
            {"written": sorted(files), "is_error": bool(result.get("isError"))}

    def read(self, path: str) -> dict:
        """Read one text file of the repository in the workspace."""
        result = self._invoke("readFiles", paths=[f"{DIRECTORY}/{path.lstrip('/')}"])
        if "error" in result:
            return result
        for item in result.get("content", []):
            resource = item.get("resource") or {}
            if "text" in resource:
                return {"path": path, "content": resource["text"]}
        return {"error": "not_found", "detail": json.dumps(result.get("content"), default=str)[:300]}

    def python(self) -> tuple[tuple[int, int], str]:
        """The Python of the workspace: ((major, minor), machine). Python 3.12 on aarch64 if it does not answer."""
        answer = self.run("python3 -c 'import sys, platform; print(sys.version_info[0], sys.version_info[1], platform.machine())'")
        words = str(answer.get("output", "")).split()
        if len(words) >= 3 and words[0].isdigit() and words[1].isdigit():
            return (int(words[0]), int(words[1])), words[2]
        return (3, 12), "aarch64"

    def reaches_the_internet(self) -> bool | None:
        """True if a request from the workspace to an address on the internet gets an answer. None: no result."""
        answer = self.run(
            "python3 -c \"import urllib.request\nfor url in ('https://pypi.org/simple/', 'https://example.com'):\n"
            "    try:\n        urllib.request.urlopen(url, timeout=8)\n        print('REACHED', url)\n"
            "    except Exception as error:\n        print('NO-PATH', type(error).__name__)\"", timeout_seconds=40)
        output = str(answer.get("output", ""))
        if "REACHED" in output:
            return True
        return False if output.count("NO-PATH") == 2 else None

    def name_resolution(self) -> dict:
        """Does the workspace resolve a name of the internet? A workspace that resolves names has a path out
        without a connection: a program can put data in the names that it asks for.
        Returns {"resolves": True, False or None (no result), and the answers of the probe}: see resolves_names."""
        answer = self.run("python3 -c " + _quoted(NAME_RESOLUTION_PROBE), timeout_seconds=40)
        line = next((line for line in str(answer.get("output", "")).splitlines() if line.startswith(PROBE_MARK)), None)
        if line is None:
            return {"resolves": None, "detail": str(answer.get("output") or answer.get("error") or "")[:300]}
        try:
            probe = json.loads(line[len(PROBE_MARK):])
        except ValueError:
            return {"resolves": None, "detail": line[:300]}
        return {"resolves": resolves_names(probe), **probe} if isinstance(probe, dict) else {"resolves": None}

    def install(self, files: dict[str, bytes], specifications: list[str]) -> dict:
        """Write package files beside the repository, and install the requirements from these files only.
        `specifications`: checked requirements (agent/packages.py)."""
        for name, content in files.items():   # one request for each file: a package can be large
            result = self._invoke("writeFiles", content=[{"path": f"{PACKAGES}/{name}", "blob": content}])
            if "error" in result or result.get("isError"):
                return {"error": "the package file was not written", "file": name, "detail": str(result)[:300]}
        quoted = " ".join(_quoted(specification) for specification in specifications)
        return self.run(f"python3 -m pip install --no-index --find-links ../{PACKAGES} --disable-pip-version-check {quoted}")

    def stop(self) -> None:
        if self._session:
            try:
                self._client.stop_code_interpreter_session(codeInterpreterIdentifier=self._id, sessionId=self._session)
            except ClientError:
                pass
            self._session = None


def _quoted(command: str) -> str:
    """A shell command as one single-quoted argument."""
    return "'" + command.replace("'", "'\"'\"'") + "'"
