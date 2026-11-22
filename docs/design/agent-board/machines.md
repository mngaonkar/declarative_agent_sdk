# agent-board machine driver — design spec

Code: `agent_board/machines.py`.

## `agent_board/machines.py`

**Does:** `MachineDriver` runs everything that happens on a machine: probing
it, staging the install scripts, building and sending the SDK wheel,
installing and removing agents, reading and writing their files, and driving
supervisord. A thin wrapper over [install.sh](../deploy.md).

**Rules — running commands:**

- AC-MACH-1: A local machine runs `bash -lc <script>`; a remote one runs the
  script through `ssh` with the stored user, key and port. No other channel
  (no scp): files go over SSH stdin.
- AC-MACH-2: A non-zero exit raises `MachineError` with stderr (or the end of
  stdout when stderr is empty). Exit 3 from `install.sh` with JSON on stdout
  is a result ("installed, not ready"), not an error.
- AC-MACH-3: Paths that reach a remote shell are quoted, and an agent's
  prefix must be absolute (`_require_prefix`), so a blank value can never aim
  a command at the home directory.

**Rules — probing:**

- AC-MACH-4: `probe` checks reachability, `uv`, the OS, and lists the
  machine's addresses (interfaces), which the create form offers and the bind
  check uses (AC-SERVER-6).

**Rules — installing:**

- AC-MACH-5: `install.sh` and `uninstall.sh` are copied to
  `~/.agent-board/bin` once per machine per control-plane process.
- AC-MACH-6: `sdk_source=local` builds a wheel from this working tree with
  `uv build --wheel`, after clearing setuptools' `build/lib` and
  `build/bdist.*` staging (otherwise files deleted from the tree keep
  shipping), and sends it to the machine. `git` installs from the
  repository, optionally at `sdk_ref`. An installed control plane has no
  working tree: building a local wheel there fails with a message to use
  `git`.
- AC-MACH-7: `create_agent` always passes `--enable-at-boot` (unless
  `start_at_boot=False`), so supervisord comes back after a reboot
  (DEPLOY-INST-14).
- AC-MACH-8: A new agent's `.env` (API key and board handshake, AC-4) is
  staged in a 0600 file, passed as `--env-file`, and deleted right after the
  install, success or not. A reinstall passes no env file, so keys set later
  are kept.
- AC-MACH-9: Each install logs a start line and a result line (time taken,
  URL, ready or the problems). A failure logs the last 40 lines of the
  installer's output.

**Rules — an installed agent:**

- AC-MACH-10: `read_agent_files` / `write_agent_files` read and replace
  `agent.yaml` and `instructions.md`.
- AC-MACH-11: `env_names` returns only the variable *names* set in `.env`;
  `set_env_var` replaces one line. Values are never read back (AC-5).
- AC-MACH-12: `agent_network` reads, and with `write` changes, the bind
  address and published URL.
- AC-MACH-13: `supervisor_action(status|restart|start|stop)` drives
  supervisorctl. When supervisord itself is down, restart/start bring it back
  (`start_supervisord`) instead of failing.
- AC-MACH-14: `agent_logs(stream, lines)` tails `logs/<name>.err.log`
  (stderr, the default — the SDK logs there), `logs/<name>.log` (stdout) or
  the machine's `supervisord.log`; at most 5000 lines; a missing file is
  reported, not raised.
- AC-MACH-15: `agent_log_file(stream)` returns a whole log for download, at
  most its last 50 MB, marked `truncated` when cut; a missing file is a
  `LookupError` (404).
- AC-MACH-16: `delete_agent` runs `uninstall.sh` (with `--purge` when asked).

**Tests:** `tests/test_agent_board.py`, `tests/test_board_logs.py`,
`tests/test_wheel_staging.py`.
