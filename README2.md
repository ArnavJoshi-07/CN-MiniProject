# Work Log: 3 October 2026

Summary of everything done today on the CN-MiniProject (SDN Network Monitoring and
Alert System). For the overall architecture and plan, see [README.md](README.md).

**Deliverable in progress:** D1 (due ~5 Oct). It is one end-to-end slice: agents send
telemetry over UDP to the controller, the controller combines it with port stats, and
at least one telemetry reading triggers a flow-rule change.

| D1 task | Status |
|---|---|
| 1. Topology + root-namespace monitor link | ✅ Done, verified |
| 2. UDP telemetry agent | ✅ Written, tested locally (not yet verified inside Mininet) |
| 3. Custom Ryu app with UDP listener | ⬜ Next |
| 4. Port stats polling + utilization | ⬜ |
| 5. Heartbeat checker | ⬜ |
| 6. Static threshold alerts | ⬜ |
| 7. Correlation + mitigation flow rule | ⬜ |
| 8. README update | ⬜ |

---

## 1. Environment check (end of Day 1)

Confirmed Mininet traffic is forwarded by the Ryu controller:

```bash
# Terminal 1
source ryu-env/bin/activate
ryu-manager --ofp-tcp-listen-port 6653 ryu.app.simple_switch_13

# Terminal 2
sudo mn --topo tree,depth=2,fanout=2 --controller=remote,ip=127.0.0.1,port=6653 \
        --switch ovsk,protocols=OpenFlow13
mininet> pingall          # 0% dropped
```

**What the Ryu command does**
- `ryu-manager` starts the Ryu framework. On its own it has no behavior: it loads apps,
  accepts switch connections, and passes OpenFlow events to the apps.
- `--ofp-tcp-listen-port 6653` makes it listen for switches on the standard OpenFlow port.
- `ryu.app.simple_switch_13` is a learning switch for OpenFlow 1.3. In SDN, switches start
  with empty flow tables and can't forward anything on their own. This app:
  1. installs a *table-miss* rule (priority 0) that sends unknown packets to the controller,
  2. learns which MAC address is on which port from each `PacketIn`,
  3. forwards the packet (or floods it if the destination is unknown),
  4. installs a priority-1 flow rule so later packets are forwarded by the switch directly.

**Problem hit: 100% dropped.** Ryu had been started as `ryu-manager --ofp-tcp-listen-port 6653`
without the app name. The switches connected, but no app installed any rules, so
every packet was dropped. Restarting Ryu with `ryu.app.simple_switch_13` fixed it.

---

## 2. Git housekeeping

- A test branch `1-test` was deleted on GitHub, but Git still showed the local copy as "up to date".
  Git's record of remote branches (`origin/1-test`) only updates on fetch.
  `git fetch --prune` removed the stale record, and then `git branch -d 1-test` removed the local branch.
- `git config fetch.prune true` makes every fetch/pull do this cleanup automatically.
- `.gitignore` now also ignores `CLAUDE.md` (a local assistant brief) and `__pycache__/`.

---

## 3. Topology (`topology/topology.py`), Task 1

```
 h1 ──┐                  ┌── h4
 h2 ──┼── s1 ══ s2 ══ s3 ┼── h5
 h3 ──┘  (bottleneck)    └── h6
       │
    monitor (10.0.0.100, root namespace, where Ryu receives telemetry)
```

### Design

| Item | Value |
|---|---|
| Switches | `s1`, `s2`, `s3` (OpenFlow 1.3, DPIDs 1, 2, 3) |
| Hosts | `h1`–`h6` at `10.0.0.1`–`10.0.0.6/8`, MACs `00:00:00:00:00:01`… (`autoSetMacs`) |
| Host links | 100 Mbit/s |
| `s1`–`s2` | **10 Mbit/s (bottleneck)** |
| `s2`–`s3` | 50 Mbit/s |
| Monitor | `10.0.0.100/8` in the root namespace, linked to `s1` (100 Mbit/s) |
| Controller | `RemoteController` at `127.0.0.1:6653` |

### Port map (needed later for the capacity map and mitigation rules)

| Switch | Port 1 | Port 2 | Port 3 | Port 4 | Port 5 |
|---|---|---|---|---|---|
| s1 | h1 | h2 | h3 | s2 | monitor |
| s2 | s1 | s3 | | | |
| s3 | h4 | h5 | h6 | s2 | |

Mininet numbers ports in the order links are added. The monitor link is added last,
so it is port 5 on s1.

### Key concepts

- **`TCLink`** uses Linux traffic control (`tc`) to give each virtual link a fixed bandwidth.
  Because each link's capacity is known exactly, link utilization can be calculated exactly.
- **Network namespaces:** each Mininet host has its own isolated copy of Linux
  networking. Ryu runs in the normal (root) namespace, which the hosts can't reach by default.
- **Monitor node:** `Node("monitor", inNamespace=False)` represents the root namespace.
  Linking it to `s1` and giving it `10.0.0.100` lets agents send telemetry to Ryu. This must be
  done *before* `net.start()` so s1 has the port when it starts (same pattern as Mininet's
  `examples/sshd.py`).
- **Agents start automatically:** after `net.start()`, the script runs `agent/agent.py` in the background on each
  host (`h.cmd("... &")`), logging to `logs/agent-hN.log`. If the agent file doesn't exist, this step is skipped.
  On exit, `h.cmd("kill %python3")` stops them. A `try/finally` makes sure agents are stopped and
  `net.stop()` runs even if the CLI crashes.

### Verification

| Test | Command | Result |
|---|---|---|
| Connectivity | `pingall` | ✅ 0% dropped |
| Bottleneck | `h6 iperf -s &` then `h1 iperf -c 10.0.0.6 -t 5` | ✅ 9.57 Mbit/s (limit 10) |
| Large packets | `h1 ping -c3 -s 1400 h6` | ✅ 0% loss |
| Monitor link | root terminal `nc -u -l 9999`; `h1 echo hi \| nc -u -w1 10.0.0.100 9999` | ✅ `hi` received |

### Problems hit

- **Switch names as variables:** `self.addSwitch(s1, ...)` raised a NameError. The names must be strings (`"s1"`).
- **Wrong links:** h4–h6 were first attached to `s2`, and `s2`–`s3` used the host bandwidth. Fixed.
- **Mininet's `iperf h1 h4` hangs.** Mininet 2.3.0 waits indefinitely for iperf 2.2.1 server output.
  Ping and large packets worked, and iperf run manually reached 9.57 Mbit/s, so the network itself was fine.
  **Workaround:** run iperf manually (now in README "How to run").
- **Two commands on one CLI line:** `h6 iperf -s & h1 iperf -c ...` didn't work because Mininet replaces
  host names with IPs anywhere in the line, so `h1` became `10.0.0.1`. Type one command per line.

---

## 4. Telemetry agent (`agent/agent.py`), Task 2

Runs inside each Mininet host using system `python3` + `psutil`. Every 2 s it sends one
JSON message over **UDP** to `10.0.0.100:9999`:

```json
{"node_id": "h1", "seq": 42, "timestamp": 1790000000.0,
 "cpu_pct": 12.5, "mem_pct": 40.1,
 "tx_bytes": 123456, "rx_bytes": 654321, "app_tx_bytes": 7140}
```

| Field | Source | Purpose |
|---|---|---|
| `seq` | +1 per message | Gaps show lost UDP messages |
| `timestamp` | `time.time()` | When the reading was taken |
| `cpu_pct`, `mem_pct` | `psutil` | Host load |
| `tx_bytes`, `rx_bytes` | `hN-eth0` counters | Total interface traffic so far |
| `app_tx_bytes` | Agent's own total | Bytes sent by the agent itself, including 42 B of Ethernet+IP+UDP headers so it is comparable with switch port counters |

All counters are totals so far. The controller calculates rates from the difference between two messages.

**Why UDP:** no connection setup and very little overhead. Losing a message now and then is acceptable,
and the `seq` numbers measure that loss. (By contrast, OpenFlow uses TCP because
control messages must be reliable.)

**Robustness**
- Sends on a fixed schedule, so the interval doesn't drift (this matters for the "3 missed intervals" heartbeat rule).
- Send errors are logged and skipped. `seq` still increases, so the controller counts them as lost.
- SIGTERM (`kill`) and Ctrl-C both exit cleanly. Log lines are flushed straight to the log file.

**Usage**
```bash
python3 agent/agent.py --node-id h1 --monitor-ip 10.0.0.100 --port 9999 --interval 2
```

**Tested:** locally against a UDP receiver: 3 messages with `seq` 1→3 and `app_tx_bytes` increasing,
then a clean exit on `kill`. Not yet tested inside Mininet.

**Known limitation:** Mininet hosts share one machine's CPU and memory, so `cpu_pct`/`mem_pct`
are the same on every host. CPU correlation is out of scope for D1.

---

## 5. Protocols in the system

| Communication | Protocol |
|---|---|
| Agents → controller (telemetry) | UDP :9999 |
| Controller ↔ switches | OpenFlow 1.3 over TCP :6653 |
| `pingall` | ICMP |
| `iperf` tests | TCP (or UDP with `-u`) |

---

## Next steps

1. Verify agents inside Mininet: all 6 nodes report every 2 s with increasing `seq`.
2. Task 3: `controller/monitor_app.py`, based on `simple_switch_13`, with a UDP listener
   (`hub.spawn`) and per-node state (last seen, seq, loss %).
