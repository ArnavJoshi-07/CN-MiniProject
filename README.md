# CN-MiniProject: Network Monitoring and Alert System

A distributed network-monitoring system built on an emulated SDN network.
Hosts send application telemetry over **UDP**; an SDN controller combines it
with **OpenFlow flow/port statistics** to detect abnormal conditions and raise alerts.

> **Status:** Day 2 of 12 — custom topology in progress. See [Plan](#12-day-plan).

---

## Architecture

```
 ┌──────────── Mininet (emulated network) ────────────┐
 │                                                     │
 │   h1 ──┐                         ┌── h4             │
 │   h2 ──┼── s1 ══ s2 ══ s3 ───────┼── h5             │
 │   h3 ──┘   (10 Mbit/s bottleneck)└── h6             │
 │                                                     │
 │   agent on each host ── UDP telemetry (JSON) ──┐    │
 └───────────────┬────────────────────────────────┼────┘
                 │ OpenFlow 1.3 (TCP :6653)       │ UDP
                 ▼                                ▼
        ┌─────────────────────────────────────────────┐
        │ Ryu controller                              │
        │  • learning-switch forwarding               │
        │  • polls port & flow stats (~5 s)           │
        │  • receives telemetry, correlates, alerts   │
        └──────────────────────┬──────────────────────┘
                               │ writes
                               ▼
                         SQLite database
                               │ reads
                               ▼
                     Streamlit dashboard (browser)
```

### Components

| Component | Folder | Runs on | Role |
|---|---|---|---|
| Topology | `topology/` | System Python (sudo) | Mininet network: 3 switches, 6 hosts, `TCLink` with fixed bandwidths |
| Telemetry agent | `agent/` | System Python inside each Mininet host | Sends CPU, memory, latency, byte counters as JSON over UDP every 2–5 s |
| Controller | `controller/` | `ryu-env` (Python 3.9) | Forwarding, OpenFlow stats polling, telemetry receiver, thresholds, alerts, SQLite |
| Dashboard | `dashboard/` | Own uv project (modern Python) | Live view of nodes, link utilization, and alerts |
| Server | `server/` | — | May be unused (see [Open decisions](#open-decisions)) |

### Protocols used

| Communication | Protocol | Why |
|---|---|---|
| Agents → controller (telemetry) | **UDP** | Lightweight; occasional loss is acceptable and is itself measured via sequence numbers |
| Controller ↔ switches | OpenFlow 1.3 over TCP :6653 | Required by OpenFlow; control messages must be reliable |
| Dashboard | HTTP | Separate from the monitored network |

---

## How monitoring works

**Telemetry message** (one per agent, every 2–5 s):
```json
{"node_id": "h1", "seq": 42, "timestamp": 1790000000.0,
 "cpu_pct": 12.5, "mem_pct": 40.1, "app_latency_ms": 3.2,
 "tx_bytes": 123456, "rx_bytes": 654321, "app_requests": 17}
```
- Gaps in `seq` → telemetry loss %.
- 3 missed intervals → node marked unreachable.

**Link utilization** (from OpenFlow port stats):
```
utilization % = (Δbytes × 8) / (interval_s × link_capacity_bps) × 100
```

**Static thresholds** (warning / critical):

| Metric | Warning | Critical |
|---|---|---|
| Link utilization | 70 % | 90 % |
| CPU | 80 % | 95 % |
| App latency | 100 ms | 300 ms |
| Telemetry loss | 5 % | 20 % |
| Missed heartbeats | 2 | 3 |
| New flows vs baseline | 2× | 5× |

Optional: dynamic thresholds using EWMA mean + 3σ.

**Correlation rules:**

| Condition | Diagnosis |
|---|---|
| High latency + high link utilization | Congestion |
| High latency/CPU + normal utilization | Host problem |
| No telemetry, but switch port still active | Agent failure |
| No telemetry and no port traffic | Link/node failure |
| Flow spike not matching app requests | Possible DoS |

**Alerts:** severity INFO / WARNING / CRITICAL, with de-duplication, cooldown,
and "resolved" messages. Logged to console, file, and SQLite.

---

## Setup

Environment: Ubuntu 26.04 (native), Mininet + Open vSwitch (apt), uv.

```bash
# System packages
sudo apt install mininet openvswitch-switch python3-psutil iperf iperf3 hping3

# Ryu controller environment (Python 3.9 — Ryu breaks on newer versions)
uv venv ryu-env --python 3.9
source ryu-env/bin/activate
uv pip install setuptools==67.6.1 wheel pbr
uv pip install --no-build-isolation ryu eventlet==0.30.2
```

---

## How to run

Use two terminals.

**Terminal 1: controller**
```bash
cd ~/projects/CN-MiniProject
source ryu-env/bin/activate
ryu-manager --ofp-tcp-listen-port 6653 ryu.app.simple_switch_13
```
> Don't forget the app name at the end — without it, Ryu accepts switches but
> forwards nothing (100 % packet loss).

**Terminal 2: network**
```bash
sudo python3 topology/topology.py
```

At the `mininet>` prompt:
```
pingall                          # expect 0% dropped
h4 iperf -s &                    # iperf server on h4 (background)
h1 iperf -c 10.0.0.4 -t 5        # expect ≈ bottleneck bandwidth (≈ 9.5 Mbit/s)
h4 kill %iperf                   # stop the server
dpctl dump-flows -O OpenFlow13   # flow rules installed by Ryu
exit
```
> Mininet's built-in `iperf h1 h4` command hangs on this setup (Mininet 2.3.0 +
> iperf 2.2.1), so run iperf manually as above. Type each command on its own
> line: Mininet replaces host names like `h1` with their IPs anywhere in the line.

**Cleanup** (always after exiting, and after any crash):
```bash
sudo mn -c
```

_This section will grow as the agent, monitoring controller, and dashboard are added._

---

## 12-day plan

| Day | Task | Status |
|---|---|---|
| 1 | Environment setup (Mininet, OVS, Ryu), pingall through Ryu | ✅ Done |
| 2 | Custom topology: 3 switches, 6 hosts, TCLink bandwidths, one bottleneck link | 🔄 In progress |
| 3 | UDP agent + basic receiver | ⬜ |
| 4 | Agents on all hosts, per-node tracking, seq loss detection, heartbeat timeouts | ⬜ |
| 5 | Ryu port/flow stats polling | ⬜ |
| 6 | Utilization calculation + SQLite storage | ⬜ |
| 7 | Static thresholds + alerting (cooldown, resolved) | ⬜ |
| 8 | Correlation rules + optional EWMA dynamic thresholds | ⬜ |
| 9 | Streamlit dashboard | ⬜ |
| 10 | Test scenarios: baseline, iperf congestion, CPU stress, node failure, hping3 flood, TCLink loss | ⬜ |
| 11 | Report + README | ⬜ |
| 12 | Full demo dry run, backup recording | ⬜ |

### Test scenarios (Day 10)

| Scenario | How | Expected alert |
|---|---|---|
| Baseline | Normal agent traffic | None |
| Congestion | `iperf` across the bottleneck | Link utilization + congestion |
| CPU stress | Stress process on one host | CPU + host problem |
| Node failure | Kill an agent / bring a link down | Agent failure / link failure |
| Flood | `hping3` flood | Possible DoS |
| Lossy link | `TCLink` with `loss=` | Telemetry loss |

---

## Open decisions

- **Where correlation and alerting live:** inside the Ryu app (UDP listener as a
  background green thread, one fewer process) or in a separate `server/`.
  Currently leaning towards inside the Ryu app.

---

## Project structure

```
CN-MiniProject/
├── agent/        # UDP telemetry agents
├── controller/   # Ryu app
├── server/       # possibly unused
├── topology/     # Mininet topology script
├── dashboard/    # Streamlit app
├── logs/         # gitignored
└── ryu-env/      # gitignored
```
