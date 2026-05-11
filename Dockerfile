# Generic Dockerfile for Declarative Agent SDK Agents
# Stage 1: Build stage
FROM python:3.11-slim as builder

# Set working directory
WORKDIR /app

# Install build dependencies including Node.js
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    git \
    curl \
    ca-certificates \
    gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install declarative-agent-sdk from GitHub
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir git+https://github.com/mngaonkar/google-adk-samples.git#subdirectory=declarative_agent_sdk

# Clone google-adk-samples repo to get a2ui
RUN git clone https://github.com/mngaonkar/google-adk-samples.git /tmp/google-adk-samples && \
    cp -r /tmp/google-adk-samples/declarative_agent_sdk/a2ui /app/a2ui && \
    rm -rf /tmp/google-adk-samples

# Clone Google's A2UI repo for renderer dependencies
RUN git clone https://github.com/google/A2UI.git /app/google-a2ui

# Install dependencies in google-a2ui (needed for wireit dependencies)
WORKDIR /app/google-a2ui/renderers/web_core
RUN echo "==== Installing Google A2UI renderer web_core dependencies ====" && \
    npm install --verbose && \
    npm run build --verbose

WORKDIR /app/google-a2ui/renderers/markdown/markdown-it
RUN echo "==== Installing Google A2UI renderer markdown dependencies ====" && \
    npm install --verbose && \
    npm run build --verbose

WORKDIR /app/google-a2ui/renderers/lit
RUN echo "==== Installing Google A2UI renderer lit dependencies ====" && \
    npm install --verbose && \
    npm run build --verbose

WORKDIR /app/a2ui

# Fix package.json to reference the cloned A2UI location
RUN sed -i 's|../../../A2UI/|../google-a2ui/|g' package.json && \
    echo "==== Updated package.json ===="

RUN cat package.json

RUN sed -i 's|../../../A2UI/|../google-a2ui/|g' tsconfig.json && \
     echo "==== Updated tsconfig.json ===="

RUN cat tsconfig.json

RUN echo "==== Removing old package-lock.json ====" && \
    rm -f package-lock.json

# Install dependencies and build with verbose output
RUN set -x && \
    echo "==== Starting npm install ====" && \
    npm install --verbose && \
    echo "==== npm install completed ===="
    
RUN set -x && \
    echo "==== Starting npm build ====" && \
    npm run build --verbose && \
    echo "==== npm build completed ===="

# Stage 2: Runtime stage
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install runtime dependencies including Node.js
RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Copy Python packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy a2ui with node_modules from builder
COPY --from=builder /app/a2ui /app/a2ui

# Copy Google A2UI repo (needed for file: dependencies)
COPY --from=builder /app/google-a2ui /app/google-a2ui

# Skills directory will be mounted at runtime
# Agent directory will be mounted at runtime

# Create workspace directory
RUN mkdir -p /app/agent/workspace

# Expose the agent server port (default, can be overridden)
EXPOSE 10004

# Set default environment variables (can be overridden at runtime)
ENV PORT=10004
ENV WORKSPACE_DIRECTORY=/app/agent/workspace

# Set working directory for agent (will be mounted at runtime)
WORKDIR /app/agent

# Default command (can be overridden at runtime)
CMD ["python", "agent.py"]
