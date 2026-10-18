# Work Log: 17 October 2026

Summary of everything done today on the CN-MiniProject (SDN Network Monitoring and
Alert System). For the overall architecture and how to run it, see [README.md](README.md).
The previous log is [README2.md](README2.md) (3 Oct).

**Deliverable in progress:** D1 (due 19 Oct). It is one end-to-end slice: agents send
telemetry over UDP to the controller, the controller combines it with port stats, and
at least one telemetry reading triggers a flow-rule change.

| D1 task | Status at end of day |
|---|---|
| 1. Topology + root-namespace monitor link | ✅ Rebuilt as a 3-tier tree, verified in Mininet, committed (`ffed525`) |
| 2. UDP telemetry agent | ✅ Verified in Mininet: 12 agents, no send errors |
| 3. Custom Ryu app with UDP listener | ✅ Written, tested with fake packets, running in Mininet |
| 4. Port stats polling + utilization | ✅ Written, unit-tested, verified in Mininet with iperf |
| 5. Heartbeat checker | ✅ Written, unit-tested, verified in Mininet (kill / restart h5's agent) |
| 6. Static threshold alerts | 🔄 Written, unit-tested; CRITICAL seen in Mininet, RESOLVED still to confirm |
| 7. Correlation + mitigation flow rule | ⬜ |
| 8. README update | 🔄 README.md brought up to date (diagram, topology, run steps, heartbeat test); test-case table still to do |

---

## 1. Bigger topology (Task 1, redone)

**Request:** the teacher asked for a bigger topology "so that I can monitor each node".
The old network was 3 switches and 6 hosts in a line. We took this to mean a network
shaped like a real one, with more hosts and switches, where the monitor tracks every
node and every link separately.

### New design

```
                  s1 (core) ── monitor 10.0.0.100 (port 3)
                /    ║
           50 Mbit   ║ 10 Mbit (bottleneck)
              /      ║
        s2 (agg)    s3 (agg)
        /    \       /    \
      s4     s5    s6     s7         edge switches, 50 Mbit uplinks
    h1-h3  h4-h6 h7-h9  h10-h12      100 Mbit host links, 10.0.0.1–12
```

| Item | Value |
|---|---|
| Switches | 7: core `s1`, aggregation `s2`–`s3`, edge `s4`–`s7` (DPIDs 1–7) |
| Hosts | `h1`–`h12` at `10.0.0.1`–`10.0.0.12/8`, 3 per edge switch |
| Links | hosts 100, edge↔agg 50, s2↔s1 50, **s3↔s1 10 Mbit/s** |
| Monitor | `10.0.0.100/8`, root namespace, s1 port 3 |

### Decisions and why

- **No loops.** `simple_switch_13` has no spanning tree. In a looped network, ARP
  broadcasts would circulate forever and the network would melt down. Redundant
  paths would need STP, which is out of scope for D1.
- **Size is set by three constants** (`NUM_AGG`, `EDGES_PER_AGG`, `HOSTS_PER_EDGE`).
  `build()` loops over them, so making the tree bigger again means changing numbers only.
- **Port numbers follow the order links are added.** Hosts are linked first, so on
  every edge switch they are ports 1–3 and the uplink is port 4. Aggregation→core
  links are added last, so every switch has its downlinks on the low ports.
- **The bottleneck is s3–s1, not s2–s1, on purpose.** Each direction of a link has its
  own queue. Test traffic h1 → h10 fills the s1 → s3 queue, while telemetry from
  h7–h12 travels s3 → s1, the other direction. So the monitor still hears every host
  while the bottleneck is congested.
- **`logs/topology.json` replaces hand-written maps.** With 7 switches, typing every
  port's capacity and every host's switch port by hand would be error-prone. The
  switches can't report the capacities either: OVS reports each virtual interface's
  raw speed (usually 10 Gbit/s), not the limit `TCLink` sets. So `export_topology()`
  reads the built network before `net.start()` and writes switches, every switch port
  (peer + `bw_bps`), host attachment points and the monitor's port.
- **The docstring became `r"""`.** The `\` characters in the diagram were acting as
  escape characters and joining lines together.

### Verification (in Mininet)

| Test | Result |
|---|---|
| `pingall` | ✅ 0% dropped |
| Agents | ✅ All 12 agents sent 312 messages each (about 10 minutes), no send errors |
| `logs/topology.json` | ✅ 12 hosts, 25 switch ports |

Committed as `ffed525` "Grow topology into a 3-tier tree and export topology.json".
The agent needed no changes: it already takes `--node-id` and finds its interface by name.

---

## 2. Controller app (`controller/monitor_app.py`), Task 3

### How it works

- **It reuses the learning switch by subclassing `SimpleSwitch13`.** The parent's
  handlers still run, so forwarding works exactly as before. Ryu only starts apps
  defined in the file it is given, so importing `SimpleSwitch13` doesn't start a
  second copy of it.
- **The UDP listener runs in a green thread** (`hub.spawn`), bound to `0.0.0.0:9999`.
  Ryu is built on eventlet: all green threads share one OS thread and take turns,
  switching only while one is waiting (for example on `recvfrom()`). So waiting for
  telemetry never blocks OpenFlow handling, and the shared dicts need no locks.
  `ryu-manager` patches the standard `socket` module for this, which is why a plain
  socket works.
- **Bad packets are logged and skipped.** `parse_telemetry()` rejects bad JSON,
  non-objects, missing fields, a `seq` that isn't a positive integer, and non-numeric
  counters (the rate maths in Task 7 would crash on something like `"app_tx_bytes": null`).
- **Each node gets its own state:** `last_seen`, `last_seq`, `received`, `lost` and the
  latest message. `last_seen` uses `time.monotonic()`, a clock that only moves forward.

| Incoming `seq` | What happens |
|---|---|
| First message from a node | Counting starts at that `seq`, so starting Ryu after the agents doesn't count fake losses |
| Jump (3 → 5) | The skipped numbers are added to `lost`; loss % = lost / (received + lost) |
| Lower, with a **newer** timestamp | The agent restarted (e.g. Mininet restarted while Ryu kept running): counters reset |
| Lower, with an **older** timestamp | A late, reordered packet: ignored |

### Tests

- **Fake packets** (Ryu on a spare OpenFlow port, 15 hand-made UDP packets): a jump from
  seq 2 to 5 gave `lost=2 loss=40.0%`; a late seq 4 was ignored; a node first seen at
  seq 50 started with no loss; an agent restart (`seq 5 -> 1`) reset the counters; all 6
  malformed packets were skipped with a reason; a valid packet after them was processed.
- **In Mininet:** the stock controller was swapped for `monitor_app.py` while Mininet
  kept running. Over 10 s, 61 packets arrived
  on `monitor-eth0` (12 agents × 5 messages, plus one other packet), the controller's
  UDP socket had an empty queue and 0 drops, so it is reading every message.

---

## 3. The `packet in ...` flood in the controller log

With the stock controller running, the log was full of lines like
`packet in 4 00:00:00:00:00:03 33:33:00:00:00:02 3` (dpid, source MAC, destination MAC,
in-port). This turned out to be harmless background traffic. Every destination was a
multicast address:

| Destination MAC | What it is |
|---|---|
| `33:33:00:00:00:02` | IPv6 router solicitation |
| `33:33:00:00:00:fb`, `01:00:5e:00:00:fb` | mDNS (avahi-daemon) |
| `01:00:5e:7f:ff:fa` | SSDP / UPnP discovery |

- **Why each packet appears 7 times:** the learning switch floods multicast and never
  installs a flow rule for flooded traffic, so every such packet goes up to the
  controller from all 7 switches. Unicast traffic (like telemetry) gets a rule after
  the first packet and disappears from the log.
- **Where the unknown MACs come from** (matched with `ip link`):
  - `de:3b:52:2e:be:b8` is `monitor-eth0` (s1 port 3): this machine's own avahi and
    SSDP services send multicast out through the monitor link.
  - `86:a6:…`, `5a:b7:…`, `b2:5b:…` are `s5-eth4`, `s6-eth4`, `s7-eth4`. Switch
    interfaces live in the root namespace, so the host OS sends IPv6 and mDNS packets
    out of them too.
  - `00:00:00:00:00:0X` are the Mininet hosts sending their own IPv6 router solicitations.
- **No risk of a storm:** the tree has no loops.
- **Fix:** filter the log: `ryu-manager ... 2>&1 | grep -v '^packet in'` (now in the
  README). `MonitorApp` prints the same lines because it subclasses `SimpleSwitch13`.
  Optional later: disable IPv6 inside the hosts and on the switch interfaces.

---

## 4. Bottleneck test

```
mininet> h10 iperf -s &
mininet> h1 iperf -c 10.0.0.10 -t 20
```

| Run | Result |
|---|---|
| h1 → h10 (crosses s3–s1) | ✅ 9.59 Mbit/s and 9.60 Mbit/s (limit 10 Mbit/s; TCP/IP headers take the rest) |

**Problem hit again: two commands on one CLI line.** `h10 iperf -s & h1 iperf -c 10.0.0.10 -t 20`
ran entirely inside h10's shell as `iperf -s & 10.0.0.1 iperf -c ...`, because Mininet sends
the whole line to the first host named and replaces other host names with their IPs.
Result: `bash: 10.0.0.1: command not found`. One command per line.

---

## 5. Port stats polling and utilization (`controller/monitor_app.py`), Task 4

Added to the same app, as a second green thread plus two event handlers.

### How it works

1. **Topology file** (`_load_topology`): reads `logs/topology.json` into
   - `capacity`: `{(dpid, port): bit/s}`
   - `port_peer`: `{(dpid, port): "s3"}` (for readable logs)
   - `switch_names`: `{1: "s1", ...}`
   - `host_port`: `{"h1": (4, 1), ...}` (for the Task 7 mitigation rule)

   It is read **when a switch connects, not when Ryu starts.** Ryu is started before
   Mininet, and `topology.py` rewrites the file on every start, just before its
   switches connect. A file-modification-time check means 7 switches connecting
   cause only one read, and a changed topology is picked up without restarting Ryu.
2. **Switch tracking** (`EventOFPStateChange`): a switch is added to `datapaths` when
   its OpenFlow handshake finishes (`MAIN_DISPATCHER`) and removed when it
   disconnects (`DEAD_DISPATCHER`). On disconnect its old counters are dropped too.
3. **Poller thread** (`_stats_poller`): every 2 s sends each switch one
   `OFPPortStatsRequest(..., OFPP_ANY)`, which asks for the counters of all its ports.
4. **Reply handler** (`EventOFPPortStatsReply`): for every port it keeps the last
   counters and computes, per direction,
   ```
   rate (bit/s) = Δbytes × 8 / Δt
   utilization % = rate / capacity × 100
   ```
   - **rx** = traffic the switch received on the port (sent by the peer);
     **tx** = traffic it sent out of the port. For a host's port, rx is what the host
     sends, which is what Task 7 needs.
   - **Δt comes from the switch**, not the controller's clock: each counter reply
     carries the port's uptime (`duration_sec` + `duration_nsec`) at the moment the
     switch read the counters. The reply can wait in queues before Ryu handles it, so
     the controller's clock would add jitter.
   - **LOCAL is skipped:** OVS also reports the bridge's own internal port
     (number above `OFPP_MAX`), which isn't a link.
   - **Counter resets:** if counters or uptime go backwards (the port was recreated by
     a Mininet restart), the sample becomes a new baseline instead of a negative rate.
   - Ports above 1 % are logged, so an idle network stays quiet:
     ```
     util: s1-eth2  -> s3       10 Mbit/s link | rx   0.1%   0.01 Mbit/s | tx  96.5%   9.65 Mbit/s
     ```
     Port names match the Mininet interfaces (`s1-eth2` = s1 port 2).

### Tests

Unit test with fake OpenFlow events (real Ryu `OFPPortStats` objects, the real
`logs/topology.json`), all checks passed:

| Case | Result |
|---|---|
| Switch connects | Topology loaded once (7 switches, 12 hosts, 25 ports), switch registered |
| Poller | One `OFPPortStatsRequest(OFPP_ANY)` per connected switch |
| s1-eth2 sends 2.4 MB in 2.0 s | tx 9.60 Mbit/s = **96.0 %** of 10 Mbit/s |
| s1-eth1 receives 480 kB in 2.0 s | rx 1.92 Mbit/s = 3.84 % of 50 Mbit/s |
| Uneven 2.5 s interval | Rate still 9.60 Mbit/s (uses the switch's Δt) |
| LOCAL port in the reply | Ignored |
| Counters reset | New baseline, no negative rate |
| Port missing from `topology.json` | Warned once, rate kept, no utilization |
| Old connection's late disconnect | Ignored; real disconnect removes switch and its counters |

The app also loads cleanly under `ryu-manager` (tried on a spare OpenFlow port).

### Verification in Mininet

The controller was restarted with the new code while Mininet kept running; all 7
switches reconnected. Then `h1 iperf -c 10.0.0.10 -t 20` (iperf: 25.1 MBytes in
21.98 s, 9.59 Mbit/s). One round of `util:` lines from the middle of the transfer:

| Port | Link | Reading |
|---|---|---|
| s4-eth1 rx (from h1) | 100 Mbit/s | 9.6 % (9.64 Mbit/s) |
| s4-eth4 tx, s2-eth1 rx, s2-eth3 tx, s1-eth1 rx | 50 Mbit/s | 19.3 % (9.65 Mbit/s) |
| **s1-eth2 tx, s3-eth3 rx** | **10 Mbit/s (bottleneck)** | **96.5 % (9.65 Mbit/s)** |
| s3-eth2 tx, s7-eth4 rx | 50 Mbit/s | 19.3 % (9.65 Mbit/s) |
| s7-eth1 tx (to h10) | 100 Mbit/s | 9.6 % (9.65 Mbit/s) |

- **All 10 ports on the path agree** to within 0.01 Mbit/s, although the numbers come
  from 5 different switches, each reporting its own counters and uptime. So the rate
  maths and the capacities from `topology.json` are right.
- **The first round read lower:** bottleneck 76.5 % (7.65 Mbit/s), upstream ports
  8.18 Mbit/s. That 2 s window only partly overlapped the transfer, and upstream was
  higher than the bottleneck because TCP's start-up burst was filling the queue in
  front of s1-eth2.
- **Why a full link reads 96.5 %, not 100 %:** segmentation offload (TSO/GSO) is on
  for the virtual interfaces, so Linux passes TCP data through them in large chunks
  (about 25 KB on average here). The interface counters, which OVS reports, include
  one set of headers per chunk. The `TCLink` shaper (HTB, 10 Mbit/s) charges the
  headers of every real 1514-byte packet. On s1-eth2: counter 90,117,332 bytes in
  3,599 chunks, shaper 94,110,680 bytes in 64,105 packets, a ratio of 0.958.
  96.5 % / 0.958 ≈ 100 %, so the link is full. For D1 this doesn't matter, since
  96.5 % is well above the 90 % critical threshold. Turning offload off
  (`ethtool -K <intf> tso off gso off`) would make the counters exact, at a CPU cost.

---

## 6. Heartbeat checker (`controller/monitor_app.py`), Task 5

Added to the same app as a third green thread, plus two small alert helpers that
Task 6 will reuse.

### How it works

- **Checker thread** (`_heartbeat_checker`): every 1 s it calls `_check_heartbeats()`,
  which compares each node's `last_seen` with the current time.
  - Silent for more than 3 intervals × 2 s = 6 s →
    `ALERT CRITICAL unreachable h5: no telemetry for 6.4 s (3 intervals missed), last seq 57`
  - Fresh again → `ALERT RESOLVED unreachable h5: telemetry is back (seq 1) (alert lasted 21 s)`
  - Checking every second instead of every 2 s means a dead node is reported 6–7 s
    after its last message instead of 6–8 s.
- **Alert helpers:**
  - `_raise_alert(kind, subject, severity, text)` logs an alert once. Active alerts
    are kept in `self.alerts`, keyed by `(kind, subject)`, e.g. `("unreachable", "h5")`.
    Raising the same alert again is ignored until it is resolved (de-duplication).
    Raising it with a different severity is logged as a change, so a WARNING can
    become a CRITICAL (Task 6).
  - `_resolve_alert(kind, subject, text)` logs `RESOLVED` with how long the alert
    lasted, and does nothing if that alert isn't active.
  - Every alert line starts with `ALERT`, so `grep ALERT` shows them all. The log
    level matches the severity (`logging.CRITICAL`, `WARNING`; `INFO` for resolved).

### Decisions and why

- **Alerts are kept in their own dict, not as a flag in the node's state.** A
  restarted agent starts again at `seq` 1, and the restart branch of `_update_node`
  replaces the node's whole state. A flag stored there would be wiped, so the
  `RESOLVED` line would never be printed.
- **Raising and resolving both happen in the checker, based only on `last_seen`.**
  Late (reordered) packets don't update `last_seen`, so they don't count as signs of
  life. A node that comes back is resolved the same way whether its agent kept
  counting or restarted. The cost is that `RESOLVED` can come up to 1 s late.
- **`silent > 6 s`, not `>= 6 s`.** The third message is due exactly 6 s after the
  last one, so `>=` could fire on a message that is only a few milliseconds late.
- **Only the CRITICAL level** (3 missed intervals), as in the task brief. The
  README's WARNING at 2 missed intervals would be one more `if`.
- **Known gap:** a host whose agent never sends anything never appears in
  `self.nodes`, so it is never alerted. Seeding the node list from the hosts in
  `topology.json` would fix that; the D1 test doesn't need it.

### Tests

Unit test with a fake clock (the real `_update_node` and `_check_heartbeats`, no Ryu
threads), 45 simulated seconds, all checks passed:

| Case | Result |
|---|---|
| h1 sends every 2 s throughout | Never alerted |
| h2 goes silent | One CRITICAL 7 s after its last message (checks aligned with messages, the worst case), no repeats while silent |
| h2 comes back with a `seq` gap | RESOLVED, 3 messages counted as lost |
| h2 silent again, then restarts at `seq` 1 | CRITICAL, then "h2 restarted" and RESOLVED |
| WARNING → WARNING → CRITICAL → CRITICAL | 2 lines logged (the change only) |
| Resolve an alert that isn't active | Nothing logged |

### Verification in Mininet

With the new controller running and all 12 nodes reporting, at the `mininet>` prompt:
```
h5 kill %python3
h5 python3 agent/agent.py --node-id=h5 --monitor-ip 10.0.0.100 --port 9999 --interval 2 > logs/agent-h5.log 2>&1 &
```
Killing the agent raised `ALERT CRITICAL unreachable h5`; restarting it logged
"h5 restarted" followed by `ALERT RESOLVED unreachable h5`. ✅

Two Mininet traps in these commands (both now in the README):
- **`--node-id=h5` needs the `=`.** The Mininet CLI replaces every separate word
  that is a host name with that host's IP, so `--node-id h5` would become
  `--node-id 10.0.0.5`: the agent would report as a new node and h5 would stay
  unreachable.
- **`pkill -f agent.py` would stop all 12 agents.** Mininet hosts have their own
  network namespace but share one process list. `kill %python3` only kills the job
  started by that host's own shell.

---

## 7. Utilization alerts (`controller/monitor_app.py`), Task 6

No new thread: every port stats reply is now also checked against static
thresholds, and the alert helpers from Task 5 got a cooldown.

### How it works

- **Thresholds** (`UTIL_THRESHOLDS`): WARNING from 70 %, CRITICAL from 90 %.
  `util_severity(util)` returns the highest one reached, or `None`.
- **Check** (`_check_util`, called at the end of `_update_port` once a port has a
  utilization): raises or resolves one alert per link direction, named after the
  direction, e.g. `utilization s1->s3`:
  - every port's **tx** is judged, as the direction from that switch to its peer;
  - a port whose peer is a host or the monitor also has its **rx** judged, as the
    direction from the peer to the switch (`h1->s4`, `monitor->s1`).
- **Cooldown** (`ALERT_COOLDOWN = {"utilization": 10}`): each active alert now
  remembers when it last saw each severity (`"seen"`). The checkers call
  `_raise_alert` on every check that finds the problem, which is what keeps
  `"seen"` up to date.
  - New alert or higher severity → logged at once.
  - Lower severity → logged only once nothing higher has been seen for 10 s.
  - Problem gone → `RESOLVED` only once no severity has been seen for 10 s.
  - Same severity again → nothing (de-duplication, as before).
  - Kinds not in `ALERT_COOLDOWN` (`unreachable`) behave exactly as in Task 5.
- Example log for iperf h1 → h10:
  ```
  ALERT CRITICAL utilization s1->s3: s1-eth2 tx 96.5% of 10 Mbit/s (9.65 Mbit/s)
  ALERT RESOLVED utilization s1->s3: s1-eth2 tx 0.1% of 10 Mbit/s (0.01 Mbit/s), under 70% for 10 s (alert lasted 34 s)
  ```

### Decisions and why

- **One alert per link direction, judged at the sending port.** On a link between
  two switches, s1-eth2 tx and s3-eth3 rx count the same s1 → s3 traffic. Alerting
  per port would report the full bottleneck twice. Using the sender's tx gives every
  switch-to-switch direction exactly one judge. Hosts and the monitor have no
  OpenFlow counters, so their outgoing direction can only be read as the switch's rx.
- **Per direction, not per link.** The two directions have separate queues: during
  iperf h1 → h10, s1 → s3 is full while s3 → s1 is nearly idle (that is why telemetry
  from h7–h12 still arrives). One alert per link would hide which way is congested.
- **Cooldown as a hold-down on lowering, not a pause before raising again.** The
  other common meaning, "after RESOLVED, don't raise the same alert for N s", still
  raises and resolves a hovering link every N s, and it hides a real overload that
  starts during the pause. Here a hovering link keeps one alert open, and getting
  worse is never delayed. The price: `RESOLVED` comes 10 s after the link calms down.
- **"Seen" per severity, not one "calm since" timer.** With a single timer,
  95 % → 80 % for 8 s → 0 % would resolve immediately on the first 0 % sample (the
  link had been "below CRITICAL" for 10 s), even though it was still above 70 % 2 s
  earlier. Keeping the last time of each severity makes lowering and resolving each
  wait 10 s after the last reading that justified the higher state.
- **The cooldown is per kind.** `unreachable` resolves on the first fresh check: a
  message arriving is a fact, not a noisy measurement.
- **Known gap:** if Mininet exits while an alert is active, that alert stays open
  (no more stats arrive to calm it) until the switches reconnect and report 10 s of
  low traffic. The `alert lasted` time then includes the downtime.

### Tests

Unit test with a fake clock and fake port stats replies (the real `_update_port`,
`_check_util`, alert helpers and `logs/topology.json`, no Ryu threads), all passed:

| Case | Result |
|---|---|
| Idle links | No alerts |
| Bottleneck at 96.5 % on s1-eth2 tx and s3-eth3 rx, 5 rounds | One `CRITICAL s1->s3`, no second alert for the rx end, no repeats |
| Back to 0 % | `RESOLVED` exactly 10 s after the last busy sample |
| 75 % then 95 % | WARNING, then CRITICAL 2 s later (escalation not delayed) |
| CRITICAL, then 80 % for 12 s | Lowered to WARNING after 10 s |
| Hovering 68 % / 72 % for 40 s | Nothing logged: one WARNING stays open |
| 95 %, 80 % once, then 0 % | `RESOLVED` 10 s after the 80 % sample, no WARNING line in between |
| WARNING, one 91 % spike | CRITICAL at once, back to WARNING 10 s later |
| s4-eth1 rx 80 %, s1-eth3 rx 95 %, s4-eth1 tx 92 % | WARNING `h1->s4`, CRITICAL `monitor->s1`, CRITICAL `s4->h1` |
| s3-eth3 rx 96 % alone (switch-to-switch rx) | No alert |
| Heartbeat after the change | `unreachable` still resolves on the first fresh check |

The app still imports under `ryu-env` (Python 3.9).

### Verification in Mininet

🔄 Partly done. With the controller restarted (Mininet kept running; the switches
reconnected) and iperf h1 → h10 as in README.md, the controller logged
```
ALERT CRITICAL utilization s1->s3: s1-eth2 tx 93.9% of 10 Mbit/s (9.39 Mbit/s)
```
✅ The alert is logged once, by the first sample at or above 90 %. That sample
covers TCP's ramp-up, so it reads below the steady 96.5 %. The steady samples have
the same severity and are not logged again.

Still to confirm: no alert for any other link, and `ALERT RESOLVED utilization
s1->s3` about 10 s after iperf ends.

---

## 8. Documentation

- **README.md** brought up to date: new architecture diagram with the tree and the
  monitor node, topology section (capacities, port map, `topology.json`), actual
  telemetry format, how utilization is computed, heartbeats and the alert log format,
  run instructions for `monitor_app.py` with the log filter, the h1 → h10 iperf test
  and the heartbeat test, D1 progress table, updated plan.
- **README3.md**: this log.

---

## Next steps

1. Task 6: confirm `RESOLVED` (and no other link alerting) in Mininet with iperf h1 → h10.
2. Task 7: correlation (high rx on a host's port + low `app_tx_bytes` rate) → allow
   telemetry, drop everything else from that port, `hard_timeout=30`. Test with
   `hping3 --flood` from h1.
3. Task 8: README test-case table and mitigation path in the diagram.
