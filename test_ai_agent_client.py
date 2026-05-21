import asyncio
import logging
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

from declarative_agent_sdk.ai_agent_client import AIAgentClient
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


async def handle_events(client: AIAgentClient, event_stream) -> None:
    """Handle events from the agent, including printing completed responses and sending tool confirmations."""
    async for event in event_stream:
        if not event.HasField('task'):
            logger.warning("Skipping non-task event: %s", event.WhichOneof('payload'))
            continue

        task = event.task
        state = task.status.state
        parts = task.status.message.parts

        if state == TaskState.TASK_STATE_COMPLETED:
            print(f"Agent: {parts}")

        elif state == TaskState.TASK_STATE_INPUT_REQUIRED:
            fn_id = _extract_function_id(parts)
            if not fn_id:
                logger.warning("INPUT_REQUIRED but no function_id found in parts")
                continue

            d = MessageToDict(parts[0].data)
            fn_name = d.get('function_response', {}).get('name', 'unknown tool') if isinstance(d, dict) else 'unknown tool'
            answer = input(f"Approve '{fn_name}'? (y/n): ").strip().lower()
            approved = answer == "y"

            await handle_events(client, client.send_tool_confirmation(task.id, task.context_id, fn_id, approved))

        elif state == TaskState.TASK_STATE_FAILED:
            logger.error("Agent failed: %s", parts[0].text if parts else "unknown error")


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
