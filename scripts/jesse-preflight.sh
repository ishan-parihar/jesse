#!/bin/bash
# ─── Jesse Preflight Validation ───────────────────────────────────────────────
# Validates ALL dependencies BEFORE the autonomous trading agent starts.
# Prevents crash loops from bad configuration, missing services, or broken venv.
#
# Inspired by OpenClaw's Level 0 preflight validation.
#
# Usage:
#   ./scripts/jesse-preflight.sh         # Human-readable output
#   ./scripts/jesse-preflight.sh --json  # Machine-readable JSON output
#   ./scripts/jesse-preflight.sh --fix   # Attempt to fix common issues
#
# Designed for:
#   - systemd ExecStartPre=
#   - Watchdog scripts before agent restart
#   - Manual operator checks
#
# Exit codes:
#   0 — All critical checks pass (MCP server healthy, venv functional)
#   1 — One or more critical checks failed
# ──────────────────────────────────────────────────────────────────────────────

set -uo pipefail

# ─── Configuration ────────────────────────────────────────────────────────────

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DATA_DIR="${HOME}/.mt5-mcp"
WATCHDOG_DIR="${DATA_DIR}/watchdog"

# Service endpoints
MCP_SERVER_PORT="${MCP_SERVER_PORT:-8010}"
TCP_BRIDGE_PORT="${TCP_BRIDGE_PORT:-8025}"
HTTP_GATEWAY_PORT="${HTTP_GATEWAY_PORT:-8020}"

# Systemd unit names for --fix (empty means skip)
MCP_SERVICE="mt5-mcp-server"
TCP_BRIDGE_SERVICE="mt5-tcp-bridge"
HTTP_GATEWAY_SERVICE="mt5-http-gateway"

# Python modules that must be importable
REQUIRED_MODULES=(
  "jesse.react_agent:JesseAgent"
  "jesse.mcp_client:MCPClient"
  "jesse.scheduler:AgentScheduler"
)

# Required environment variables: name:required|optional
REQUIRED_ENV_VARS=(
  "TELEGRAM_BOT_TOKEN:required"
  "TELEGRAM_CHAT_ID:required"
  "JESSE_BASE_URL:optional"
  "JESSE_MODEL:optional"
)

PYTHON_BIN="${PROJECT_DIR}/.venv/bin/python"

# ─── Parse flags ──────────────────────────────────────────────────────────────

JSON_OUTPUT=false
FIX_MODE=false

for arg in "$@"; do
  case "$arg" in
    --json)   JSON_OUTPUT=true ;;
    --fix)    FIX_MODE=true ;;
    -h|--help)
      echo "Usage: $0 [--json] [--fix]"
      echo "  --json  Output machine-readable JSON"
      echo "  --fix   Attempt to fix common issues"
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      echo "Usage: $0 [--json] [--fix]" >&2
      exit 2
      ;;
  esac
done

# ─── Result tracking ─────────────────────────────────────────────────────────

OK_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0

# Named results for JSON output: name|status|detail
declare -a RESULT_ENTRIES=()

record_ok() {
  local name="$1" detail="$2"
  OK_COUNT=$((OK_COUNT + 1))
  RESULT_ENTRIES+=("${name}|ok|${detail}")
  [ "$JSON_OUTPUT" = false ] && echo -e "\033[32m[OK]\033[0m   ${detail}"
}

record_warn() {
  local name="$1" detail="$2"
  WARN_COUNT=$((WARN_COUNT + 1))
  RESULT_ENTRIES+=("${name}|warn|${detail}")
  [ "$JSON_OUTPUT" = false ] && echo -e "\033[33m[WARN]\033[0m ${detail}"
}

record_fail() {
  local name="$1" detail="$2"
  FAIL_COUNT=$((FAIL_COUNT + 1))
  RESULT_ENTRIES+=("${name}|fail|${detail}")
  [ "$JSON_OUTPUT" = false ] && echo -e "\033[31m[FAIL]\033[0m ${detail}"
}

# ─── Fix helpers ──────────────────────────────────────────────────────────────

fix_create_dir() {
  local dir="$1" desc="$2"
  if [ ! -d "$dir" ]; then
    if mkdir -p "$dir" 2>/dev/null; then
      echo -e "\033[36m[FIX]\033[0m Created $desc: $dir"
      return 0
    else
      echo "[FIX] FAILED to create $desc: $dir" >&2
      return 1
    fi
  fi
  return 0
}

fix_copy_env() {
  if [ ! -f "${PROJECT_DIR}/.env" ] && [ -f "${PROJECT_DIR}/.env.example" ]; then
    if cp "${PROJECT_DIR}/.env.example" "${PROJECT_DIR}/.env" 2>/dev/null; then
      echo -e "\033[36m[FIX]\033[0m Created .env from .env.example — YOU MUST fill in secrets"
      # Re-source so newly copied vars are visible
      set -a; source "${PROJECT_DIR}/.env" 2>/dev/null; set +a
      return 0
    else
      echo "[FIX] FAILED to create .env from .env.example" >&2
      return 1
    fi
  fi
  return 0
}

fix_start_service() {
  local service="$1" desc="$2"
  [ -z "$service" ] && return 1

  if command -v systemctl &>/dev/null; then
    if systemctl is-active --quiet "$service" 2>/dev/null; then
      return 0
    fi
    if systemctl start "$service" 2>/dev/null; then
      echo -e "\033[36m[FIX]\033[0m Started $desc via systemctl ($service)"
      return 0
    fi
  fi
  echo -e "\033[36m[FIX]\033[0m systemctl unavailable or failed — skipping auto-start of $desc"
  return 1
}

# ─── Load .env early ─────────────────────────────────────────────────────────

ENV_FILE="${PROJECT_DIR}/.env"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

# ─── CHECK 1: MCP Server ─────────────────────────────────────────────────────

check_mcp_server() {
  local url="http://127.0.0.1:${MCP_SERVER_PORT}/health"
  local http_code response

  response=$(curl -s --connect-timeout 5 --max-time 10 -w "\n%{http_code}" "$url" 2>&1)
  http_code=$(echo "$response" | tail -1)

  if [ "$http_code" = "200" ]; then
    record_ok "mcp_server" "MCP Server healthy on port ${MCP_SERVER_PORT} (HTTP 200)"
  else
    record_fail "mcp_server" "MCP Server not responding on port ${MCP_SERVER_PORT} (HTTP ${http_code:-no response})"
    [ "$FIX_MODE" = true ] && fix_start_service "$MCP_SERVICE" "MCP server"
  fi
}

# ─── CHECK 2: TCP Bridge ─────────────────────────────────────────────────────

check_tcp_bridge() {
  local url="http://127.0.0.1:${TCP_BRIDGE_PORT}/status"
  local http_code response body

  response=$(curl -s --connect-timeout 5 --max-time 10 -w "\n%{http_code}" "$url" 2>&1)
  http_code=$(echo "$response" | tail -1)
  body=$(echo "$response" | sed '$d')

  # Valid JSON starts with { or [
  if echo "$body" | grep -qE '^\s*[\{\[]'; then
    record_ok "tcp_bridge" "TCP Bridge responding on port ${TCP_BRIDGE_PORT} (valid JSON)"
  else
    record_fail "tcp_bridge" "TCP Bridge not responding on port ${TCP_BRIDGE_PORT} (HTTP ${http_code:-no response}, no valid JSON)"
    [ "$FIX_MODE" = true ] && fix_start_service "$TCP_BRIDGE_SERVICE" "TCP bridge"
  fi
}

# ─── CHECK 3: HTTP Gateway ───────────────────────────────────────────────────

check_http_gateway() {
  local url="http://127.0.0.1:${HTTP_GATEWAY_PORT}/bridge/terminal/status"
  local http_code response

  response=$(curl -s --connect-timeout 5 --max-time 10 -w "\n%{http_code}" "$url" 2>&1)
  http_code=$(echo "$response" | tail -1)

  if [ "$http_code" = "200" ]; then
    record_ok "http_gateway" "HTTP Gateway healthy on port ${HTTP_GATEWAY_PORT} (HTTP 200)"
  else
    record_fail "http_gateway" "HTTP Gateway not responding on port ${HTTP_GATEWAY_PORT} (HTTP ${http_code:-no response})"
    [ "$FIX_MODE" = true ] && fix_start_service "$HTTP_GATEWAY_SERVICE" "HTTP gateway"
  fi
}

# ─── CHECK 4: Environment Variables ──────────────────────────────────────────

check_env_vars() {
  local issues=() has_fail=false

  if [ "$FIX_MODE" = true ]; then
    fix_copy_env
  fi

  for entry in "${REQUIRED_ENV_VARS[@]}"; do
    local var_name="${entry%%:*}"
    local requirement="${entry##*:}"
    local var_value="${!var_name:-}"

    if [ -z "$var_value" ]; then
      if [ "$requirement" = "required" ]; then
        has_fail=true
        issues+=("MISSING required: ${var_name}")
      else
        issues+=("MISSING optional: ${var_name} (will use default)")
      fi
    fi
  done

  if [ ${#issues[@]} -eq 0 ]; then
    record_ok "env_vars" "All required environment variables are set"
  elif [ "$has_fail" = true ]; then
    local detail=""
    for issue in "${issues[@]}"; do
      detail="${detail}${issue}; "
    done
    record_fail "env_vars" "${detail}"
  else
    local detail=""
    for issue in "${issues[@]}"; do
      detail="${detail}${issue}; "
    done
    record_warn "env_vars" "${detail}"
  fi
}

# ─── CHECK 5: Python venv ────────────────────────────────────────────────────

check_python_venv() {
  # Check venv binary exists
  if [ ! -x "$PYTHON_BIN" ]; then
    record_fail "python_venv" "Python venv not found at ${PYTHON_BIN}"
    return
  fi

  # Check required module imports
  local import_issues=()
  for module_entry in "${REQUIRED_MODULES[@]}"; do
    local module="${module_entry%%:*}"
    local class="${module_entry##*:}"
    local result
    result=$("$PYTHON_BIN" -c "from ${module} import ${class}" 2>&1)
    if [ $? -ne 0 ]; then
      import_issues+=("Cannot import ${class} from ${module}")
    fi
  done

  if [ ${#import_issues[@]} -eq 0 ]; then
    record_ok "python_venv" "Venv OK, all ${#REQUIRED_MODULES[@]} required modules importable"
  else
    local detail=""
    for issue in "${import_issues[@]}"; do
      detail="${detail}${issue}; "
    done
    record_warn "python_venv" "${detail}"
  fi
}

# ─── CHECK 6: Data directory ─────────────────────────────────────────────────

check_data_dir() {
  # Check data directory
  if [ ! -d "$DATA_DIR" ]; then
    if [ "$FIX_MODE" = true ] && fix_create_dir "$DATA_DIR" "data directory"; then
      :  # created successfully, continue
    else
      record_fail "data_dir" "Data directory missing and cannot create: ${DATA_DIR}"
      return
    fi
  fi

  # Check writable
  if [ ! -w "$DATA_DIR" ]; then
    record_fail "data_dir" "Data directory not writable: ${DATA_DIR}"
    return
  fi

  # Check watchdog subdirectory
  if [ ! -d "$WATCHDOG_DIR" ]; then
    if [ "$FIX_MODE" = true ] && fix_create_dir "$WATCHDOG_DIR" "watchdog directory"; then
      record_ok "data_dir" "Data directory writable, watchdog directory created"
    else
      record_warn "data_dir" "Data directory writable, watchdog missing: ${WATCHDOG_DIR}"
    fi
  else
    record_ok "data_dir" "Data directory writable, watchdog directory exists"
  fi
}

# ─── Output ───────────────────────────────────────────────────────────────────

print_header() {
  if [ "$JSON_OUTPUT" = false ]; then
    echo ""
    echo "============================================"
    echo "  Jesse Preflight Validation"
    echo "  $(date '+%Y-%m-%d %H:%M:%S')"
    echo "  Project: ${PROJECT_DIR}"
    echo "============================================"
    echo ""
    echo "Checking dependencies..."
    echo ""
  fi
}

print_summary() {
  # Critical: MCP server (index 0) and Python venv (index 4) must not fail
  local all_critical_pass=true
  for entry in "${RESULT_ENTRIES[@]}"; do
    local name="${entry%%|*}"
    local rest="${entry#*|}"
    local status="${rest%%|*}"
    if [ "$name" = "mcp_server" ] || [ "$name" = "python_venv" ]; then
      if [ "$status" = "fail" ]; then
        all_critical_pass=false
      fi
    fi
  done

  if [ "$JSON_OUTPUT" = true ]; then
    local json="{\"checks\":["
    local first=true
    for entry in "${RESULT_ENTRIES[@]}"; do
      local name="${entry%%|*}"
      local rest="${entry#*|}"
      local status="${rest%%|*}"
      local detail="${rest#*|}"
      # Escape quotes in detail for JSON
      detail="${detail//\"/\\\"}"
      if [ "$first" = true ]; then
        first=false
      else
        json+=","
      fi
      json+="{\"name\":\"${name}\",\"status\":\"${status}\",\"detail\":\"${detail}\"}"
    done
    json+="],\"passed\":${all_critical_pass}}"
    echo "$json"
  else
    echo ""
    echo "--------------------------------------------"
    echo "  Summary: ${OK_COUNT} OK, ${WARN_COUNT} warnings, ${FAIL_COUNT} failures"
    echo "--------------------------------------------"
    echo ""

    if [ "$all_critical_pass" = false ]; then
      echo -e "\033[31mCRITICAL: One or more critical checks failed. Do NOT start Jesse.\033[0m"
    elif [ "$WARN_COUNT" -gt 0 ]; then
      echo -e "\033[33mWARNING: Non-critical issues found. Jesse may start with degraded functionality.\033[0m"
    else
      echo -e "\033[32mAll checks passed. Jesse is ready to start.\033[0m"
    fi
    echo ""
  fi

  if [ "$all_critical_pass" = false ]; then
    exit 1
  fi
  exit 0
}

# ─── Main ─────────────────────────────────────────────────────────────────────

main() {
  print_header

  check_mcp_server
  check_tcp_bridge
  check_http_gateway
  check_env_vars
  check_python_venv
  check_data_dir

  print_summary
}

main
