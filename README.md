# CN-MiniProject: Network Monitoring and Alert System

A distributed network-monitoring system built on an emulated SDN network.
Hosts send application telemetry over **UDP**; an SDN controller combines it
with **OpenFlow port statistics** to detect abnormal conditions, raise alerts,
and install flow rules in response.

> **Status (18 Oct):** deliverable D1 is complete. The topology, agents,
> telemetry receiver, link-utilization polling, heartbeat and utilization alerts,
> and the correlation + mitigation rule are all verified in Mininet (see the
> [D1 test cases](#d1-test-cases)). See [D1 progress](#d1-deliverable) and the work logs
> ([3 Oct](README2.md), [17 Oct](README3.md), [18 Oct](README4.md)).

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
 │                                                                       │
 │   mitigation, e.g. h1 floods: rules on its edge port s4-eth1 drop     │
 │   everything from h1 except its telemetry (and ARP for the monitor)   │
 └─────────┬──────────────▲────────────────────────────────────┬─────────┘
           │ port stats   │ FlowMods: mitigation rules         │ UDP :9999
           │ every 2 s    │ (hard_timeout 30 s)                │ telemetry
           ▼              │     OpenFlow 1.3 over TCP :6653    ▼
        ┌──────────────────────────────────────────────────────────┐
        │ Ryu controller: controller/monitor_app.py                │
        │  • learning-switch forwarding (subclass of               │
        │    simple_switch_13)                                     │
        │  • telemetry listener: per-node seq, loss %, last seen   │
        │  • port stats every 2 s -> per-port link utilization     │
        │  • heartbeat checker: "unreachable" / "resolved" alerts  │
        │  • utilization alerts: WARNING 70 % / CRITICAL 90 %      │
        │  • correlation: host port busy but its agent sent        │
        │    little -> "unexplained" alert + mitigation rules      │
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
| Controller | `controller/` | `ryu-env` (Python 3.9) | Forwarding, telemetry receiver, port stats polling and utilization, heartbeat and utilization alerts, correlation + mitigation flow rules |
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
| High ingress rate on a host's switch port + low `app_tx_bytes` rate | Traffic not explained by the application → drop rule on that port, telemetry still allowed (D1, see below) |
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

**Correlation + mitigation** (D1's telemetry-triggered flow-rule change). On
every port stats reply for a port a host attaches to, the controller compares
two views of what that host sends:

```
switch: rx rate of the host's port   = everything the host transmits
agent:  rate of its app_tx_bytes     = what the application itself sent
```

When the port receives at least 60 % of the host link and the agent accounts
for under 10 % of that, the traffic is not explained by the application (e.g.
an `hping3` flood). The controller raises `ALERT CRITICAL unexplained hN`
(cooldown 10 s) and installs three rules on the host's edge port, each with
`hard_timeout=30`:

| Priority | Match (all on the host's in_port) | Action |
|---|---|---|
| 200 | UDP to 10.0.0.100:9999 (telemetry) | Output on the port towards the monitor |
| 200 | ARP for 10.0.0.100 | Output on the port towards the monitor |
| 100 | anything else | Drop |

- **60 %, not 50 %:** a normal TCP flow to a host behind a 50 Mbit/s link
  reaches about 48 % of the 100 Mbit/s host link and must not be cut off.
- **The allow rules output directly** on the port towards the monitor (found by
  a breadth-first walk over `topology.json`): there is one flow table and the
  first matching rule ends the lookup, so they can't hand packets on to the
  learned flows. All three rules outrank simple_switch_13's learned flows
  (priority 1).
- **ARP is allowed** because nothing confirms a one-way UDP stream, so Linux
  re-checks the monitor's MAC every 15–45 s. Dropping that would stop
  telemetry too.
- **Renewal and expiry:** the switch counts received packets before the flow
  table, so the alert stays active while the flood continues, even though it
  is dropped. The rules are installed again in their last 4 s. Once the
  traffic stops, the alert resolves after the 10 s cooldown and the switch
  deletes the rules by itself within 30 s.
- The drop happens at the switch, so the host's own outgoing queue is still
  full during a flood and some of its telemetry may be lost.

The controller logs (rates vary per run):
```
ALERT CRITICAL unexplained h1: traffic not explained by the application: s4-eth1 rx 95.80 Mbit/s (95.8% of 100 Mbit/s), agent sent 1.1 kbit/s (0.00%)
MITIGATION h1: installed on s4-eth1 for 30 s: drop everything from h1 except telemetry (udp 10.0.0.100:9999) and arp for 10.0.0.100, sent out s4-eth4
MITIGATION h1: renewed on s4-eth1 for 30 s, traffic still not explained
ALERT RESOLVED unexplained h1: s4-eth1 rx ... (alert lasted ... s)
MITIGATION h1: rules on s4-eth1 expired (hard_timeout 30 s), its traffic is forwarded normally again
```

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

**Flood (correlation + mitigation test):**
```
h11 iperf -s &                                       # iperf server on h11
h2 iperf -c 10.0.0.11 -t 10                          # baseline across the bottleneck: ≈ 9.6 Mbit/s
h1 hping3 --flood --udp -p 5001 -d 1200 10.0.0.11 &  # h1 floods h11
                                 # -> ALERT CRITICAL unexplained h1, MITIGATION h1: installed on s4-eth1
sh ovs-ofctl -O OpenFlow13 dump-flows s4             # 3 rules with hard_timeout=30; the drop rule's n_packets keeps rising
h2 iperf -c 10.0.0.11 -t 10                          # recovers: h1's flood is dropped at s4
h1 pkill hping3                  # stop the flood -> ALERT RESOLVED after ~10 s, MITIGATION ... expired within 30 s
h1 ping -c3 10.0.0.11                                # h1 is forwarded normally again
h11 kill %iperf
```
While the flood runs, h1's seq numbers keep increasing in the controller log
(its telemetry passes the allow rule). Before the rules land, the flood also
fills h1's path, so utilization alerts on `s4->s2`, `s2->s1` and `s1->s3` may
appear; they resolve once the dropped traffic is off those links.
> Keep `-d 1200`: hping3's default packets are 54 bytes, and it may not send
> enough of them to reach the 60 % trigger on a 100 Mbit/s link.

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
| 6 | Static utilization alerts (70 / 90 %, de-dup + cooldown) | ✅ Verified in Mininet (iperf h1 → h10 raised CRITICAL `s1->s3` at 93.9 %, RESOLVED after the 10 s cooldown, no other link alerted) |
| 7 | Correlation + mitigation flow rules | ✅ Verified in Mininet (hping3 flood from h1 → drop rule on s4-eth1, h2 → h11 recovered, h1 telemetry continued) |
| 8 | README: mitigation path in diagram, test-case table | ✅ Done |

### D1 test cases

All run in Mininet with the commands in [How to run](#how-to-run).

| # | Test | How | Expected | Result |
|---|---|---|---|---|
| 1 | Connectivity | `pingall` | 0 % dropped | ✅ |
| 2 | All 12 nodes reporting | Start the topology, watch the controller log | `new node h1` … `h12`, seq increasing, 0 % loss | ✅ 12 agents, 312 msgs each, no send errors |
| 3 | Utilization under congestion | `iperf` h1 → h10 (crosses the s3–s1 bottleneck) | s1 → s3 ≈ 96 %, `CRITICAL utilization s1->s3`, `RESOLVED` ~10 s after iperf ends, no other link alerts | ✅ 96.5 % mid-transfer, CRITICAL raised at 93.9 %, RESOLVED after cooldown, no other alerts |
| 4 | Agent failure | `h5 kill %python3`, then restart the agent | `CRITICAL unreachable h5` within ~7 s, `RESOLVED` when it is back | ✅ |
| 5 | Unexplained traffic | `hping3 --flood` from h1 to h11 | `CRITICAL unexplained h1`, 3 rules on s4-eth1, h2 → h11 recovers, h1 telemetry continues, rules expire ≤ 30 s after the flood stops | ✅ |

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
| 8 | Correlation rules + optional EWMA dynamic thresholds | 🔄 "Traffic not explained" rule + mitigation done; other rules and EWMA later |
| 9 | Streamlit dashboard | ⬜ |
| 10 | Test scenarios: baseline, iperf congestion, CPU stress, node failure, hping3 flood, TCLink loss | 🔄 Congestion, agent failure and flood done for D1; the rest later |
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
- **Mitigation rules expire on their own.** They use `hard_timeout`, and the
  controller only installs them again while the traffic continues, so a host
  is never left cut off, even if the controller stops or restarts.

---

## Project structure

```
CN-MiniProject/
├── agent/
│   └── agent.py          # UDP telemetry agent (runs in each Mininet host)
├── controller/
│   └── monitor_app.py    # Ryu app: forwarding + telemetry + port stats + alerts + mitigation
├── topology/
│   └── topology.py       # Mininet tree, monitor node, topology.json, agents
├── dashboard/            # Streamlit app (after D1)
├── server/               # unused for now
├── logs/                 # gitignored: topology.json, agent-hN.log
├── ryu-env/              # gitignored
├── README.md             # this file
├── README2.md            # work log, 3 Oct
├── README3.md            # work log, 17 Oct
└── README4.md            # work log, 18 Oct
```
