# Declarative Agent SDK - Docker Container

This directory contains Docker configuration for running Declarative Agent SDK agents in containers.

## Overview

The Dockerfile creates a **generic base image** that:
- Installs the `declarative-agent-sdk` from GitHub
- Includes Node.js and pre-built A2UI with dependencies
- Contains Python packages and Node.js dependencies
- Allows any agent to be mounted at runtime (no rebuild needed for code changes)
- Allows skills directory to be mounted at runtime
- Allows A2UI to be mounted at runtime for development (with hot reload)

This means:
- ✅ One image works for all agents and UI
- ✅ Agent code changes don't require rebuilding
- ✅ UI dependencies pre-installed (fast startup)
- ✅ UI code changes hot-reload automatically when mounted
- ✅ Fast iteration during development

## Files

**In declarative_agent_sdk/:**
- **Dockerfile**: Multi-stage Docker build configuration
- **build-docker.sh**: Script to build the generic Docker image (run once)
- **run_ui.sh**: Script to run the A2UI development server
- **.dockerignore**: Files to exclude from Docker build context
- **DOCKER.md**: This documentation

**In each agent directory (e.g., wazy_agent/):**
- **run.sh**: Script to run the agent container
- **docker-compose.yaml**: Docker Compose configuration
- **.env** and **.env.example**: Environment variables

## Quick Start

### 1. Build the Docker image

Build once from the SDK directory:
```bash
cd declarative_agent_sdk
chmod +x build-docker.sh
./build-docker.sh
```

Or specify a custom tag:
```bash
./build-docker.sh v1.0.0
```

The image is named `declarative-agent:latest` and works for all agents.

### 2. Run an agent

Each agent has its own run script. For example, for wazy_agent:

```bash
cd wazy_agent
cp .env.example .env
# Edit .env and add your API keys
./run.sh
```

**Or using Docker directly:**
```bash
docker run -d \
  --name my-agent \
  -p 10004:10004 \
  -v /path/to/agent:/app/agent \
  -v /path/to/skills:/app/skills \
  --env-file /path/to/agent/.env \
  declarative-agent:latest
```

### 3. Run the A2UI Development Server (Optional)

The same image includes Node.js and the built A2UI application:

```bash
cd declarative_agent_sdk
chmod +x run_ui.sh
./run_ui.sh
```

This starts a Vite development server on port 5173 (default) with hot-reload:
- **URL**: http://localhost:5173
- **Container**: a2ui-dev-server
- **Hot reload**: Changes to a2ui files reload automatically

**View logs:**
```bash
docker logs -f a2ui-dev-server
```

**Stop the server:**
```bash
docker stop a2ui-dev-server
```

## Configuration

### Environment Variables

Environment variables are loaded from the `.env` file using Docker's `--env-file` flag. This keeps sensitive API keys out of the command line and script files.

Required environment variables (set in `.env`):
- `GOOGLE_API_KEY`: Google API key for Google services
- `TAVILY_API_KEY`: Tavily API key for search functionality
- `OPENAI_API_KEY`: OpenAI API key (if using OpenAI models)
- `ANTHROPIC_API_KEY`: Anthropic API key (if using Anthropic models)

Optional:
- `PORT`: Port to expose (default: 10004)
- `LOG_LEVEL`: Logging level (default: INFO)

The run script uses `--env-file .env` which passes all variables from the file directly to the container.

### Agent Configuration

Each agent has its own configuration file (e.g., `wazy_agent/configs/wazy_agent.yaml`). You can modify:
- Model provider and endpoint
- Tools and skills
- Temperature and other model parameters

**Note:** Since the entire agent directory is mounted, configuration changes take effect immediately without restarting the container (unless the agent caches the config).

## How It Works

### SDK Installation

The Dockerfile installs the SDK directly from GitHub:
```dockerfile
RUN pip install git+https://github.com/mngaonkar/google-adk-samples.git#subdirectory=declarative_agent_sdk
```

This means:
- No local SDK files copied into the image
- Image can be built anywhere (doesn't need the repo locally)
- SDK version is locked to the GitHub commit

### A2UI Build & Mounting

During the Docker build, A2UI and its dependencies are cloned from GitHub and built:
```dockerfile
# Clone google-adk-samples for a2ui
RUN git clone https://github.com/mngaonkar/google-adk-samples.git /tmp/google-adk-samples && \
    cp -r /tmp/google-adk-samples/declarative_agent_sdk/a2ui /app/a2ui && \
    rm -rf /tmp/google-adk-samples

# Clone Google's A2UI repo for renderer dependencies
RUN git clone https://github.com/google/A2UI.git /app/google-a2ui

# Fix package.json references, install, and build
RUN sed -i 's|file:../../../A2UI/|file:../google-a2ui/|g' package.json
RUN npm install && npm run build
```

For development, the local a2ui directory is **mounted at runtime**, overlaying the built version:
```bash
-v "${A2UI_DIR}:/app/a2ui"
```

This means:
- A2UI contact demo and Google A2UI dependencies sourced from GitHub
- npm dependencies pre-installed and TypeScript pre-compiled in the image
- Fast container startup (no build step needed)
- Mount local a2ui for development → hot reload works immediately with Vite
- No need to run `npm install` on container start

### Agent Mounting

The agent code is **mounted at runtime**, not copied into the image:
```bash
-v "${SCRIPT_DIR}:/app/agent"
-v "${PROJECT_ROOT}/skills:/app/skills"
```

This means:
- Edit `agent.py` locally → changes reflect immediately in container
- Edit configs → changes take effect on restart (or immediately if hot-reloaded)
- Workspace data persists on host machine
- Skills are shared across all agents
- Same image can run any agent by changing the mount paths

## Usage

### Access the agent

Once running, the agent server is available at:
```
http://localhost:10004
```

### View logs

```bash
docker logs -f wazy-agent-container
```

Or with Docker Compose:
```bash
docker-compose logs -f
```

### Stop the container

```bash
docker stop wazy-agent-container
```

Or with Docker Compose:
```bash
docker-compose down
```

### Execute commands in the container

```bash
docker exec -it wazy-agent-container /bin/bash
```

## Volume Mounts

Two directories are mounted at runtime:
- `.:/app/agent` - Agent directory (includes agent.py, configs/, workspace/)
- `../skills:/app/skills` - Shared skills directory

This allows you to:
- Edit agent code without rebuilding the image
- Edit skills without rebuilding the image
- Modify configuration files on the fly
- Persist workspace data across container restarts
- Share skills across multiple agents
- View logs and outputs on the host system

## Using with Other Agents

Since the image is generic, you can use it with any agent:

```bash
# Run storage_agent
docker run -d \
  --name storage-agent \
  -p 10005:10004 \
  -v /path/to/storage_agent:/app/agent \
  -v /path/to/skills:/app/skills \
  --env-file /path/to/storage_agent/.env \
  declarative-agent:latest

# Run music_agent
docker run -d \
  --name music-agent \
  -p 10006:10004 \
  -v /path/to/music_agent:/app/agent \
  -v /path/to/skills:/app/skills \
  --env-file /path/to/music_agent/.env \
  declarative-agent:latest
```

Just mount any agent directory to `/app/agent` and skills to `/app/skills`!

## Building for Production

### Tag and push to a registry

```bash
# Build with version tag
./build.sh v1.0.0

# Tag for your registry
docker tag declarative-agent:v1.0.0 your-registry/declarative-agent:v1.0.0

# Push to registry
docker push your-registry/declarative-agent:v1.0.0
```

**Deployment Strategy:**
1. Build the generic image once
2. Push to your registry
3. Deploy to any environment
4. Mount different agents as needed

### Multi-architecture builds

```bash
# From declarative_agent_sdk directory
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t your-registry/declarative-agent:latest \
  -f Dockerfile \
  --push \
  ..
```

## Troubleshooting

### .env file not found

If you see "Warning: .env file not found", create one in your agent directory from the template:
```bash
cd your_agent_directory
cp .env.example .env
# Edit .env with your actual API keys
```

### Container fails to start

1. Check logs: `docker logs <container-name>`
2. Verify environment variables are set in the agent's `.env` file
3. Ensure the port is not already in use
4. Check that endpoints in the agent's config file are accessible

### Cannot connect to vLLM/external endpoint

Update the endpoint in your agent's config file (e.g., `configs/agent_name.yaml`). If running in Docker, use:
- **Same machine**: `host.docker.internal:PORT` (Mac/Windows) or `172.17.0.1:PORT` (Linux)
- **Different machine**: Use the IP address directly
- **Host network mode**: `docker run --network host` (removes network isolation)

### Permission issues with volumes

Ensure the workspace and logs directories have proper permissions:
```bash
chmod -R 755 workspace logs
```

## Development

### Build without cache

```bash
cd declarative_agent_sdk
docker build --no-cache -t declarative-agent:dev -f Dockerfile ..
```

### Run with custom settings

```bash
docker run -d \
  --name wazy-agent-dev \
  -p 10005:10004 \
  --env-file .env \
  -e PORT=10004 \
  -v $(pwd):/app/agent \
  declarative-agent:dev
```

### Hot Reloading

Since the agent code is mounted, changes to `agent.py` or configuration files are reflected immediately:
1. Edit `agent.py` locally
2. Restart the container: `docker restart wazy-agent-container`
3. Or, implement file watching in your agent for true hot reload

## Health Checks

The container includes a health check that verifies the agent server is responding:
```bash
docker inspect --format='{{.State.Health.Status}}' wazy-agent-container
```

## Container Management

### View resource usage

```bash
docker stats wazy-agent-container
```

### Restart policy

The container is configured with `--restart unless-stopped`, meaning it will automatically restart on failure but not when manually stopped.

## Additional Resources

- [Google ADK Documentation](https://github.com/google/adk)
- [Declarative Agent SDK](../declarative_agent_sdk/README.md)
- [Docker Documentation](https://docs.docker.com/)
