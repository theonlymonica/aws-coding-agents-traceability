"""Python packages for the workspace of a session.

The workspace has no path to the internet: code that runs there cannot send the code of the repository to
an address, and it cannot download anything. A build still needs its dependencies. The agent process, which
has a path to the internet, gets them:

  1. it accepts only names and versions of packages: no address, no option of pip, no file;
  2. it downloads them from one package index, and only as built packages (wheels), so that no code of a
     package runs in the microVM of the agent;
  3. it writes the files to the workspace, and the workspace installs them from these files.

A package of the index can still be harmful. It runs in the workspace, which has no credentials and no path
to the internet: it can change the work of the session, which a person reviews in the pull request.
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

INDEX_URL = "https://pypi.org/simple"
MAX_REQUIREMENTS = 40
MAX_BYTES = 300 * 1024 * 1024      # of all the files of one request
DOWNLOAD_SECONDS = 300
_VERSION = r"(?:==|~=|!=|<=|>=|<|>)\s*[A-Za-z0-9][A-Za-z0-9.*+!_-]*"
# One requirement: a name, optional extras, optional versions. `pytest`, `pytest>=8,<9`, `requests[socks]==2.32.3`.
REQUIREMENT = re.compile(
    rf"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?(?:\[[A-Za-z0-9][A-Za-z0-9._,-]*\])?(?:\s*{_VERSION}(?:\s*,\s*{_VERSION})*)?")


class Invalid(Exception):
    """A requirement that is not a name and a version of a package."""


def requirements(lines) -> list[str]:
    """The requirements of a list of lines. Raise Invalid for a line that is not one requirement."""
    found = []
    for line in lines:
        text = str(line).split("#", 1)[0].strip()   # a comment ends a line of a requirements file
        if not text:
            continue
        if not REQUIREMENT.fullmatch(text):
            raise Invalid(f"not a name and a version of a package: {text[:80]!r}. An address, an option of pip, a marker "
                          "and a file are not accepted")
        found.append(re.sub(r"\s+", "", text))
    if not found:
        raise Invalid("no package to install")
    if len(found) > MAX_REQUIREMENTS:
        raise Invalid(f"more than {MAX_REQUIREMENTS} requirements")
    return found


def box_options() -> list[str]:
    """Inside a box of Strands Box (boxed.py) the only path to the package index is the gateway of the box. The
    box gives its address and its certificate as variables of the process; pip gets them as options."""
    proxy, certificate = os.environ.get("HTTPS_PROXY", ""), os.environ.get("SSL_CERT_FILE", "")
    if not os.environ.get("AGENT_IN_BOX") or not proxy or not certificate:
        return []
    return ["--proxy", proxy, "--cert", certificate]


def download(specifications: list[str], python: tuple[int, int] = (3, 12), machine: str = "aarch64") -> tuple[dict[str, bytes], str]:
    """Download the built packages of the requirements and of their dependencies, for the Python of the workspace.
    Return ({file name: content}, output of pip). Raise Invalid if pip does not find them."""
    specifications = requirements(specifications)   # the caller checked them: never an unchecked text to pip
    with tempfile.TemporaryDirectory() as directory:
        command = [
            sys.executable, "-m", "pip", "download", "--isolated", "--no-input", "--disable-pip-version-check", "--quiet",
            "--only-binary=:all:", "--index-url", INDEX_URL, "--dest", directory,
            "--python-version", f"{python[0]}.{python[1]}", "--implementation", "cp", "--abi", f"cp{python[0]}{python[1]}",
            *[option for platform in (f"manylinux_2_28_{machine}", f"manylinux_2_17_{machine}", f"manylinux2014_{machine}",
                                      f"linux_{machine}") for option in ("--platform", platform)],
            *box_options(), "--", *specifications]
        # No setting of the environment reaches pip: `--isolated` ignores the variables and the files of a user.
        done = subprocess.run(command, capture_output=True, text=True, timeout=DOWNLOAD_SECONDS, check=False,
                              env={"PATH": os.environ.get("PATH", ""), "HOME": directory}, cwd=directory)
        output = (done.stdout + done.stderr).strip()[-2000:]
        if done.returncode != 0:
            raise Invalid(f"pip did not find the packages as built packages for the workspace: {output}")
        files, size = {}, 0
        for path in sorted(Path(directory).glob("*.whl")):
            size += path.stat().st_size
            if size > MAX_BYTES:
                raise Invalid(f"the packages are larger than {MAX_BYTES // (1024 * 1024)} MB")
            files[path.name] = path.read_bytes()
        if not files:
            raise Invalid("pip downloaded no package")
        return files, output
