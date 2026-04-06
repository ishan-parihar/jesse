#!/usr/bin/env bash
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; }

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
DATA_DIR="$HOME/.mt5-mcp"
OPENCODE_CONFIG="$HOME/.config/opencode/opencode.json"

info "========================================="
info "  MT5-MCP Autonomous Agent Installer"
info "========================================="
echo

# Step 1: Python version check
info "Step 1: Checking Python version..."
PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
info "Python $PYTHON_VERSION detected"
if (( $(echo "$PYTHON_VERSION < 3.11" | bc -l) )); then
    error "Python 3.11+ required (found $PYTHON_VERSION)"
    exit 1
fi
success "Python version OK"
echo

# Step 2: Create virtual environment
info "Step 2: Setting up virtual environment..."
if [[ -d "$VENV_DIR" ]]; then
    warn "Virtual environment exists at $VENV_DIR"
else
    python3 -m venv "$VENV_DIR"
    success "Created virtual environment"
fi

"$VENV_DIR/bin/pip" install -q --upgrade pip
info "Installing dependencies..."
"$VENV_DIR/bin/pip" install -q httpx fastapi uvicorn[standard] pydantic typing-extensions structlog
"$VENV_DIR/bin/pip" install -q langgraph langchain-openai apscheduler lancedb ollama
success "All dependencies installed"
echo

# Step 3: Create data directory
info "Step 3: Preparing data directory..."
mkdir -p "$DATA_DIR"
success "Data directory: $DATA_DIR"
echo

# Step 4: Configure MCP server
info "Step 4: MCP Server Configuration"
info "The autonomous agent connects to the MT5-MCP server at http://127.0.0.1:8010"
info "Ensure the following services are running:"
info "  1. TCP Bridge (port 8025):  poetry run python -m apps.tcp_bridge.main"
info "  2. HTTP Gateway (port 8020): poetry run uvicorn apps.bridge_gateway.main:app --host 127.0.0.1 --port 8020"
info "  3. MCP Server (port 8010):  poetry run uvicorn apps.mcp_server.main:app --host 127.0.0.1 --port 8010"
echo

# Step 5: Configure opencode.json
info "Step 5: Configuring opencode.json..."
if [[ -f "$OPENCODE_CONFIG" ]]; then
    success "Found existing opencode.json at $OPENCODE_CONFIG"
    
    if python3 -c "import json; json.load(open('$OPENCODE_CONFIG'))" 2>/dev/null; then
        success "opencode.json is valid JSON"
    else
        error "opencode.json is invalid JSON — please fix manually"
    fi
    
    if grep -q '"mt5-mcp"' "$OPENCODE_CONFIG"; then
        success "mt5-mcp MCP already configured in opencode.json"
    else
        warn "mt5-mcp MCP not found in opencode.json"
        info "Add this to the \"mcp\" section of $OPENCODE_CONFIG:"
        cat << 'MCP_BLOCK'
    "mt5-mcp": {
      "type": "local",
      "command": [
        "python",
        "/home/ishanp/Documents/GitHub/MT5-mcp/tools/mcp_mt5_wrapper.py"
      ],
      "environment": {
        "PYTHONPATH": "/home/ishanp/Documents/GitHub/MT5-mcp/src",
        "MCP_HTTP_URL": "http://127.0.0.1:8010",
        "MT5_GATEWAY_URL": "http://127.0.0.1:8020"
      },
      "enabled": true
    },
MCP_BLOCK
    fi
    
    if grep -q '"qwen-proxy/coder-model"' "$OPENCODE_CONFIG"; then
        success "Model provider qwen-proxy/coder-model already configured"
    else
        warn "qwen-proxy/coder-model not found — ensure it's set in opencode.json"
    fi
else
    warn "No opencode.json found at $OPENCODE_CONFIG"
    info "Create it with your model provider configuration"
fi
echo

# Step 6: Telegram Bot Configuration
info "Step 6: Telegram Bot Configuration"
read -p "Enter your Telegram Bot Token (from @BotFather, or press Enter to skip): " TELEGRAM_TOKEN
read -p "Enter your Telegram Chat ID (or press Enter to skip): " TELEGRAM_CHAT_ID

if [[ -n "$TELEGRAM_TOKEN" && -n "$TELEGRAM_CHAT_ID" ]]; then
    export TELEGRAM_BOT_TOKEN="$TELEGRAM_TOKEN"
    export TELEGRAM_CHAT_ID="$TELEGRAM_CHAT_ID"
    success "Telegram bot configured"
else
    warn "Telegram bot not configured — agent will run without notifications"
    warn "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables later"
fi
echo

# Step 7: Install systemd service
info "Step 7: Installing systemd service..."
SERVICE_FILE="$PROJECT_DIR/deploy/systemd/mt5-autonomous-agent.service"
if [[ -f "$SERVICE_FILE" ]]; then
    CURRENT_USER=$(whoami)
    sed "s/%USER%/$CURRENT_USER/g" "$SERVICE_FILE" > "/tmp/mt5-autonomous-agent.service"
    
    if command -v systemctl &>/dev/null; then
        if [[ $EUID -eq 0 ]]; then
            cp "/tmp/mt5-autonomous-agent.service" /etc/systemd/system/
            systemctl daemon-reload
            systemctl enable mt5-autonomous-agent
            success "systemd service installed and enabled"
        else
            warn "Run with sudo to install systemd service:"
            warn "  sudo cp /tmp/mt5-autonomous-agent.service /etc/systemd/system/"
            warn "  sudo systemctl daemon-reload"
            warn "  sudo systemctl enable mt5-autonomous-agent"
        fi
    else
        warn "systemd not available — skip service installation"
    fi
    rm -f "/tmp/mt5-autonomous-agent.service"
else
    warn "Service file not found at $SERVICE_FILE"
fi
echo

# Step 8: Verify setup
info "Step 8: Verifying setup..."
if "$VENV_DIR/bin/python" -c "from mt5_mcp.autonomous.react_agent import JesseAgent; print('ReAct agent OK')" 2>/dev/null; then
    success "Agent ReAct core compiles successfully"
else
    error "Agent ReAct core failed to compile — check dependencies"
fi

if "$VENV_DIR/bin/python" -c "from mt5_mcp.autonomous.mcp_client import MCPClient; print('MCP Client OK')" 2>/dev/null; then
    success "MCP client imports successfully"
else
    error "MCP client import failed"
fi

if "$VENV_DIR/bin/python" -c "from mt5_mcp.autonomous.semantic_memory import SemanticMemory; print('Memory OK')" 2>/dev/null; then
    success "Semantic memory imports successfully"
else
    error "Semantic memory import failed"
fi

if "$VENV_DIR/bin/python" -c "from mt5_mcp.autonomous.scheduler import AgentScheduler; print('Scheduler OK')" 2>/dev/null; then
    success "Scheduler imports successfully"
else
    error "Scheduler import failed"
fi
echo

# Step 9: Install Watchdog
info "Step 9: Installing Jesse Watchdog..."
WATCHDOG_SRC="$PROJECT_DIR/scripts/jesse-watchdog.sh"
WATCHDOG_DST="$HOME/.mt5-mcp/jesse-watchdog.sh"
if [[ -f "$WATCHDOG_SRC" ]]; then
    cp "$WATCHDOG_SRC" "$WATCHDOG_DST"
    chmod +x "$WATCHDOG_DST"
    success "Watchdog script installed at $WATCHDOG_DST"

    # Create systemd service for watchdog
    cat > "/tmp/jesse-watchdog.service" << 'EOF'
[Unit]
Description=Jesse Agent Watchdog (Self-Healing Monitor)
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
User=%USER%
Group=%USER%
WorkingDirectory=PROJECT_DIR_PLACEHOLDER
EnvironmentFile=PROJECT_DIR_PLACEHOLDER/.env
Environment=JESSE_PROJECT_DIR=PROJECT_DIR_PLACEHOLDER
Environment=JESSE_WATCHDOG_INTERVAL=60
ExecStart=WATCHDOG_DST_PLACEHOLDER
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=jesse-watchdog

[Install]
WantedBy=multi-user.target
EOF

    # Replace placeholders
    sed -i "s|PROJECT_DIR_PLACEHOLDER|$PROJECT_DIR|g" "/tmp/jesse-watchdog.service"
    sed -i "s|WATCHDOG_DST_PLACEHOLDER|$WATCHDOG_DST|g" "/tmp/jesse-watchdog.service"

    if command -v systemctl &>/dev/null; then
        if [[ $EUID -eq 0 ]]; then
            cp "/tmp/jesse-watchdog.service" /etc/systemd/system/
            systemctl daemon-reload
            systemctl enable jesse-watchdog
            success "Watchdog systemd service installed and enabled"
        else
            warn "Run with sudo to install watchdog systemd service"
        fi
    fi
    rm -f "/tmp/jesse-watchdog.service"
else
    warn "Watchdog script not found at $WATCHDOG_SRC — skipping"
fi
echo

# Step 10: Preflight Setup
info "Step 10: Configuring preflight validation..."
PREFLIGHT_SRC="$PROJECT_DIR/scripts/jesse-preflight.sh"
if [[ -f "$PREFLIGHT_SRC" ]]; then
    chmod +x "$PREFLIGHT_SRC"
    success "Preflight validation script ready"

    # Run preflight and report results
    info "Running preflight validation..."
    if "$PREFLIGHT_SRC"; then
        success "All preflight checks passed"
    else
        warn "Some preflight checks failed — review output above before starting agent"
    fi
else
    warn "Preflight script not found at $PREFLIGHT_SRC"
fi
echo

# Summary
info "========================================="
info "  Installation Complete"
info "========================================="
echo
info "To start the autonomous agent:"
info ""
info "  1. Ensure MT5-MCP services are running (ports 8010, 8020, 8025)"
info "  2. Start the agent:"
info "     $VENV_DIR/bin/python -m apps.autonomous_agent.main"
info ""
if command -v systemctl &>/dev/null && [[ -f /etc/systemd/system/mt5-autonomous-agent.service ]]; then
    info "  Or via systemd:"
    info "     sudo systemctl start mt5-autonomous-agent"
    info "     sudo journalctl -u mt5-autonomous-agent -f"
fi
echo
info "Watchdog (self-healing monitor):"
info "  sudo systemctl start jesse-watchdog"
info "  sudo systemctl status jesse-watchdog"
info "  journalctl -u jesse-watchdog -f"
info "  Log file: /tmp/jesse-watchdog.log"
echo
info "Health check:"
info "  curl http://127.0.0.1:8090/health"
echo
info "Data directory: $DATA_DIR"
info "  - agent_state.db (checkpoint state)"
info "  - lancedb/ (semantic memory with embeddings)"
info "  - agent_wake_plan.json (next wake schedule)"
info "  - trading_journal.db (trade history)"
echo
