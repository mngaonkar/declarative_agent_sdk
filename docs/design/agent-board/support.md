# agent-board support modules — design spec

Code: `agent_board/config.py`, `agent_board/catalog.py`,
`agent_board/metrics.py`, `agent_board/logbook.py`.

## `agent_board/config.py`

**Does:** reads, validates and rewrites an installed agent's `agent.yaml` for
the Settings dialog.

**Rules:**

- AC-CONFIG-1: The form edits a fixed list of properties
  (`EDITABLE_PROPERTIES`): description, framework, provider, model,
  `endpoint.url`, temperature, max output tokens, model call timeout, tools,
  tool approval, instruction file, skills and workspace directories, and the
  lean loop limits. `properties_for(framework)` shows only those that reach
  that runtime.
- AC-CONFIG-2: `merge` applies form values to the existing file and re-emits
  it, keeping the comment block at the top (other comments are lost; Raw YAML
  editing keeps the file exactly as typed).
- AC-CONFIG-3: `validate` raises `ConfigError` (shown as 400) for a config
  that cannot work: not a mapping, `name` changed (the name is the folder and
  service), unknown framework, `tools` not a list. Everything else comes back
  as warnings: a tool not registered, `vllm` without `endpoint.url`, approval
  off while `exec_command` is available.
- AC-CONFIG-4: `runtime_of` normalises framework names to lean / adk /
  deepagent.

**Tests:** `tests/test_agent_board.py`.

## `agent_board/catalog.py`

**Does:** what a provider offers, for the create-agent form.

**Rules:**

- AC-CATALOG-1: `defaults()` is the static part (providers, default models
  and endpoints), sent with `/api/state` so the form needs no extra request.
- AC-CATALOG-2: `list_models` asks `{endpoint}/models` for providers that
  support listing (`openai`, `vllm`). For OpenAI, models that cannot chat
  (embeddings, audio, images) are filtered out; the list is sorted and
  de-duplicated.

## `agent_board/metrics.py`

**Does:** reads what the model server behind each agent is doing, for the
"Model servers" cards on the Agents tab.

**Rules:**

- AC-METRICS-1: For each distinct endpoint, read `/metrics` (Prometheus),
  `/slots` (the live request's prompt size) and `/props` (loaded model and
  context window). A llama.cpp server needs `--metrics` for the first.
- AC-METRICS-2: `probe` never raises: an unreachable endpoint is a result.
  Endpoints are read in parallel, so one slow server delays only itself.
- AC-METRICS-3: Throughput is computed over the window since the previous
  poll (`Sampler`), not since the server started; with no new requests the
  last measured rate is shown, marked as held.
- AC-METRICS-4: `contexts_for` asks every agent `GET /context` for its history
  size, in parallel.
- AC-METRICS-5: `mcps_for` asks every agent `GET /mcp` for its MCP servers, in
  parallel with AC-METRICS-4. A stopped agent, or one installed before MCP
  (no route), is `supported: false` with no servers and the reason.

## `agent_board/logbook.py`

**Does:** the control plane's own log, kept where the UI can show it.

**Rules:**

- AC-LOG-1: Every record from the process goes to a ring buffer of the last
  5000 records, each with a rising `seq`. `read(after, level, query, limit)`
  returns records after `seq`, filtered by lowest level and text, oldest
  first, at most `limit` (the newest when more match), plus `last` to pass
  back next time.
- AC-LOG-2: Records also go to `logs/control.log` beside the database,
  rotated at 5 MB with 3 old files kept.
- AC-LOG-3: uvicorn's access log is left out of both, so UI polling does not
  bury everything else.
- AC-LOG-4: `install(log_dir)` is safe to call repeatedly: one buffer, one
  file handler per folder.
- AC-LOG-5: `tail_lines(text, n)` bounds long command output in log lines.

**Tests:** `tests/test_board_logs.py`.
