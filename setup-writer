#!/usr/bin/env bash
set -e

SYSTEM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SYSTEM_DIR/.env"
BIN_DIR="/usr/local/bin"

echo "=== Writer System Setup ==="

# 1. Check Prerequisites
if ! command -v docker &> /dev/null; then
    echo "Docker is not installed. Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker $USER
    echo "Docker installed. Please re-run setup after logging back in."
    exit 1
fi

if ! command -v curl &> /dev/null; then
    echo "Installing curl..."
    sudo apt update && sudo apt install -y curl
fi

# 2. Setup Directory Structure
mkdir -p "$SYSTEM_DIR/vault"
mkdir -p "$SYSTEM_DIR/signal-data"
mkdir -p "$SYSTEM_DIR/ollama-data"

# 3. Interactive API Key & Configuration Prompting
touch "$ENV_FILE"

get_or_prompt_env() {
    local key="$1"
    local prompt_text="$2"
    local default_val="$3"
    local existing_val
    
    existing_val=$(grep -E "^${key}=" "$ENV_FILE" | cut -d '=' -f2- | tr -d '"' | tr -d "'")
    
    if [ -z "$existing_val" ]; then
        if [ -n "$default_val" ]; then
            read -p "$prompt_text [$default_val]: " input_val
            input_val="${input_val:-$default_val}"
        else
            read -p "$prompt_text: " input_val
        fi
        echo "${key}=\"${input_val}\"" >> "$ENV_FILE"
    else
        echo "✓ $key is configured."
    fi
}

echo ""
echo "--- Environment Configuration ---"
get_or_prompt_env "GITHUB_USER" "Enter GitHub Username/Org for downloads" "your-username"
get_or_prompt_env "GITHUB_REPO" "Enter GitHub Repository Name" "writer-system"
get_or_prompt_env "GITHUB_BRANCH" "Enter Branch Name" "main"
get_or_prompt_env "GEMINI_API_KEY" "Enter Gemini API Key" ""
get_or_prompt_env "SIGNAL_PHONE_NUMBER" "Enter Signal Phone Number (+1234567890)" ""
get_or_prompt_env "DAILY_BUDGET_LIMIT" "Enter Daily API Budget Limit (USD)" "0.50"

# Load variables for downloads
source "$ENV_FILE"

RAW_BASE_URL="https://raw.githubusercontent.com/${GITHUB_USER}/${GITHUB_REPO}/${GITHUB_BRANCH}"

# 4. Download docker-compose.yml from GitHub
echo ""
echo "--- Downloading docker-compose.yml from GitHub ---"
if curl -sSL -f "${RAW_BASE_URL}/docker-compose.yml" -o "$SYSTEM_DIR/docker-compose.yml"; then
    echo "✓ Downloaded docker-compose.yml from GitHub"
else
    echo "⚠️ Could not download docker-compose.yml from GitHub. Generating local default..."
    cat <<'EOF' > "$SYSTEM_DIR/docker-compose.yml"
version: '3.8'

services:
  signal-cli:
    image: bbernhard/signal-cli-rest-api:latest
    container_name: signal_gateway
    environment:
      - MODE=native
    volumes:
      - ./signal-data:/home/pb/.local/share/signal-cli
    ports:
      - "8080:8080"
    restart: unless-stopped

  ollama:
    image: ollama/ollama:latest
    container_name: local_llm
    volumes:
      - ./ollama-data:/root/.ollama
    ports:
      - "11434:11434"
    restart: unless-stopped

  syncthing:
    image: syncthing/syncthing:latest
    container_name: vault_sync
    volumes:
      - ./vault:/var/syncthing/vault
    ports:
      - "8384:8384"
      - "22000:22000/tcp"
      - "22000:22000/udp"
    restart: unless-stopped

  writer-bot:
    build: .
    container_name: writer_bot
    depends_on:
      - signal-cli
      - ollama
    env_file:
      - .env
    volumes:
      - ./vault:/app/vault
      - /var/run/docker.sock:/var/run/docker.sock
    restart: unless-stopped
EOF
fi

# 5. Download Operational Control Scripts from GitHub
SCRIPTS=("writer-start" "writer-stop" "writer-restart" "writer-status")

echo ""
echo "--- Downloading Operational Scripts from GitHub ---"
for script in "${SCRIPTS[@]}"; do
    echo "Fetching $script..."
    if curl -sSL -f "${RAW_BASE_URL}/${script}" -o "$SYSTEM_DIR/$script"; then
        chmod +x "$SYSTEM_DIR/$script"
        echo " ✓ Downloaded and executable: $script"
    else
        echo "⚠️ Warning: Failed to download $script from ${RAW_BASE_URL}/${script}."
    fi
done

# 6. Create placeholding Dockerfile and main.py if missing
if [ ! -f "$SYSTEM_DIR/Dockerfile" ]; then
    cat <<'EOF' > "$SYSTEM_DIR/Dockerfile"
FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir requests docker
COPY main.py .
CMD ["python", "-u", "main.py"]
EOF
fi

if [ ! -f "$SYSTEM_DIR/main.py" ]; then
    cat <<'EOF' > "$SYSTEM_DIR/main.py"
import os, time
print("Writer Bot Service Running...")
while True:
    time.sleep(10)
EOF
fi

# 7. Symlink All Executables to System PATH
echo ""
echo "--- Linking Commands to PATH ---"
chmod +x "$SYSTEM_DIR/setup-writer"

for cmd in setup-writer "${SCRIPTS[@]}"; do
    if [ -f "$SYSTEM_DIR/$cmd" ]; then
        sudo ln -sf "$SYSTEM_DIR/$cmd" "$BIN_DIR/$cmd"
        echo " Linked: $cmd -> $BIN_DIR/$cmd"
    fi
done

echo ""
echo "=== Setup Complete ==="
echo "Management commands available system-wide:"
echo "  writer-start   : Launch the Docker stack"
echo "  writer-stop    : Stop all containers"
echo "  writer-restart : Restart the stack"
echo "  writer-status  : View logs and container states"
