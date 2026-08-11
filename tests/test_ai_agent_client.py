import asyncio
import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from rich.console import Console
from rich.markdown import Markdown

console = Console()

from declarative_agent_sdk.transports.a2a.client import AIAgentClient
from a2a.types import TaskState
from google.protobuf.json_format import MessageToDict
import uuid


def _extract_function_id(parts) -> str | None:
    for part in parts:
        if part.HasField('data'):
            d = MessageToDict(part.data)
            if isinstance(d, dict):
                fn_id = d.get('function_response', {}).get('id')
                if fn_id:
                    return fn_id
    return None


def _unpack_event(event):
    """Extract (state, parts, task_id, context_id) from a StreamResponse payload oneof."""
    if event.HasField('task'):
        t = event.task
        return t.status.state, list(t.status.message.parts), t.id, t.context_id
    elif event.HasField('status_update'):
        su = event.status_update
        return su.status.state, list(su.status.message.parts), su.task_id, su.context_id
    elif event.HasField('artifact_update'):
        return None, [], None, None
    elif event.HasField('message'):
        return None, [], None, None
    return None, [], None, None


async def handle_events(client: AIAgentClient, event_stream) -> None:
    """Handle events from the agent, including printing completed responses and sending tool confirmations."""
    async for event in event_stream:
        logger.info(f"Received event: {event}")
        state, parts, task_id, context_id = _unpack_event(event)
        if state is None:
            continue
        logger.info(f"Received task update: state={state} parts={parts}")

        if state == TaskState.TASK_STATE_COMPLETED:
            text = parts[0].text if parts else "No response text"
            print(text)
            console.print(Markdown(text))

        elif state == TaskState.TASK_STATE_INPUT_REQUIRED:
            fn_id = _extract_function_id(parts)
            if not fn_id:
                logger.warning("INPUT_REQUIRED but no function_id found in parts")
                continue

            d = MessageToDict(parts[0].data) if parts and parts[0].HasField('data') else {}
            fn_name = d.get('function_response', {}).get('name', 'unknown tool') if isinstance(d, dict) else 'unknown tool'
            answer = input(f"Approve '{fn_name}'? (y/n): ").strip().lower()
            approved = answer == "y"

            await handle_events(client, client.send_tool_confirmation(task_id, context_id, fn_id, approved))

        elif state == TaskState.TASK_STATE_FAILED:
            logger.error("Agent failed: %s", parts[0].text if parts else "unknown error")

        elif state == TaskState.TASK_STATE_WORKING:
            logger.info("Working: %s", parts[0].text if parts else "...")

async def main():
    agent_url = "http://localhost:8000"
    client = AIAgentClient(agent_url=agent_url)
    context_id = uuid.uuid4().hex
    logger.info(f"Using context_id: {context_id}")

    while True:
        query = input("User: ").strip()
        if query.lower() == "exit":
            print("Exiting...")
            break
        if not query:
            continue

        try:
            await handle_events(client, client.run(query, context_id))
            print("Agent request completed.\n")
        except Exception as e:
            logger.error("Error: %s", e)


if __name__ == "__main__":
    asyncio.run(main())
