import asyncio
from ai_agent_client import AIAgentClient
from a2a.types import TaskState
from google.protobuf.json_format import MessageToDict
import json

async def main():
    agent_url = "http://localhost:8000"
    client = AIAgentClient(agent_url=agent_url)

    async for event in client.run("Download best nebula images from NASA website"):
        print(f"Received event: {event}")
        task_id = event.task.id
        context_id = event.task.context_id


        if event.task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED:
            response = input(f"Agent is requesting input: {event.task.status.message.parts[0].text}\nY/N: ")
            data_dict = MessageToDict(event.task.status.message.parts[0].data)
            print(f"Event data: {data_dict.get("function_response")}")
            function_id = data_dict.get("function_response", {}).get("id")
            assert function_id, "Expected event data to contain function_response details for approval"

            if response.strip().lower() == "y":
                async for follow_up_event in client.send_tool_approval(
                    event.task.id,
                    event.task.context_id,
                    function_id,
                ):
                    print(f"Received follow-up event: {follow_up_event}")
            else:
                async for follow_up_event in client.send_tool_denial(
                    event.task.id,
                    event.task.context_id,
                    function_id,
                ):
                    print(f"Received follow-up event: {follow_up_event}")


if __name__ == "__main__":
    asyncio.run(main())