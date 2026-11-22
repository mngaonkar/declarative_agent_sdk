# Documentation

The project's overview and quick start are in the [root README](../README.md).
Everything else is here.

## Design spec

[design/](design/README.md) — what every source file does, in plain English,
as numbered rules. Start here before changing code; edit the spec with the
code, and paste its sections into prompts for code changes.

## Guides

| Page | Contents |
|---|---|
| [architecture.md](architecture.md) | Runtimes, events, Discord images, package map |
| [agents-control.md](agents-control.md) | agents-control: machines, the board, peer chat, kill switch |
| [deployment.md](deployment.md) | Running agents in the background under supervisord (`install.sh`) |
| [docker.md](docker.md) | The container image and its mounts |
| [a2ui.md](a2ui.md) | The A2UI web client |
| [a2ui-custom-components.md](a2ui-custom-components.md) | Custom A2UI components |
| [a2ui-custom-components-tests.md](a2ui-custom-components-tests.md) | Manual test pages for those components |

## Examples

Each page goes with a folder under [`examples/`](../examples/).

| Page | Example |
|---|---|
| [examples/simple_agent.md](examples/simple_agent.md) | The smallest agent |
| [examples/lean_agent.md](examples/lean_agent.md) | Lean runtime with a skill |
| [examples/adk_agent.md](examples/adk_agent.md) | Google ADK runtime |
| [examples/deepagent_agent.md](examples/deepagent_agent.md) | Deepagents runtime |
| [examples/a2a_lean.md](examples/a2a_lean.md) | A lean agent served over A2A |
| [examples/discord_bot.md](examples/discord_bot.md) | A Discord bot |
| [examples/llm_wiki.md](examples/llm_wiki.md) | A wiki kept by an agent |
| [examples/workflow.md](examples/workflow.md) | A multi-agent workflow |
| [examples/docker.md](examples/docker.md) | Running an example in Docker |
