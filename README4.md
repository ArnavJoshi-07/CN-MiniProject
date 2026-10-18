# Work Log: 18 October 2026

Summary of everything done today on the CN-MiniProject (SDN Network Monitoring and
Alert System). For the overall architecture and how to run it, see [README.md](README.md).
The previous log is [README3.md](README3.md) (17 Oct).

This log also covers the Task 7 code, which was written late on 17 Oct, after
README3.md was finished, and tested in Mininet today.

**Deliverable:** D1 (due 19 Oct). It is one end-to-end slice: agents send
telemetry over UDP to the controller, the controller combines it with port stats, and
at least one telemetry reading triggers a flow-rule change.

| D1 task | Status at end of day |
|---|---|
| 1. Topology + root-namespace monitor link | ✅ (17 Oct) |
| 2. UDP telemetry agent | ✅ (17 Oct) |
| 3. Custom Ryu app with UDP listener | ✅ (17 Oct) |
| 4. Port stats polling + utilization | ✅ (17 Oct) |
| 5. Heartbeat checker | ✅ (17 Oct) |
| 6. Static threshold alerts | ✅ RESOLVED and "no other link alerting" confirmed in Mininet |
| 7. Correlation + mitigation flow rule | ✅ Written and unit-tested (17 Oct evening), checked in Mininet |
| 8. README update | ✅ Mitigation path in the diagram, correlation section, flood test, D1 test-case table |

D1 is complete. Everything is committed and pushed (`fe3074a`), and a pull request
into `main` is open.

---

## 1. Utilization alerts confirmed (Task 6)

What was still open from 17 Oct: confirm that `ALERT RESOLVED utilization s1->s3`
appears about 10 s after iperf ends, and that no other link alerts.

Same test as yesterday, at the `mininet>` prompt:
```
h10 iperf -s &
h1 iperf -c 10.0.0.10 -t 20
```
✅ The run raised one `CRITICAL utilization s1->s3`, resolved it after the 10 s
cooldown, and no other link alerted (the 50 Mbit/s links on the path stay near
19 %). The controller output wasn't saved, so no timings are recorded for this run.
The CRITICAL value in the README (93.9 %) is from the 17 Oct run.

---

## 2. Correlation + mitigation (`controller/monitor_app.py`), Task 7

This is D1's telemetry-triggered flow-rule change. No new thread: every port stats
reply for a port that a host attaches to is now also compared with that host's
telemetry.

### How it works

1. **The agent's own send rate** (`app_rate`, called from `_update_node`): for each
   new, in-order message,
   ```
   app_tx_bps = Δapp_tx_bytes × 8 / Δtimestamp
   ```
   between this message and the previous one. Both values come from the agent:
   `app_tx_bytes` is a running total and `timestamp` is the host's clock. So if a
   message in between is lost, the next rate simply covers 4 s instead of 2 and is
   still right. It is `None` until a node has sent two messages (and again after an
   agent restart). A normal agent sends about 1.1 kbit/s.
2. **The check** (`_check_unexplained`, called at the end of `_update_port` when the
   port's peer is a host): compares two views of what the host sends.
   ```
   switch: rx rate of the host's port   = everything the host transmits
   agent:  app_tx_bps                   = what the application itself sent
   share = app_tx_bps / rx_bps × 100     (an idle port counts as 100 % explained)
   ```
   If the port receives **at least 60 %** of the host link (`UNEXPLAINED_MIN_UTIL`)
   and **share is under 10 %** (`UNEXPLAINED_MAX_SHARE`), the traffic is not
   explained by the application:
   ```
   ALERT CRITICAL unexplained h1: traffic not explained by the application: s4-eth1 rx 96.00 Mbit/s (96.0% of 100 Mbit/s), agent sent 1.1 kbit/s (0.00%)
   ```
   It goes through the same `_raise_alert` / `_resolve_alert` helpers as the other
   alerts, so it gets de-duplication for free, plus a 10 s cooldown
   (`ALERT_COOLDOWN["unexplained"]`).
3. **Mitigation** (`_mitigate` → `_install_mitigation`): three FlowMods on the host's
   edge switch, all matching the host's port and all with `hard_timeout=30`:

   | Priority | Match (on the host's `in_port`) | Action |
   |---|---|---|
   | 200 | IPv4, UDP, `ipv4_dst=10.0.0.100`, `udp_dst=9999` (telemetry) | Output on the port towards the monitor |
   | 200 | ARP, `arp_tpa=10.0.0.100` | Output on the port towards the monitor |
   | 100 | anything else | Drop (no instructions) |

   ```
   MITIGATION h1: installed on s4-eth1 for 30 s: drop everything from h1 except telemetry (udp 10.0.0.100:9999) and arp for 10.0.0.100, sent out s4-eth4
   ```
4. **Port towards the monitor** (`ports_towards_monitor`): a breadth-first walk over
   `topology.json`, starting from the monitor's switch. A switch first reached from
   its neighbour n reaches the monitor through its port to n. The tree has no loops,
   so that is the only path. Result: `{1: 3, 2: 3, 3: 3, 4: 4, 5: 4, 6: 4, 7: 4}`
   (s1 port 3 is the monitor, every other switch goes up its uplink).
5. **Renewal and expiry.** `self.mitigations` remembers, per host, the switch, port
   and when the rules expire.
   - The flood keeps triggering the check even while it is being dropped (see below),
     so while it continues the rules are sent again in their last 4 s
     (`MITIGATION_RENEW` = 2 polling intervals, so one poll always lands in that
     window): `MITIGATION h1: renewed on s4-eth1 for 30 s, traffic still not explained`.
   - Once the traffic stops, the alert resolves after the 10 s cooldown and the switch
     deletes the rules itself when the 30 s run out. On the first poll after that
     time, the controller forgets them and logs
     `MITIGATION h1: rules on s4-eth1 expired (hard_timeout 30 s), its traffic is forwarded normally again`.
   - When a switch disconnects, its mitigations are forgotten: a restarted switch
     has lost its rules, and installing a rule that is still there just replaces it.

### Decisions and why

- **60 %, not 50 %.** The first plan said "over 50 %". But a TCP flow to a host
  behind a 50 Mbit/s link (h1 → h4 goes over s4–s2 at 50 Mbit/s) reaches about 48 %
  of the 100 Mbit/s host link. That is too close to 50 % to be safe, and traffic
  like that is normal even though no agent reports it.
- **The switch counts packets before the flow table.** A dropped packet still shows
  up in the port's rx counter. That is what lets the alert stay active while the
  flood is being dropped, and what drives the renewal.
- **The allow rules output directly** on the port towards the monitor instead of
  handing packets back to the learning switch. There is one flow table and the first
  matching rule ends the lookup, so a priority-200 rule can't "fall through" to the
  learned priority-1 flows. Outputting to a fixed port also means the rule works even
  if the learning switch hasn't seen the monitor's MAC yet.
- **ARP for the monitor is allowed too** (not in the task brief). Nothing answers a
  one-way UDP stream, so Linux re-checks the monitor's MAC with an ARP request every
  15–45 s. If that request were dropped, telemetry would stop a few seconds later.
- **The matches list every lower layer.** OpenFlow only accepts a field if the layer
  below it is matched too: `udp_dst` needs `ip_proto=UDP`, which needs
  `eth_type=IPv4`. Leaving them out makes the switch reject the FlowMod.
- **Priorities 200 / 100** are above simple_switch_13's learned flows (priority 1),
  so the drop rule wins over any learned path out of the host's port.
- **`hard_timeout`, not deletes from the controller.** The timeout lives on the
  switch, so if the controller crashes or is restarted, the host is unblocked within
  30 s anyway. Renewing works because installing a rule with the same match and
  priority replaces it with a fresh timeout (OpenFlow 1.3).
- **Drop at the edge port.** The flood is stopped at the first switch, before it
  reaches the s3–s1 bottleneck, and the host's telemetry keeps flowing.

### Tests

Unit test with a fake clock, fake telemetry for h1 and h2 (270 B per message, like
the real agent), fake port stats, and a fake switch that serializes every FlowMod
(the real `_update_node`, `_update_port`, alert helpers and `logs/topology.json`, no
Ryu threads). All cases passed:

| Case | Result |
|---|---|
| Idle | No alerts, no rules; h1's agent rate 1080 bit/s |
| TCP-like traffic at 48 % of the host link (behind a 50 Mbit/s link) | No `unexplained` alert |
| 80 % rx, agent reports 80 Mbit/s (the app explains it) | Only `WARNING utilization h1->s4`; no `unexplained` |
| Flood: 96 % rx, agent sends only telemetry | One `CRITICAL unexplained h1`, 3 FlowMods to s4 only (`hard_timeout` 30, `idle_timeout` 0, priorities 200 / 200 / 100, output port 4). A `CRITICAL utilization h1->s4` fires as well |
| Flood continues, every other telemetry message lost | No repeated alert or install for 22 s, then exactly one `renewed` 26 s after the install |
| Flood stops | `RESOLVED` exactly 10 s after the last flood sample; `expired` exactly 30 s after the renewal; nothing sent to the switch |
| Flood again | Installed afresh |
| s4 disconnects and reconnects | Mitigation forgotten. With no path to the monitor: `MITIGATION h1: cannot install on s4-eth1 (...)`, nothing sent, no crash. Path back: installed |
| h3 floods before its agent has sent two messages | No `unexplained` (nothing to compare), only `CRITICAL utilization h3->s4` |
| `app_rate` across a lost message | 2000 bit/s (timestamps 10 → 14 s, 0 → 1000 B); counter going backwards → `None` |

The serialized FlowMods were also decoded by Open vSwitch's own parser
(`ovs-ofctl ofp-parse`), which confirms the switch will accept them:
```
OFPT_FLOW_MOD (OF1.3) (xid=0x1): ADD priority=200,udp,in_port=1,nw_dst=10.0.0.100,tp_dst=9999 hard:30 out_port:0 actions=output:4
OFPT_FLOW_MOD (OF1.3) (xid=0x2): ADD priority=200,arp,in_port=1,arp_tpa=10.0.0.100 hard:30 out_port:0 actions=output:4
OFPT_FLOW_MOD (OF1.3) (xid=0x3): ADD priority=100,in_port=1 hard:30 out_port:0 actions=drop
```
The Task 4, 5 and 6 unit tests still pass, and the app loads under `ryu-manager`.

**Problems hit while testing (both in the test, not the app):**
- **A false alarm at 80 %.** The first run of the "app explains it" case raised
  `unexplained` and installed rules. The fake agent added the app's bytes *after*
  building its message, so its report was always one round behind the port stats.
  The real agent counts the bytes before it reports them; the test was changed to do
  the same and the case passed. No change to the app.
- **"Cannot install while disconnected" can't happen.** The first version of the
  disconnect case expected that log line while s4 was gone. But a disconnected switch
  sends no port stats, so the controller can't see a flood behind it at all. The case
  was changed to "reconnected, but no path to the monitor".

### Verification in Mininet

The flood test from README.md, one command per line:
```
h11 iperf -s &
h2 iperf -c 10.0.0.11 -t 10                          # baseline across the bottleneck
h1 hping3 --flood --udp -p 5001 -d 1200 10.0.0.11 &  # h1 floods h11
sh ovs-ofctl -O OpenFlow13 dump-flows s4             # the 3 rules, drop rule's n_packets rising
h2 iperf -c 10.0.0.11 -t 10                          # recovers
h1 pkill hping3                                      # RESOLVED after ~10 s, expired within 30 s
h1 ping -c3 10.0.0.11                                # forwarded normally again
```
✅ The flood from h1 raised `CRITICAL unexplained h1`, the drop rule appeared on
s4-eth1, h2 → h11 recovered, and h1's telemetry kept arriving. As with Task 6, the
controller output and iperf numbers from this run weren't saved. The log lines in
README.md are samples ("rates vary per run"), not measurements.

Points to know before running it:
- **Keep `-d 1200`.** hping3's default packets are 54 bytes, and it may not send
  enough of them to reach 60 % of a 100 Mbit/s link.
- **Run `pingall` first**, so the learned flows exist before the flood. Otherwise
  the flood's first packets all go up to the controller.
- **Don't run `h1 iperf -c 10.0.0.2` (or any host on the same edge switch) during
  the demo.** Two hosts on s4 talk at about 96 Mbit/s, the agent doesn't report
  iperf's traffic, and h1 would be cut off. See known gaps.
- Wait about 30 s after stopping the flood before the final `ping`, so the rules have
  expired.

---

## 3. Documentation (Task 8)

README.md changes:
- **Status line:** D1 complete, with a link to the test cases.
- **Architecture diagram:** a note on the mitigation path inside the Mininet box
  (h1 floods → rules on s4-eth1), and three labelled arrows between Mininet and the
  controller: port stats every 2 s, FlowMods with the mitigation rules, UDP telemetry.
  A correlation bullet in the controller box. One arrow junction was a column off and
  was fixed.
- **New "Correlation + mitigation" section:** the switch-versus-agent comparison, the
  60 % / 10 % trigger, the three-rule table, why 60 not 50, direct output, ARP,
  renewal and expiry, possible telemetry loss, and the log format.
- **How to run:** the flood test commands and notes (`-d 1200`, possible utilization
  alerts on h1's path before the rules land).
- **D1 table** all ✅, and a new **D1 test-case table**: connectivity, 12 nodes
  reporting, utilization under congestion, agent failure, unexplained traffic.
- **12-day plan** (days 8 and 10 updated), a design note on why mitigation rules expire
  on their own, and the project-structure comments.

---

## 4. Git: commit, push, pull request

- **Commit `fe3074a`** "Add correlation + mitigation flow rules and finish D1 README"
  (README.md and `controller/monitor_app.py`), pushed to `d1/topology-and-agent`.
- **Pull request into `main`.** The previous PR (#2) was *squash-merged*: GitHub
  turned the branch's four commits into one new commit on `main`. The branch still
  had the four originals, so Git saw two unrelated histories that both added
  `README.md` and `topology/topology.py`, and a new PR would have conflicted in those
  files and `.gitignore`.
  - `main`'s files were exactly the same as the branch's commit `bd5ffb9`, so
    nothing on `main` was missing from the branch.
  - Fix: `git merge -s ours origin/main`. The `ours` strategy records `main` as merged
    but keeps the branch's files unchanged. The PR now shows only the 17–18 Oct work
    and merges cleanly.
  - Next time: after a squash merge, start the next deliverable on a fresh branch
    from `main` instead of reusing the old one.
- **README4.md**: this log.

---

## Known gaps

- **The rule can't tell a flood from an unreported bulk transfer.** Any traffic the
  agent doesn't count looks "unexplained", so `h1 iperf -c 10.0.0.2` (about
  96 Mbit/s inside s4) would get h1 cut off. The 60 % threshold only spares traffic
  limited by a 50 Mbit/s uplink. A real fix needs applications to report their own
  traffic, or per-flow matching, both after D1.
- **Mitigation is coarse.** While the rules are active, h1 can't even ping; only its
  telemetry and ARP for the monitor get through.
- **The flooding host loses some telemetry.** The drop happens at the switch, so h1's
  own outgoing queue is still full of flood packets competing with the agent's.
- **Telemetry is not authenticated.** A host could report a large `app_tx_bytes` to
  make its flood look explained.
- **The agent's rate is never marked stale** (noticed while writing this log). If an
  agent goes silent, the check keeps using its last `app_tx_bps`. A silent agent
  raises `unreachable` anyway, so this matters little for D1.
- **The unit tests aren't in the repo.** They were scratch scripts; adding them as a
  `tests/` folder would let them be re-run after changes.

---

## Next steps

1. Merge the pull request into `main`.
2. Before submitting: one clean run of all five D1 test cases in a single session
   (start with `sudo mn -c`), saving the controller log, e.g.
   `... 2>&1 | grep --line-buffered -v '^packet in' | tee logs/controller.log`.
   Put the real numbers from the flood run into the README test-case table.
3. After D1 (next deliverable): flow stats, SQLite, CPU / loss / latency alerts,
   EWMA thresholds, the Streamlit dashboard, the CPU-stress and lossy-link scenarios,
   the report and the demo dry run.
