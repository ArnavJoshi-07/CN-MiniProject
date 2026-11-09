#!/usr/bin/env bash
# One command for the whole demo:
#   1. cleans up any old Mininet state,
#   2. starts the Ryu controller (logs to the results folder),
#   3. starts the Streamlit dashboard and opens it in the browser,
#   4. runs the guided demo (topology/demo.py) with sudo,
#   5. stops everything again on exit, even after Ctrl-C.
#
# Usage (from anywhere, as your normal user, not with sudo):
#   ./demo.sh             pauses before every scenario, so you can explain
#   ./demo.sh --auto      no pauses (rehearsal), no Mininet CLI at the end
#   ./demo.sh --no-browser   don't open the dashboard automatically
#
# Everything lands in results/demo-<date>-<time>/: controller.log, demo.log,
# events.log, monitor.db, agent logs, topology.json, iperf output.

set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
PROJECT="$PWD"

DEMO_ARGS=()
OPEN_BROWSER=1
for arg in "$@"; do
    case "$arg" in
        --auto) DEMO_ARGS+=(--auto) ;;
        --no-browser) OPEN_BROWSER=0 ;;
        -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg (try --help)"; exit 1 ;;
    esac
done

if [[ $EUID -eq 0 ]]; then
    echo "Run ./demo.sh as your normal user; it asks for sudo itself."
    exit 1
fi

# --- Prerequisites ---------------------------------------------------------
missing=()
[[ -x ryu-env/bin/ryu-manager ]] || missing+=("ryu-env (controller venv)")
command -v uv >/dev/null || missing+=("uv")
for tool in mn ovs-vsctl iperf hping3; do
    command -v "$tool" >/dev/null || missing+=("$tool")
done
if (( ${#missing[@]} )); then
    echo "Missing: ${missing[*]}. See README.md, Setup."
    exit 1
fi

OUT="$PROJECT/results/demo-$(date +%Y%m%d-%H%M)"
mkdir -p "$OUT" logs
echo "Results folder: ${OUT#$PROJECT/}"

echo "sudo is needed for Mininet:"
sudo -v

# --- Clean slate -----------------------------------------------------------
# mn -c also kills any ryu-manager still running, so do it before starting
# ours, never after.
echo "Cleaning up old Mininet state ..."
sudo mn -c >/dev/null 2>&1 || true

RYU_PID=""
DASH_PID=""
cleanup() {
    echo
    echo "Stopping controller and dashboard ..."
    [[ -n "$RYU_PID" ]] && kill "$RYU_PID" 2>/dev/null || true
    pkill -f "ryu-manager.*monitor_app.py" 2>/dev/null || true
    [[ -n "$DASH_PID" ]] && kill "$DASH_PID" 2>/dev/null || true
    sudo mn -c >/dev/null 2>&1 || true
    echo "Done. Results in ${OUT#$PROJECT/}/"
}
trap cleanup EXIT

# --- Controller ------------------------------------------------------------
# PYTHONUNBUFFERED: log lines reach the file immediately. The grep drops
# "packet in" lines (multicast from the hosts), which would flood the log.
echo "Starting the Ryu controller ..."
(PYTHONUNBUFFERED=1 ryu-env/bin/ryu-manager --ofp-tcp-listen-port 6653 \
    controller/monitor_app.py 2>&1 \
    | grep --line-buffered -v '^packet in' > "$OUT/controller.log") &
RYU_PID=$!

for _ in $(seq 1 30); do
    grep -q "telemetry: listening" "$OUT/controller.log" 2>/dev/null && break
    sleep 0.5
done
if ! grep -q "telemetry: listening" "$OUT/controller.log"; then
    echo "The controller didn't start; see ${OUT#$PROJECT/}/controller.log:"
    tail -20 "$OUT/controller.log"
    exit 1
fi
sleep 2     # first database write

# --- Dashboard ---------------------------------------------------------------
if ss -ltn 2>/dev/null | grep -q ':8501 '; then
    echo "Dashboard already running on http://localhost:8501"
else
    echo "Starting the dashboard on http://localhost:8501 ..."
    (cd dashboard && exec uv run --quiet streamlit run app.py \
        --server.headless true --server.port 8501 \
        --server.address localhost --browser.gatherUsageStats false \
        > "$OUT/dashboard.log" 2>&1) &
    DASH_PID=$!
    for _ in $(seq 1 40); do
        ss -ltn 2>/dev/null | grep -q ':8501 ' && break
        sleep 0.5
    done
fi
if (( OPEN_BROWSER )) && command -v xdg-open >/dev/null; then
    xdg-open http://localhost:8501 >/dev/null 2>&1 || true
fi

# --- Demo ------------------------------------------------------------------------
# sudo sets SUDO_UID / SUDO_GID, so demo.py can hand its result files back
# to you.
sudo python3 topology/demo.py --out "$OUT" "${DEMO_ARGS[@]}"
