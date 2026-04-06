#!/bin/bash
# jesse-watchdog.sh - External watchdog daemon for Jesse autonomous trading agent
# Monitors health, auto-recovers from crashes, alerts via Telegram
# Usage: ./jesse-watchdog.sh [--dry-run] [--once]

set -uo pipefail

###############################################################################
# Configuration (env vars with sensible defaults)
###############################################################################
PROJECT_DIR="${JESSE_PROJECT_DIR:-/home/ishanp/Documents/GitHub/MT5-mcp}"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
HEALTH_URL="http://127.0.0.1:8090/health"
CHECK_INTERVAL="${JESSE_WATCHDOG_INTERVAL:-60}"
MAX_REPAIR_ATTEMPTS="${JESSE_MAX_REPAIR:-3}"
COOLDOWN_PERIOD="${JESSE_COOLDOWN:-300}"
LOG_FILE="/tmp/jesse-watchdog.log"
STATE_DIR="$HOME/.mt5-mcp/watchdog"
BACKOFF_DELAYS=(10 30 90 300 600)
MAX_RESTARTS_PER_HOUR=10
ALERT_COOLDOWN=900

###############################################################################
# Load .env if TELEGRAM_BOT_TOKEN not already set
###############################################################################
if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]] && [[ -f "$PROJECT_DIR/.env" ]]; then
  set -a; source "$PROJECT_DIR/.env"; set +a
fi

###############################################################################
# Flags
###############################################################################
DRY_RUN=false
ONCE=false
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    --once)    ONCE=true ;;
  esac
done

###############################################################################
# Directory + PID file setup
###############################################################################
mkdir -p "$STATE_DIR"
PID_FILE="$STATE_DIR/watchdog.pid"
echo $$ > "$PID_FILE"

###############################################################################
# Utility: Logging
###############################################################################
log() {
  local level="${1:-INFO}"
  shift
  local message="$*"
  local timestamp; timestamp="$(date '+%Y-%m-%d %H:%M:%S')"
  local line="[$timestamp] [$level] $message"
  echo "$line" >> "$LOG_FILE"
  echo "$line"
}

###############################################################################
# Utility: Telegram notification with dedup
###############################################################################
notify_telegram() {
  local message="$1"
  local token="${TELEGRAM_BOT_TOKEN:-}"
  local chat_id="${TELEGRAM_CHAT_ID:-}"

  # Skip if no credentials
  if [[ -z "$token" ]] || [[ -z "$chat_id" ]]; then
    return 0
  fi

  # Dedup: check cooldown
  local last_alert=0
  if [[ -f "$STATE_DIR/last-alert-timestamp" ]]; then
    last_alert=$(cat "$STATE_DIR/last-alert-timestamp" 2>/dev/null || echo 0)
  fi
  local now; now=$(date +%s)
  local elapsed=$((now - last_alert))
  if [[ "$elapsed" -lt "$ALERT_COOLDOWN" ]]; then
    log "DEBUG" "Telegram alert suppressed (cooldown: $((ALERT_COOLDOWN - elapsed))s remaining)"
    return 0
  fi

  local url="https://api.telegram.org/bot${token}/sendMessage"
  if $DRY_RUN; then
    log "DRY-RUN" "Would send Telegram alert: $message"
  else
    local http_code; http_code=$(curl -s -o /dev/null -w "%{http_code}" \
      -X POST "$url" \
      -H "Content-Type: application/json" \
      -d "{\"chat_id\":\"$chat_id\",\"text\":\"Jesse Watchdog: $message\",\"parse_mode\":\"HTML\"}" \
      --connect-timeout 10 --max-time 15 2>/dev/null || echo "000")

    if [[ "$http_code" == "200" ]]; then
      log "INFO" "Telegram alert sent successfully"
      echo "$now" > "$STATE_DIR/last-alert-timestamp"
    else
      log "WARN" "Telegram alert failed (HTTP $http_code)"
    fi
  fi
}

###############################################################################
# Utility: Crash counter
###############################################################################
get_crash_count() {
  if [[ -f "$STATE_DIR/crash-count" ]]; then
    cat "$STATE_DIR/crash-count" 2>/dev/null || echo 0
  else
    echo 0
  fi
}

increment_crash_count() {
  local current; current=$(get_crash_count)
  local next=$((current + 1))
  echo "$next" > "$STATE_DIR/crash-count"
  date +%s > "$STATE_DIR/crash-timestamp"
  log "INFO" "Crash count incremented to $next"
}

reset_crash_count() {
  echo 0 > "$STATE_DIR/crash-count"
  rm -f "$STATE_DIR/crash-timestamp"
  log "INFO" "Crash count reset to 0"
}

check_crash_decay() {
  if [[ -f "$STATE_DIR/crash-timestamp" ]]; then
    local crash_ts; crash_ts=$(cat "$STATE_DIR/crash-timestamp" 2>/dev/null || echo 0)
    local now; now=$(date +%s)
    local age=$((now - crash_ts))
    local six_hours=21600
    if [[ "$age" -gt "$six_hours" ]]; then
      log "INFO" "Crash decay triggered (${age}s > 6h), resetting counter"
      reset_crash_count
    fi
  fi
}

###############################################################################
# Utility: Healing lock (atomic via mkdir)
###############################################################################
acquire_healing_lock() {
  local lock_path="$STATE_DIR/healing.lock"

  # Try to acquire
  if mkdir "$lock_path" 2>/dev/null; then
    log "INFO" "Healing lock acquired"
    return 0
  fi

  # Check for stale lock (older than 10 minutes)
  local lock_ts_file="$lock_path/timestamp"
  if [[ -d "$lock_path" ]]; then
    local lock_age=999999
    if [[ -f "$lock_ts_file" ]]; then
      local lock_ts; lock_ts=$(cat "$lock_ts_file" 2>/dev/null || echo 0)
      local now; now=$(date +%s)
      lock_age=$((now - lock_ts))
    fi

    if [[ "$lock_age" -gt 600 ]]; then
      log "WARN" "Removing stale healing lock (${lock_age}s old)"
      rm -rf "$lock_path"
      if mkdir "$lock_path" 2>/dev/null; then
        log "INFO" "Healing lock re-acquired after stale cleanup"
        return 0
      fi
    fi
  fi

  log "WARN" "Could not acquire healing lock (another recovery in progress)"
  return 1
}

release_healing_lock() {
  rmdir "$STATE_DIR/healing.lock" 2>/dev/null
  log "INFO" "Healing lock released"
}

###############################################################################
# Utility: Backoff delay calculation
###############################################################################
get_backoff_delay() {
  local count; count=$(get_crash_count)
  local max_idx=$(( ${#BACKOFF_DELAYS[@]} - 1 ))
  local idx=$count
  if [[ "$idx" -gt "$max_idx" ]]; then
    idx=$max_idx
  fi
  echo "${BACKOFF_DELAYS[$idx]}"
}

###############################################################################
# Utility: Restart rate limiter
###############################################################################
check_restarts_per_hour() {
  local log_file="$STATE_DIR/restart-log"
  local now; now=$(date +%s)
  local one_hour=3600
  local count=0

  if [[ -f "$log_file" ]]; then
    # Count entries within the last hour
    while IFS= read -r ts; do
      if [[ -n "$ts" ]] && [[ "$ts" =~ ^[0-9]+$ ]]; then
        local age=$((now - ts))
        if [[ "$age" -lt "$one_hour" ]]; then
          count=$((count + 1))
        fi
      fi
    done < "$log_file"
  fi

  echo "$count"
}

record_restart() {
  local now; now=$(date +%s)
  echo "$now" >> "$STATE_DIR/restart-log"

  # Prune old entries (keep only last hour)
  local cutoff=$((now - 3600))
  if [[ -f "$STATE_DIR/restart-log" ]]; then
    local tmp="$STATE_DIR/restart-log.tmp"
    grep -E "^[0-9]+$" "$STATE_DIR/restart-log" | while read -r ts; do
      if [[ "$ts" -ge "$cutoff" ]]; then
        echo "$ts"
      fi
    done > "$tmp"
    mv "$tmp" "$STATE_DIR/restart-log"
  fi
}

###############################################################################
# Health check
###############################################################################
check_health() {
  local resp; resp=$(curl -s --connect-timeout 5 --max-time 10 "$HEALTH_URL" 2>/dev/null) || return 1
  echo "$resp" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d.get('status')=='healthy' or d.get('phase') else 1)" 2>/dev/null || return 1
}

###############################################################################
# Dependency check
###############################################################################
check_dependencies() {
  local mcp_status="fail"
  local bridge_status="fail"
  local gateway_status="fail"

  # Check MCP server
  if curl -s --connect-timeout 5 http://127.0.0.1:8010/health >/dev/null 2>&1; then
    mcp_status="ok"
  fi

  # Check TCP Bridge
  if curl -s --connect-timeout 5 http://127.0.0.1:8025/status >/dev/null 2>&1; then
    bridge_status="ok"
  fi

  # Check HTTP Gateway
  if curl -s --connect-timeout 5 http://127.0.0.1:8020/bridge/terminal/status >/dev/null 2>&1; then
    gateway_status="ok"
  fi

  echo "{\"mcp\":\"$mcp_status\",\"bridge\":\"$bridge_status\",\"gateway\":\"$gateway_status\"}"
}

###############################################################################
# Recovery attempt
###############################################################################
attempt_recovery() {
  log "WARN" "=== Starting recovery attempt ==="

  # a. Acquire healing lock
  if ! acquire_healing_lock; then
    log "WARN" "Skipping recovery: could not acquire healing lock"
    return 1
  fi

  # b. Check restarts per hour
  local restarts; restarts=$(check_restarts_per_hour)
  if [[ "$restarts" -ge "$MAX_RESTARTS_PER_HOUR" ]]; then
    log "ERROR" "Max restarts per hour reached ($restarts/$MAX_RESTARTS_PER_HOUR). Aborting recovery."
    notify_telegram "MAX RESTARTS REACHED ($restarts/hr). Recovery aborted. Manual intervention required."
    release_healing_lock
    return 1
  fi

  # c. Increment crash count
  increment_crash_count

  # d. Get backoff delay
  local delay; delay=$(get_backoff_delay)
  log "INFO" "Backoff delay for this attempt: ${delay}s (crash count: $(get_crash_count))"

  # e. Check dependencies
  local deps; deps=$(check_dependencies)
  log "INFO" "Dependency check: $deps"

  # Parse dependency statuses
  local mcp_ok bridge_ok gateway_ok
  mcp_ok=$(echo "$deps" | python3 -c "import sys,json; print(json.load(sys.stdin).get('mcp','fail'))" 2>/dev/null || echo "fail")
  bridge_ok=$(echo "$deps" | python3 -c "import sys,json; print(json.load(sys.stdin).get('bridge','fail'))" 2>/dev/null || echo "fail")
  gateway_ok=$(echo "$deps" | python3 -c "import sys,json; print(json.load(sys.stdin).get('gateway','fail'))" 2>/dev/null || echo "fail")

  # f. Attempt to restart MCP if down
  if [[ "$mcp_ok" == "fail" ]]; then
    log "WARN" "MCP server is down, attempting restart..."
    if $DRY_RUN; then
      log "DRY-RUN" "Would run: systemctl restart mt5-mcp-server"
    else
      systemctl restart mt5-mcp-server 2>/dev/null || {
        log "ERROR" "Failed to restart MCP server via systemctl"
        notify_telegram "MCP server is DOWN and cannot be auto-restarted. Manual intervention required."
      }
    fi
  fi

  # g. Attempt to restart TCP Bridge if down
  if [[ "$bridge_ok" == "fail" ]]; then
    log "WARN" "TCP Bridge is down, attempting restart..."
    if $DRY_RUN; then
      log "DRY-RUN" "Would run: systemctl restart mt5-tcp-bridge"
    else
      systemctl restart mt5-tcp-bridge 2>/dev/null || {
        log "WARN" "Failed to restart TCP Bridge via systemctl"
        notify_telegram "TCP Bridge is DOWN and auto-restart failed."
      }
    fi
  fi

  # h. Attempt to restart HTTP Gateway if down
  if [[ "$gateway_ok" == "fail" ]]; then
    log "WARN" "HTTP Gateway is down, attempting restart..."
    if $DRY_RUN; then
      log "DRY-RUN" "Would run: systemctl restart mt5-http-gateway"
    else
      systemctl restart mt5-http-gateway 2>/dev/null || {
        log "WARN" "Failed to restart HTTP Gateway via systemctl"
        notify_telegram "HTTP Gateway is DOWN and auto-restart failed."
      }
    fi
  fi

  # i. Check if Jesse process is alive
  local jesse_pid; jesse_pid=$(pgrep -f "autonomous_agent.main" 2>/dev/null || true)

  # j. If alive but unhealthy, kill it
  if [[ -n "$jesse_pid" ]]; then
    log "WARN" "Jesse process found (PID: $jesse_pid) but unhealthy. Terminating..."
    if $DRY_RUN; then
      log "DRY-RUN" "Would kill PID $jesse_pid (TERM, then -9 if needed)"
    else
      kill -TERM "$jesse_pid" 2>/dev/null || true
      log "INFO" "Sent SIGTERM to Jesse (PID: $jesse_pid), waiting 5s..."
      sleep 5

      # Check if still alive
      if pgrep -f "autonomous_agent.main" >/dev/null 2>&1; then
        log "WARN" "Jesse still alive after SIGTERM. Sending SIGKILL..."
        kill -9 "$jesse_pid" 2>/dev/null || true
        sleep 2
      fi
    fi
  fi

  # k. Restart Jesse
  log "INFO" "Restarting Jesse..."
  record_restart

  if $DRY_RUN; then
    log "DRY-RUN" "Would restart Jesse via systemctl or direct invocation"
    # Simulate success for dry-run testing
    local recovery_success=true
  else
    local recovery_success=false

    # Try systemctl first
    if systemctl restart mt5-autonomous-agent 2>/dev/null; then
      log "INFO" "Restarted Jesse via systemctl (mt5-autonomous-agent)"
      recovery_success=true
    elif systemctl restart mt5-jesse 2>/dev/null; then
      log "INFO" "Restarted Jesse via systemctl (mt5-jesse)"
      recovery_success=true
    else
      # Fallback: direct invocation
      log "INFO" "Systemctl services unavailable, starting Jesse directly..."
      if [[ -x "$VENV_PYTHON" ]]; then
        cd "$PROJECT_DIR" && nohup "$VENV_PYTHON" -m apps.autonomous_agent.main >> /tmp/jesse.log 2>&1 &
        local new_pid=$!
        log "INFO" "Started Jesse directly (PID: $new_pid)"
        recovery_success=true
      else
        log "ERROR" "Python venv not found at $VENV_PYTHON"
        recovery_success=false
      fi
    fi

    # l. Wait and check health
    if $recovery_success; then
      log "INFO" "Waiting 15s for Jesse to stabilize..."
      sleep 15

      if check_health; then
        # m. Recovery successful
        log "INFO" "Jesse recovery SUCCESSFUL - health check passed"
        reset_crash_count
        notify_telegram "Jesse has RECOVERED successfully after $(get_crash_count) crash(es)."
      else
        # n. Recovery failed
        log "ERROR" "Jesse recovery FAILED - still unhealthy after restart"
        notify_telegram "Jesse recovery FAILED. Crash count: $(get_crash_count). Will retry after ${delay}s backoff."
        recovery_success=false
      fi
    fi
  fi

  release_healing_lock

  if [[ "${recovery_success:-false}" != "true" ]]; then
    log "WARN" "Recovery attempt complete. Waiting ${delay}s backoff delay..."
    if ! $DRY_RUN; then
      sleep "$delay"
    fi
    return 1
  fi

  return 0
}

###############################################################################
# Signal handling for clean exit
###############################################################################
RUNNING=true

cleanup() {
  log "INFO" "Received shutdown signal. Cleaning up..."
  RUNNING=false
  rm -f "$PID_FILE"
  release_healing_lock
  log "INFO" "Watchdog stopped cleanly."
  exit 0
}

trap cleanup SIGTERM SIGINT

###############################################################################
# Startup banner
###############################################################################
echo "============================================================"
echo " Jesse Watchdog v1.0"
echo " Monitoring: $HEALTH_URL"
echo " Check interval: ${CHECK_INTERVAL}s"
echo " Max restarts/hr: $MAX_RESTARTS_PER_HOUR"
echo " State dir: $STATE_DIR"
echo " Log file: $LOG_FILE"
echo " PID: $$"
if $DRY_RUN; then
  echo " ** DRY-RUN MODE - no actions will be executed **"
fi
echo "============================================================"
log "INFO" "Jesse Watchdog started (PID: $$)"

###############################################################################
# Main loop
###############################################################################
while $RUNNING; do

  # Warn if near restart limit
  restarts_count=$(check_restarts_per_hour)
  if [[ "$restarts_count" -ge $((MAX_RESTARTS_PER_HOUR - 2)) ]] && [[ "$restarts_count" -lt "$MAX_RESTARTS_PER_HOUR" ]]; then
    log "WARN" "Approaching restart limit: $restarts_count/$MAX_RESTARTS_PER_HOUR this hour"
    notify_telegram "WARNING: Approaching restart limit ($restarts_count/$MAX_RESTARTS_PER_HOUR this hour)."
  fi

  # Check crash decay (reset counter if > 6h since last crash)
  check_crash_decay

  # Health check
  if check_health; then
    # Healthy - optionally decrement crash count if it was previously elevated
    current_crashes=$(get_crash_count)
    if [[ "$current_crashes" -gt 0 ]]; then
      log "INFO" "Jesse healthy (crash count: $current_crashes). Stabilizing..."
      reset_crash_count
    fi

    if $ONCE; then
      log "INFO" "Single check (--once): Jesse is healthy. Exiting."
      break
    fi

    sleep "$CHECK_INTERVAL"
  else
    log "WARN" "Jesse appears UNHEALTHY"

    # Quick re-checks to avoid false positives (3 attempts, 5s apart)
    false_positive=false
    for i in 1 2 3; do
      if $ONCE; then
        log "INFO" "Single check (--once): Unhealthy on re-check $i/3"
        false_positive=false
        break
      fi

      log "INFO" "Re-check $i/3 in 5s..."
      sleep 5
      if check_health; then
        log "INFO" "Jesse recovered on re-check $i/3 (false positive)"
        false_positive=true
        break
      fi
    done

    if [[ "$false_positive" != "true" ]]; then
      log "ERROR" "Jesse confirmed unhealthy after 3 re-checks. Initiating recovery..."
      notify_telegram "Jesse is DOWN. Initiating automatic recovery."

      if ! attempt_recovery; then
        log "ERROR" "Recovery attempt did not fully succeed"
      fi
    fi

    if $ONCE; then
      log "INFO" "Single check (--once): Recovery cycle complete. Exiting."
      break
    fi

    sleep "$CHECK_INTERVAL"
  fi

done

log "INFO" "Watchdog main loop ended."
