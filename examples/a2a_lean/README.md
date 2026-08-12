# Lean agent + A2A server

Run the **lean** deliberative loop behind the shared A2A transport (`AIAgentServer`).

## Prerequisites

```bash
# from repo root
pip install -e ".[a2a]"   # or your venv that already has a2a + uvicorn
export OPENAI_API_KEY=sk-...
```

## Start the server

```bash
cd examples/a2a_lean
python run_server.py
# HOST=0.0.0.0 PORT=8000 python run_server.py
```

You should see something like:

```text
Lean A2A agent 'lean_a2a_agent' on http://127.0.0.1:8000/
```

## Talk to it (client REPL)

```bash
cd examples/a2a_lean
python run_client.py
```

Example prompts:

- `Say hello and end.`
- `How much free disk space is there?` (may prompt tool approval for `exec_command`)

On tool approval, type `y` or `n`.

## What is under test

| Piece | Role |
|--------|------|
| `agent_framework: lean` | Plan/act/reflect loop + skills |
| `AIAgentServer` | Starlette + JSON-RPC + agent card |
| `AIAgentExecutor` | Maps `AgentEvent` → A2A task states |
| `AIAgentClient` | Discover card, stream messages, resume HITL |

## Automated tests (no live API)

From repo root:

```bash
pytest tests/test_lean_a2a.py -q
```

These mock the LLM and exercise lean `invoke` + the A2A executor (final answer, tool approval, resume).
