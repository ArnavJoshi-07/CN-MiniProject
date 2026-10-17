# CN-MiniProject: Network Monitoring and Alert System

A distributed network-monitoring system built on an emulated SDN network.
Hosts send application telemetry over **UDP**; an SDN controller combines it
with **OpenFlow port statistics** to detect abnormal conditions, raise alerts,
and install flow rules in response.

> **Status (17 Oct):** working on deliverable D1. The topology, agents,
> telemetry receiver, link-utilization polling and heartbeat alerts are verified
> in Mininet; utilization alerts are unit-tested and raise CRITICAL in Mininet
> (RESOLVED still to confirm); next is the mitigation rule. See [D1 progress](#d1-deliverable) and the work logs
> ([3 Oct](README2.md), [17 Oct](README3.md)).

---

## Architecture

```
 ┌───────────────────── Mininet (emulated network) ──────────────────────┐
 │                                                                       │
 │                    s1 (core) ─────── monitor 10.0.0.100               │
 │                  /     ║             (root namespace, where Ryu       │
 │       50 Mbit/s        ║ 10 Mbit/s    receives telemetry)             │
 │              /         ║ bottleneck                                   │
 │         s2 (agg)      s3 (agg)                                        │
 │        /    \        /    \          edge uplinks: 50 Mbit/s          │
 │      s4     s5     s6     s7         host links:  100 Mbit/s          │
 │    h1-h3  h4-h6  h7-h9  h10-h12                                       │
 │                                                                       │
 │   agent on every host ── UDP telemetry (JSON, every 2 s) ──> monitor  │
 └───────────────┬──────────────────────────────────────────┬────────────┘
                 │ OpenFlow 1.3 (TCP :6653)                 │ UDP :9999
                 ▼                                          ▼
        ┌──────────────────────────────────────────────────────────┐
        │ Ryu controller: controller/monitor_app.py                │
        │  • learning-switch forwarding (subclass of               │
        │    simple_switch_13)                                     │
        │  • telemetry listener: per-node seq, loss %, last seen   │
        │  • port stats every 2 s -> per-port link utilization     │
        │  • heartbeat checker: "unreachable" / "resolved" alerts  │
        │  • utilization alerts: WARNING 70 % / CRITICAL 90 %      │
        │  • next: correlation, mitigation                         │
        └─────────────────────────────┬────────────────────────────┘
                                      │ after D1
                                      ▼
                        SQLite database -> Streamlit dashboard
```

### Components

| Component | Folder | Runs on | Role |
|---|---|---|---|
| Topology | `topology/` | System Python (sudo) | Mininet 3-tier tree: 7 switches, 12 hosts, monitor node, `TCLink` bandwidths. Writes `logs/topology.json` and starts the agents |
| Telemetry agent | `agent/` | System Python inside each Mininet host | Sends CPU, memory and byte counters as JSON over UDP every 2 s |
| Controller | `controller/` | `ryu-env` (Python 3.9) | Forwarding, telemetry receiver, port stats polling and utilization, heartbeat and utilization alerts; later correlation, mitigation |
| Dashboard | `dashboard/` | Own uv project (modern Python) | After D1: live view of nodes, link utilization, and alerts |
| Server | `server/` | — | Unused for now (see [Design decisions](#design-decisions)) |

### Protocols used

| Communication | Protocol | Why |
|---|---|---|
| Agents → controller (telemetry) | **UDP** :9999 | Lightweight; occasional loss is acceptable and is itself measured via sequence numbers |
| Controller ↔ switches | OpenFlow 1.3 over TCP :6653 | Required by OpenFlow; control messages must be reliable |
| Dashboard | HTTP | Separate from the monitored network |

---

## Topology

`topology/topology.py` builds a loop-free tree. The learning switch has no
spanning tree, so a loop would make broadcasts circulate forever.

| Link | Capacity |
|---|---|
| Host ↔ edge switch (s4–s7) | 100 Mbit/s |
| Edge ↔ aggregation (s2, s3) | 50 Mbit/s |
| s2 ↔ s1 | 50 Mbit/s |
| **s3 ↔ s1** | **10 Mbit/s (bottleneck)** |
| Monitor ↔ s1 | 100 Mbit/s |

Hosts `h1`–`h12` are `10.0.0.1`–`10.0.0.12/8`, MACs `00:00:00:00:00:01`…`0c`.
The monitor node is `10.0.0.100` in the root namespace on s1 port 3.

**Port map** (port *n* of `sX` is Mininet interface `sX-ethn`):

| Switch | Port 1 | Port 2 | Port 3 | Port 4 |
|---|---|---|---|---|
| s1 | s2 | s3 | monitor | |
| s2 | s4 | s5 | s1 | |
| s3 | s6 | s7 | s1 | |
| s4 | h1 | h2 | h3 | s2 |
| s5 | h4 | h5 | h6 | s2 |
| s6 | h7 | h8 | h9 | s3 |
| s7 | h10 | h11 | h12 | s3 |

**`logs/topology.json`:** before `net.start()` the script writes the switches,
every switch port (peer + capacity in bit/s), where each host attaches, and the
monitor's port. The controller reads this file instead of hardcoded maps. OVS
can't supply the capacities itself: it reports each virtual interface's raw
speed, not the `TCLink` limit. The tree size is set by three constants at the
top of `topology.py`, and the file follows automatically.

The bottleneck is on s3–s1 on purpose. Test traffic from h1 to h10 fills the
s1 → s3 direction, while telemetry from h7–h12 goes s3 → s1 (the other
direction's queue), so the monitor still hears every host during congestion.

---

## How monitoring works

**Telemetry message** (one per agent, every 2 s):
```json
{"node_id": "h1", "seq": 42, "timestamp": 1790000000.0,
 "cpu_pct": 12.5, "mem_pct": 40.1,
 "tx_bytes": 123456, "rx_bytes": 654321, "app_tx_bytes": 7140}
```
- Counters are totals so far; the controller computes rates from differences.
- `app_tx_bytes` counts only what the agent itself sent (including headers), so
  the controller can tell application traffic apart from everything else.
- Gaps in `seq` → telemetry loss %. A lower `seq` with a newer timestamp means
  the agent restarted, and its counters are reset.
- **Heartbeats:** every second the controller checks when each node last sent
  telemetry. No message for more than 3 intervals (6 s) raises
  `ALERT CRITICAL unreachable hN`; the next message resolves it, whether the agent
  kept counting or restarted from `seq` 1.

**Link utilization** (from OpenFlow port stats, polled every 2 s):
```
utilization % = (Δbytes × 8) / (Δt × link_capacity_bps) × 100
```
- Computed per switch port and per direction: **rx** is traffic the switch
  received on the port, **tx** is traffic it sent out of it.
- Δt comes from the port uptime the switch reports with its counters, which is
  more accurate than the controller's clock.
- Capacities come from `logs/topology.json`. Ports above 1 % are logged.
- A full link reads about 96 %, not 100 %. Segmentation offload is on for the
  virtual interfaces, so Linux passes TCP data through them in large chunks and
  the interface counters include headers only once per chunk, while the
  `TCLink` shaper charges the headers of every real packet. On s1-eth2 the
  counter was 4.2 % below the shaper's byte count.

**Static thresholds** (warning / critical). D1 implements link utilization
(both levels) and missed heartbeats (critical level only); the rest come later.

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
| High ingress rate on a host's switch port + low `app_tx_bytes` rate | Traffic not explained by the application → drop rule on that port, telemetry still allowed (D1 task 7) |
| High latency + high link utilization | Congestion |
| High latency/CPU + normal utilization | Host problem |
| No telemetry, but switch port still active | Agent failure |
| No telemetry and no port traffic | Link/node failure |

**Alerts:** severity INFO / WARNING / CRITICAL, with de-duplication, cooldown,
and "resolved" messages. D1 logs them to the console; file and SQLite come later.
Every alert is one log line starting with `ALERT`, so `grep ALERT` shows them all:
```
ALERT CRITICAL unreachable h5: no telemetry for 6.4 s (3 intervals missed), last seq 57
ALERT RESOLVED unreachable h5: telemetry is back (seq 1) (alert lasted 21 s)
ALERT CRITICAL utilization s1->s3: s1-eth2 tx 96.5% of 10 Mbit/s (9.65 Mbit/s)
ALERT RESOLVED utilization s1->s3: s1-eth2 tx 0.1% of 10 Mbit/s (0.01 Mbit/s), under 70% for 10 s (alert lasted 34 s)
```
- **Identity and de-duplication:** an alert is identified by its kind and
  subject (`unreachable`, `h5`) and logged once while it is active. Only a
  change of severity logs a new line.
- **Utilization alerts are per link direction** (`s1->s3`), checked on every
  port stats reply. A link between two switches is measured at both ends
  (s1-eth2 tx and s3-eth3 rx count the same traffic), so each direction is
  judged by the tx counter of the switch that sends it, and a busy link raises
  one alert, not two. Hosts and the monitor have no port stats, so on their
  links the switch's rx counter covers the direction towards the switch
  (`h1->s4`).
- **Cooldown:** a higher severity is logged at once. A utilization alert is
  lowered (CRITICAL → WARNING) or resolved only after the link has stayed below
  its severity for 10 s, so a link hovering around 70 % keeps one alert open
  instead of raising and resolving it every 2 s. `unreachable` has no
  cooldown: the first message from the node resolves it.

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

Use two terminals. Start the controller first.

**Terminal 1: controller**
```bash
cd ~/projects/CN-MiniProject
source ryu-env/bin/activate
ryu-manager --ofp-tcp-listen-port 6653 controller/monitor_app.py 2>&1 | grep -v '^packet in'
```
> The `grep` hides the learning switch's `packet in ...` lines. Most of them are
> multicast noise (IPv6 router solicitations, mDNS, SSDP) from the hosts and from
> this machine's own services, which the learning switch floods and never
> installs rules for. To see only alerts and node events, filter with
> `grep -E 'ALERT|restarted|new node'` instead.

**Terminal 2: network**
```bash
sudo python3 topology/topology.py
```
The script writes `logs/topology.json`, starts an agent on every host (logs in
`logs/agent-hN.log`), and opens the `mininet>` prompt. The controller log shows:
```
telemetry: listening on udp 0.0.0.0:9999              (as soon as Ryu starts)
topology: loaded 7 switches, 12 hosts, 25 ports from .../logs/topology.json
switch: s1 connected (dpid 1)                         (one line per switch)
telemetry: new node h1                                (one per host, h1-h12)
telemetry: h1   seq=2     received=2     lost=0   loss=0.0%
```

At the `mininet>` prompt, one command per line:
```
pingall                          # expect 0% dropped
h10 iperf -s &                   # iperf server on h10 (background)
h1 iperf -c 10.0.0.10 -t 20      # crosses the s3–s1 bottleneck: ≈ 9.6 Mbit/s
h10 kill %iperf                  # stop the server
dpctl dump-flows -O OpenFlow13   # flow rules installed by Ryu
exit
```
During the iperf run the controller logs every busy port on the path every 2 s, e.g.
```
util: s1-eth2  -> s3       10 Mbit/s link | rx   0.1%   0.01 Mbit/s | tx  96.5%   9.65 Mbit/s
```
Measured mid-transfer: s1-eth2 tx and s3-eth3 rx 96.5 % (the bottleneck is full;
see [Link utilization](#how-monitoring-works) for why it isn't 100 %), the
50 Mbit/s links on the path 19.3 %, h1's and h10's ports 9.6 %. The first and
last rounds read lower because the transfer covers only part of those 2 s.

Expected alerts: `ALERT CRITICAL utilization s1->s3` within one polling round of
the start (possibly preceded by a WARNING if the first partial round lands
between 70 and 90 %), nothing else while it runs, and `ALERT RESOLVED
utilization s1->s3` about 10 s after iperf ends. No other link on the path
crosses 70 %.

> Mininet's built-in `iperf h1 h10` command hangs on this setup (Mininet 2.3.0 +
> iperf 2.2.1), so run iperf manually as above. Type each command on its own
> line: Mininet runs the whole line on the first host named and replaces other
> host names like `h1` with their IPs.

**Node failure (heartbeat test):**
```
h5 kill %python3                 # stop h5's agent -> ALERT CRITICAL unreachable h5 within ~7 s
h5 python3 agent/agent.py --node-id=h5 --monitor-ip 10.0.0.100 --port 9999 --interval 2 > logs/agent-h5.log 2>&1 &
                                 # agent back -> "h5 restarted", then ALERT RESOLVED within 1 s
```
> Write `--node-id=h5` with `=`: as a separate word, Mininet would replace `h5`
> with `10.0.0.5` and the agent would report under that name. Don't use
> `pkill -f agent.py`: Mininet hosts share one process list, so it would stop
> all 12 agents.

**Cleanup** (always after exiting, and after any crash):
```bash
sudo mn -c
```

---

## D1 deliverable

One end-to-end slice: agents → UDP → controller; the controller combines
telemetry with port stats; at least one telemetry-triggered flow-rule change.

| # | Task | Status |
|---|---|---|
| 1 | Topology + root-namespace monitor link (3-tier tree, `topology.json`) | ✅ Verified in Mininet |
| 2 | UDP telemetry agent | ✅ Verified in Mininet (12 agents, no send errors) |
| 3 | Ryu app: learning switch + UDP telemetry listener, per-node seq/loss | ✅ Running in Mininet |
| 4 | Port stats polling + utilization from `topology.json` | ✅ Verified in Mininet (bottleneck at 96.5 % during iperf h1 → h10) |
| 5 | Heartbeat checker (unreachable / resolved) | ✅ Verified in Mininet (h5 agent killed → CRITICAL, restarted → RESOLVED) |
| 6 | Static utilization alerts (70 / 90 %, de-dup + cooldown) | 🔄 Unit-tested; in Mininet iperf h1 → h10 raised CRITICAL `s1->s3` (93.9 %), RESOLVED still to confirm |
| 7 | Correlation + mitigation flow rules | ⬜ |
| 8 | README: mitigation path in diagram, test-case table | ⬜ |

## 12-day plan

| Day | Task | Status |
|---|---|---|
| 1 | Environment setup (Mininet, OVS, Ryu), pingall through Ryu | ✅ Done |
| 2 | Custom topology with TCLink bandwidths and a bottleneck link (grown to a 3-tier tree, 12 hosts, on 17 Oct) | ✅ Done |
| 3 | UDP agent + basic receiver | ✅ Done |
| 4 | Agents on all hosts, per-node tracking, seq loss detection, heartbeat timeouts | ✅ Done |
| 5 | Ryu port/flow stats polling | 🔄 Port stats done; flow stats later |
| 6 | Utilization calculation + SQLite storage | 🔄 Utilization done; SQLite after D1 |
| 7 | Static thresholds + alerting (cooldown, resolved) | 🔄 Link utilization + heartbeats done; CPU, loss, latency later |
| 8 | Correlation rules + optional EWMA dynamic thresholds | ⬜ |
| 9 | Streamlit dashboard | ⬜ |
| 10 | Test scenarios: baseline, iperf congestion, CPU stress, node failure, hping3 flood, TCLink loss | ⬜ |
| 11 | Report + README | ⬜ |
| 12 | Full demo dry run, backup recording | ⬜ |

### Test scenarios (Day 10)

| Scenario | How | Expected alert |
|---|---|---|
| Baseline | Normal agent traffic | None |
| Congestion | `iperf` h1 → h10 across the s3–s1 bottleneck | Link utilization + congestion |
| CPU stress | Stress process on one host | CPU + host problem |
| Node failure | Kill an agent / bring a link down | Agent failure / link failure |
| Flood | `hping3 --flood` from h1 | Traffic not explained by the application → drop rule |
| Lossy link | `TCLink` with `loss=` | Telemetry loss |

---

## Design decisions

- **Correlation and alerting live inside the Ryu app.** The UDP listener, the
  stats poller and the heartbeat checker are green threads (`hub.spawn`) in
  `monitor_app.py`, so there is one fewer process and no second channel between
  server and controller.
  `server/` is unused for now.
- **No hardcoded maps.** Port numbers, capacities and host locations come from
  `logs/topology.json`, re-read whenever a switch connects (Mininet rewrites it
  on every start, after Ryu is already running).

---

## Project structure

```
CN-MiniProject/
├── agent/
│   └── agent.py          # UDP telemetry agent (runs in each Mininet host)
├── controller/
│   └── monitor_app.py    # Ryu app: forwarding + telemetry + port stats + alerts
├── topology/
│   └── topology.py       # Mininet tree, monitor node, topology.json, agents
├── dashboard/            # Streamlit app (after D1)
├── server/               # unused for now
├── logs/                 # gitignored: topology.json, agent-hN.log
├── ryu-env/              # gitignored
├── README.md             # this file
├── README2.md            # work log, 3 Oct
└── README3.md            # work log, 17 Oct
```
