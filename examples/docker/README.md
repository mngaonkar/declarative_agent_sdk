# Docker Deployment Example

Demonstrates running agents built with the **Declarative Agent SDK** inside Docker containers using the generic image and `docker compose`.

---

## 1. Build the Generic Base Image

From the repository root, build the Docker image:

```bash
cd ../..
./build-docker.sh
```

This builds the `declarative-agent:latest` image containing:
- Python 3.14 + Declarative Agent SDK
- Node.js + pre-built A2UI components
- A2A protocol and transport dependencies

---

## 2. Configure Environment

Copy `.env.example` to `.env` and fill in your API keys:

```bash
cd examples/docker
cp ../../.env.example .env
```

---

## 3. Run with Docker Compose

Start the containerized agent and UI:

```bash
docker compose up -d
```

- **A2A Agent Server:** http://localhost:8000/
- **Agent Card:** http://localhost:8000/.well-known/agent-card.json
- **A2UI Web Interface:** http://localhost:5173/

View logs:
```bash
docker compose logs -f
```

Stop the container:
```bash
docker compose down
```
