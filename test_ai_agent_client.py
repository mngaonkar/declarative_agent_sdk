import asyncio
from ai_agent_client import AIAgentClient


async def main():
    agent_url = "http://localhost:8000"
    client = AIAgentClient(agent_url=agent_url)

    async for event in client.run("Download best nebula images from NASA website"):
        print(f"Received event: {event}")


if __name__ == "__main__":
    asyncio.run(main())