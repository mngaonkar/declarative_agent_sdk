"""
Machine driver — provisions agents on a registered macOS/Linux box.

Everything here is a thin wrapper over ``packaging/deploy/install.sh`` and
``uninstall.sh``, which were written to be driven by a program: non-interactive,
progress on stderr, one JSON object on stdout with ``--json``. The driver's only
jobs are to get those scripts onto the machine, to pass flags, and to keep
secrets off the command line.

Transport is plain SSH, by design. The agents' supervisord listens on a unix
socket with no network listener (``packaging/deploy/README.md`` rejects
``inet_http_server`` because it has no authentication), so there is nothing to
dial remotely even if we wanted to — and SSH means no new privileged daemon on
every box.

Secrets never reach argv. The provider key and the control-plane handshake are
written to a 0600 file on the target over the SSH *stdin* channel, handed to
``install.sh --env-file`` (which copies it to the agent's ``.env`` at 0600), and
then removed.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from declarative_agent_sdk.core.agent_logging import get_logger

logger = get_logger(__name__)

# Where the driver stages its helpers on a target machine.
#
# $HOME rather than ~, because these paths are also passed on to install.sh as
# argument *values*: no shell expands a tilde inside quotes, so install.sh would
# be handed a path beginning with a literal "~" and refuse to start. $HOME does
# expand inside double quotes, which also keeps a home directory with a space
# in it intact.
REMOTE_HOME = "$HOME/.agents-control"
REMOTE_BIN = f"{REMOTE_HOME}/bin"

PROVIDER_KEY_NAMES = {
    "google": "GOOGLE_API_KEY",
    "google_genai": "GOOGLE_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "litellm": "OPENAI_API_KEY",
    "vllm": "",  # local server, no key
}

# Marker between the two files read in one round trip. Long and unlikely enough
# that a config or an instruction file will not contain it.
_SPLIT = "---agents-control-split-8f3a9c---"
_IFACE_MARK = "---interfaces---"

# Every address a peer could dial, with the interface it belongs to. `ip` on
# Linux, `ifconfig` on macOS; both print one "iface address" pair per line, and
# both families are collected — an agent may well need to publish its IPv6.
#
# install.sh would otherwise advertise whichever address the kernel picks for
# the default route, and on a host with two external interfaces that is a coin
# flip: the agent then publishes a card nobody can reach.
_LIST_INTERFACES = (
    'if command -v ip >/dev/null 2>&1; then '
    "ip -o addr show scope global 2>/dev/null | "
    "awk '{split($4,a,\"/\"); print $2\" \"a[1]\" \"$0}'; "
    'else '
    "ifconfig -a 2>/dev/null | "
    "awk '/^[a-zA-Z0-9_.-]+:/{gsub(\":\",\"\",$1); i=$1} "
    "/[ \\t]inet6? /{print i\" \"$2\" \"$0}'; "
    'fi'
)

# Never worth publishing: nothing else can reach them.
_UNREACHABLE_PREFIXES = (
    "127.",        # IPv4 loopback
    "169.254.",    # IPv4 link-local
    "fe80:",       # IPv6 link-local
    "::1",         # IPv6 loopback
)

DEFAULT_SSH_OPTS = [
    "-o", "BatchMode=yes",              # never prompt for a password
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
]


class MachineError(RuntimeError):
    """A command on a target machine failed. Carries stderr for the UI."""

    def __init__(self, message: str, stderr: str = "", code: int = 1) -> None:
        super().__init__(message)
        self.stderr = stderr.strip()
        self.code = code

    def detail(self) -> str:
        return f"{self}\n{self.stderr}".strip()


def installer_paths() -> Dict[str, Path]:
    """
    Locate install.sh / uninstall.sh.

    ``AGENTS_CONTROL_DEPLOY_DIR`` wins, so a deployment can point at a patched
    pair; otherwise they come from this package (the repo root doubles as the
    package root, see ``[tool.setuptools.package-dir]``).
    """
    override = os.environ.get("AGENTS_CONTROL_DEPLOY_DIR")
    base = Path(override).expanduser() if override else (
        Path(__file__).resolve().parents[1] / "packaging" / "deploy"
    )
    paths = {"install": base / "install.sh", "uninstall": base / "uninstall.sh"}
    missing = [str(p) for p in paths.values() if not p.is_file()]
    if missing:
        raise MachineError(
            "deploy scripts not found: " + ", ".join(missing)
            + " — set AGENTS_CONTROL_DEPLOY_DIR to the directory holding "
              "install.sh and uninstall.sh"
        )
    return paths


class MachineDriver:
    """Runs provisioning commands locally or over SSH."""

    def __init__(self, default_timeout: int = 900) -> None:
        self.default_timeout = default_timeout
        self._pushed: set = set()  # machines whose scripts are already staged

    # ------------------------------------------------------------ transport

    @staticmethod
    def _is_local(machine: Dict[str, Any]) -> bool:
        return bool(machine.get("is_local")) or machine.get("host") in (
            "localhost", "127.0.0.1", "::1", ""
        )

    def _ssh_command(self, machine: Dict[str, Any]) -> List[str]:
        cmd = ["ssh", *DEFAULT_SSH_OPTS]
        key = str(machine.get("ssh_key") or "")
        if key:
            cmd += ["-i", str(Path(key).expanduser())]
        port = int(machine.get("ssh_port") or 22)
        if port != 22:
            cmd += ["-p", str(port)]
        user = str(machine.get("ssh_user") or "")
        host = str(machine.get("host") or "")
        cmd.append(f"{user}@{host}" if user else host)
        return cmd

    def run(
        self,
        machine: Dict[str, Any],
        script: str,
        stdin: Optional[str] = None,
        timeout: Optional[int] = None,
        stdin_bytes: Optional[bytes] = None,
    ) -> str:
        """
        Run a shell snippet on the machine; return stdout or raise.

        ``stdin_bytes`` sends a payload byte for byte — needed for a wheel,
        which cannot survive being decoded and re-encoded as text.
        """
        if self._is_local(machine):
            argv = ["bash", "-lc", script]
        else:
            argv = self._ssh_command(machine) + [script]

        logger.debug(f"[{machine.get('name')}] {script[:200]}")
        binary = stdin_bytes is not None
        try:
            proc = subprocess.run(
                argv,
                input=stdin_bytes if binary else stdin,
                capture_output=True,
                text=not binary,
                timeout=timeout or self.default_timeout,
            )
            if binary:
                # Normalise to the text result every caller expects.
                proc = subprocess.CompletedProcess(
                    proc.args, proc.returncode,
                    proc.stdout.decode("utf-8", "replace"),
                    proc.stderr.decode("utf-8", "replace"),
                )
        except FileNotFoundError as exc:          # no ssh / no bash
            raise MachineError(f"cannot run command: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise MachineError(
                f"timed out after {exc.timeout:.0f}s on '{machine.get('name')}'"
            ) from exc

        if proc.returncode != 0:
            # install.sh exits 3 when the agent installed but cannot start —
            # that is a result, not a transport failure, so it is handled by
            # the caller which still gets the JSON on stdout.
            if proc.returncode == 3 and proc.stdout.strip():
                return proc.stdout
            raise MachineError(
                f"command failed on '{machine.get('name')}' (exit {proc.returncode})",
                stderr=proc.stderr,
                code=proc.returncode,
            )
        return proc.stdout

    # ------------------------------------------------------------- staging

    def probe(self, machine: Dict[str, Any]) -> Dict[str, Any]:
        """
        Check reachability and prerequisites, and list the usable addresses.

        The addresses are collected here so the create form can offer them: on
        a multi-homed host, which one an agent publishes is a choice, not
        something to be guessed from the default route.
        """
        try:
            out = self.run(
                machine,
                "uname -s; command -v uv >/dev/null 2>&1 && echo uv-ok || echo uv-missing; "
                f"echo {_IFACE_MARK}; {_LIST_INTERFACES}",
                timeout=30,
            )
        except MachineError as exc:
            return {"ok": False, "os": "", "uv": False, "interfaces": [],
                    "error": exc.detail()}

        head, _, iface_block = out.partition(_IFACE_MARK)
        lines = [line.strip() for line in head.splitlines() if line.strip()]
        os_kind = lines[0] if lines else ""
        has_uv = "uv-ok" in lines
        interfaces = _parse_interfaces(iface_block)

        # If the listing came back empty or missed it, the address this driver
        # reaches the machine on is certainly reachable — offer that at least.
        host = str(machine.get("host") or "")
        if host and not self._is_local(machine):
            if not any(i["address"] == host for i in interfaces):
                interfaces.append({
                    "name": "ssh", "address": host, "note": "",
                    "family": "ipv6" if ":" in host else "ipv4",
                })

        return {
            "ok": True,
            "os": os_kind,
            "uv": has_uv,
            "interfaces": interfaces,
            "error": "" if has_uv else (
                "uv is not installed on this machine — install.sh requires it "
                "(https://astral.sh/uv); pip cannot resolve the SDK's dependencies"
            ),
        }

    def push_scripts(self, machine: Dict[str, Any], force: bool = False) -> None:
        """Stage install.sh / uninstall.sh under ~/.agents-control/bin."""
        key = str(machine.get("id") or machine.get("name"))
        if key in self._pushed and not force:
            return
        if self._is_local(machine):
            self._pushed.add(key)
            return

        paths = installer_paths()
        self.run(machine, f'umask 077; mkdir -p "{REMOTE_BIN}"', timeout=60)
        for kind, path in paths.items():
            target = f'"{REMOTE_BIN}/{kind}.sh"'
            self.run(
                machine,
                f"umask 077; cat > {target} && chmod 700 {target}",
                stdin=path.read_text(),
                timeout=120,
            )
        self._pushed.add(key)
        logger.info(f"staged deploy scripts on '{machine.get('name')}'")

    def _script_path(self, machine: Dict[str, Any], kind: str) -> str:
        """A shell-ready token for the staged script, already quoted."""
        if self._is_local(machine):
            return shlex.quote(str(installer_paths()[kind]))
        return f'"{REMOTE_BIN}/{kind}.sh"'

    def _staged_env_path(self, machine: Dict[str, Any], name: str) -> tuple:
        """
        (directory, file) for the staged env file, as shell-ready tokens.

        Both are returned double-quoted: the remote form has to expand $HOME,
        and either form may sit under a path containing a space. install.sh
        restricts the agent name to [a-z0-9_-], so nothing else needs escaping.
        """
        if self._is_local(machine):
            directory = Path(os.path.expanduser("~")) / ".agents-control"
            return f'"{directory}"', f'"{directory / ("env." + name)}"'
        return f'"{REMOTE_HOME}"', f'"{REMOTE_HOME}/env.{name}"'

    # ------------------------------------------------- the SDK an agent runs

    def build_wheel(self, force: bool = False) -> Path:
        """
        Build a wheel from the working tree this control plane is running.

        ``install.sh`` installs the SDK from GitHub by default, so an agent
        created from unpushed code would silently run whatever is on the
        branch instead. Building and shipping a wheel closes that gap without
        anything being committed, which is the normal state of affairs while
        the board and the agents are being worked on together.
        """
        root = Path(__file__).resolve().parents[1]
        out = Path(os.path.expanduser("~")) / ".agents-control" / "wheels"
        out.mkdir(parents=True, exist_ok=True)

        if force:
            for stale in out.glob("*.whl"):
                stale.unlink()

        tool = shutil.which("uv")
        if tool is None:
            raise MachineError(
                "uv is needed to build a wheel from the working tree "
                "(https://astral.sh/uv), or create the agent from git instead"
            )
        try:
            proc = subprocess.run(
                [tool, "build", "--wheel", "--out-dir", str(out), str(root)],
                capture_output=True, text=True, timeout=600,
                # uv build runs from a temp dir; the repo root holds a `build`
                # directory that shadows the `build` module for anything that
                # imports it, so keep the cwd elsewhere.
                cwd=str(out),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MachineError(f"could not build a wheel: {exc}") from exc
        if proc.returncode != 0:
            raise MachineError("wheel build failed", stderr=proc.stderr)

        wheels = sorted(out.glob("*.whl"), key=lambda p: p.stat().st_mtime)
        if not wheels:
            raise MachineError("wheel build produced nothing", stderr=proc.stdout)
        logger.info(f"built {wheels[-1].name} from {root}")
        return wheels[-1]

    def push_wheel(self, machine: Dict[str, Any], wheel: Path) -> str:
        """Copy a wheel to the machine; returns its path there, shell-quoted."""
        if self._is_local(machine):
            return shlex.quote(str(wheel))

        target_dir = f"{REMOTE_HOME}/wheels"
        target = f'"{target_dir}/{wheel.name}"'
        self.run(machine, f'umask 077; mkdir -p "{target_dir}"', timeout=60)
        # Through SSH stdin, like everything else here: no scp, no second
        # authentication path, and it works wherever `ssh` already works.
        self.run(machine, f"cat > {target}", stdin_bytes=wheel.read_bytes(), timeout=600)
        size = self.run(machine, f"wc -c < {target}", timeout=60).strip()
        if int(size or 0) != wheel.stat().st_size:
            raise MachineError(
                f"wheel arrived truncated ({size} of {wheel.stat().st_size} bytes)"
            )
        logger.info(f"sent {wheel.name} to '{machine.get('name')}' ({size} bytes)")
        return target

    # --------------------------------------------------------------- agents

    def list_agents(self, machine: Dict[str, Any]) -> List[Dict[str, Any]]:
        """``install.sh --list`` — JSON array of .agent-meta records."""
        self.push_scripts(machine)
        out = self.run(
            machine, f"{self._script_path(machine, 'install')} --list", timeout=120
        )
        return _parse_json(out, expect_list=True) or []

    def create_agent(
        self,
        machine: Dict[str, Any],
        *,
        name: str,
        framework: str = "lean",
        provider: str = "openai",
        model: str = "",
        endpoint: str = "",
        description: str = "",
        instructions: str = "",
        tools: str = "",
        approval: bool = True,
        api_key: str = "",
        control_env: Optional[Dict[str, str]] = None,
        bind_host: str = "",
        advertise_host: str = "",
        sdk_source: str = "git",
        sdk_ref: str = "",
        timeout: int = 900,
    ) -> Dict[str, Any]:
        """
        Provision one agent. Returns install.sh's JSON result.

        ``ready: false`` with ``problems`` is a normal outcome (a missing
        credential, say) and is reported, not raised.

        ``sdk_source`` picks the code the agent will run: "git" installs from
        the repository (optionally at ``sdk_ref``), "local" builds a wheel from
        this working tree and sends it. Use "local" while the board and the
        agents are being changed together, which is when the two are most
        likely to disagree.
        """
        self.push_scripts(machine)
        script = self._script_path(machine, "install")

        args = ["--name", name, "--framework", framework, "--provider", provider, "--json"]
        if model:
            args += ["--model", model]
        if endpoint:
            args += ["--endpoint", endpoint]
        if description:
            args += ["--description", description]
        if instructions:
            args += ["--instructions", instructions]
        if tools:
            args += ["--tools", tools]
        if bind_host:
            args += ["--host", bind_host]
        if advertise_host:
            # Without this install.sh advertises whatever the default route
            # resolves to, which on a multi-homed host may be an address the
            # board cannot reach.
            args += ["--advertise-host", advertise_host]
        if not approval:
            args.append("--no-approval")

        wheel_arg = ""
        if sdk_source == "local":
            wheel_arg = self.push_wheel(machine, self.build_wheel())
        elif sdk_ref:
            args += ["--ref", sdk_ref]

        env_lines = _env_file_lines(provider, api_key, control_env)
        env_path = ""
        if env_lines:
            env_dir, env_path = self._staged_env_path(machine, name)
            self.run(
                machine,
                f"umask 077; mkdir -p {env_dir} && cat > {env_path}",
                stdin="\n".join(env_lines) + "\n",
                timeout=60,
            )

        command = f"{script} {' '.join(shlex.quote(a) for a in args)}"
        if wheel_arg:
            command += f" --source wheel --wheel {wheel_arg}"
        # Appended already quoted, deliberately *not* through shlex.quote: that
        # would single-quote the path, and no shell expands $HOME or ~ inside
        # single quotes. install.sh would then be told to read a file that does
        # not exist and would stop with "--env-file not found".
        if env_path:
            command += f" --env-file {env_path}"
        try:
            out = self.run(machine, command, timeout=timeout)
        finally:
            if env_path:
                # The key now lives in the agent's own 0600 .env; this copy goes.
                try:
                    self.run(machine, f"rm -f {env_path}", timeout=30)
                except MachineError as exc:
                    logger.warning(f"could not remove staged env file: {exc}")

        result = _parse_json(out) or {}
        if not result:
            raise MachineError(f"install.sh returned no JSON for agent '{name}'", stderr=out)
        return result

    # ------------------------------------------------- an agent's own config

    def read_agent_files(
        self, machine: Dict[str, Any], prefix: str, timeout: int = 60
    ) -> Dict[str, str]:
        """
        Read ``agent.yaml`` and ``instructions.md`` from an installed agent.

        Both come back in one round trip, split on a sentinel, because a second
        SSH connection costs more than the bytes do.
        """
        prefix = _require_prefix(prefix)
        quoted = shlex.quote(prefix)
        out = self.run(
            machine,
            f"cat {quoted}/agent.yaml; printf '\\n{_SPLIT}\\n'; "
            f"cat {quoted}/instructions.md 2>/dev/null || true",
            timeout=timeout,
        )
        config, _, instructions = out.partition(_SPLIT)
        return {"yaml": config.strip("\n"), "instructions": instructions.strip("\n")}

    def write_agent_files(
        self,
        machine: Dict[str, Any],
        prefix: str,
        yaml_text: Optional[str] = None,
        instructions: Optional[str] = None,
        timeout: int = 120,
    ) -> Dict[str, Any]:
        """
        Replace an agent's config files.

        The previous agent.yaml is kept as ``agent.yaml.bak`` — a bad edit here
        stops the agent from starting at all, and the file may be the only
        record of how it was set up.
        """
        prefix = _require_prefix(prefix)
        quoted = shlex.quote(prefix)
        written = []
        if yaml_text is not None:
            self.run(
                machine,
                f"umask 077; cp -p {quoted}/agent.yaml {quoted}/agent.yaml.bak "
                f"2>/dev/null || true; cat > {quoted}/agent.yaml",
                stdin=yaml_text if yaml_text.endswith("\n") else yaml_text + "\n",
                timeout=timeout,
            )
            written.append("agent.yaml")
        if instructions is not None:
            self.run(
                machine,
                f"umask 077; cat > {quoted}/instructions.md",
                stdin=instructions if instructions.endswith("\n") else instructions + "\n",
                timeout=timeout,
            )
            written.append("instructions.md")
        return {"written": written, "backup": f"{prefix}/agent.yaml.bak"}

    def supervisor_action(
        self,
        machine: Dict[str, Any],
        prefix: str,
        name: str,
        action: str = "status",
        timeout: int = 120,
    ) -> Dict[str, Any]:
        """
        Drive supervisorctl for one agent (``status`` / ``restart`` / ``start``
        / ``stop``).

        The supervisord that owns every agent on a machine lives one level above
        an agent's own directory, so its paths come from the prefix rather than
        from guessing this platform's AGENTS_ROOT.
        """
        if action not in ("status", "restart", "start", "stop"):
            raise MachineError(f"unsupported supervisor action: {action}")
        prefix = _require_prefix(prefix)
        root = shlex.quote(str(Path(prefix).parent))
        command = (
            f"{root}/supervisor/.venv/bin/supervisorctl "
            f"-c {root}/supervisor/supervisord.conf {action} {shlex.quote(name)}"
        )
        try:
            out = self.run(machine, command, timeout=timeout)
            return {"ok": True, "action": action, "output": out.strip()}
        except MachineError as exc:
            # `supervisorctl status` exits non-zero for a stopped program, and
            # agents installed by an older install.sh may be launchd/systemd
            # units with no supervisord at all. Neither is a server error.
            return {
                "ok": False, "action": action,
                "output": exc.stderr or str(exc),
                "hint": "this agent may predate supervisord (service_kind "
                        "launchagent/systemd) — restart it by hand",
            }

    def delete_agent(
        self, machine: Dict[str, Any], name: str, purge: bool = False, timeout: int = 300
    ) -> Dict[str, Any]:
        """``uninstall.sh --name NAME [--purge] --json``."""
        self.push_scripts(machine)
        args = ["--name", name, "--json"]
        if purge:
            args.append("--purge")
        command = (
            f"{self._script_path(machine, 'uninstall')} "
            f"{' '.join(shlex.quote(a) for a in args)}"
        )
        out = self.run(machine, command, timeout=timeout)
        return _parse_json(out) or {"name": name, "removed": True}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _require_prefix(prefix: str) -> str:
    """
    Guard the path an agent's files are read from and written to.

    These paths end up in a remote shell command, and a blank or relative one
    would aim it at the SSH user's home directory.
    """
    prefix = (prefix or "").strip().rstrip("/")
    if not prefix or not prefix.startswith("/"):
        raise MachineError(
            f"refusing to touch agent files at {prefix!r}: an absolute install "
            "directory is required. Refresh the machine so its agents report "
            "their prefix."
        )
    return prefix


def _parse_interfaces(block: str) -> List[Dict[str, str]]:
    """
    "iface address [flags…]" lines -> [{"name", "address", "family", "note"}].

    Everything a peer could dial is listed; loopback and link-local are not,
    because nothing else can reach them. IPv4 comes first, then stable IPv6,
    then SLAAC privacy addresses — those are real but they rotate, so they are
    labelled and sorted last rather than hidden.
    """
    seen = set()
    out: List[Dict[str, Any]] = []
    for line in (block or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name, address = parts[0], parts[1]
        rest = " ".join(parts[2:]).lower()
        # macOS writes a scope on IPv6 (fe80::1%en0); ip(8) a prefix (10.0.0.1/24)
        address = address.split("%", 1)[0].split("/", 1)[0]
        if not address or address.lower().startswith(_UNREACHABLE_PREFIXES):
            continue
        if address in seen:
            continue
        seen.add(address)
        note = ""
        if "temporary" in rest:
            note = "temporary"
        elif "deprecated" in rest:
            note = "deprecated"
        out.append({
            "name": name,
            "address": address,
            "family": "ipv6" if ":" in address else "ipv4",
            "note": note,
        })

    def order(entry: Dict[str, Any]) -> tuple:
        return (
            1 if entry["note"] else 0,            # stable before rotating
            0 if entry["family"] == "ipv4" else 1,
            entry["name"],
        )

    return sorted(out, key=order)


def _env_file_lines(
    provider: str, api_key: str, control_env: Optional[Dict[str, str]]
) -> List[str]:
    """
    Contents of the staged env file.

    install.sh copies this to the agent's ``.env`` (0600), and the SDK's
    dotenv loader reads it at startup — which is also how the agent learns the
    control plane's address for the ``send_to_agent`` tool.
    """
    lines: List[str] = []
    key_name = PROVIDER_KEY_NAMES.get(provider.lower(), "")
    if api_key and key_name:
        lines.append(f"{key_name}={api_key}")
    for key, value in (control_env or {}).items():
        if value:
            lines.append(f"{key}={value}")
    return lines


def _parse_json(text: str, expect_list: bool = False) -> Any:
    """
    Pull the JSON out of a script's stdout.

    Progress goes to stderr by design, but a shell profile on the far side can
    still print a banner, so scan for the first plausible JSON start rather
    than trusting the whole buffer.
    """
    text = (text or "").strip()
    if not text:
        return [] if expect_list else None
    opener = "[" if expect_list else "{"
    start = text.find(opener)
    if start < 0:
        return [] if expect_list else None
    chunk = text[start:]
    try:
        return json.loads(chunk)
    except json.JSONDecodeError:
        # Trailing output after the object: walk back to the last closer.
        closer = "]" if expect_list else "}"
        end = chunk.rfind(closer)
        if end > 0:
            try:
                return json.loads(chunk[: end + 1])
            except json.JSONDecodeError:
                pass
    logger.warning(f"could not parse JSON from output: {text[:300]}")
    return [] if expect_list else None
