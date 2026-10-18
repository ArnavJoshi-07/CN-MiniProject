"""
Ryu app: learning switch + telemetry monitor + port statistics + alerts.

Forwarding is unchanged from Ryu's simple_switch_13 (this class subclasses
it, so its PacketIn and switch-features handlers still run). On top of that
it runs three green threads.

1. Telemetry listener: receives the agents' UDP telemetry on 0.0.0.0:9999
   and keeps per-node state:

    nodes["h3"] = {"last_seen": 1234.5,  # time.monotonic() of last message
                   "last_seq": 41,       # highest seq seen so far
                   "received": 40,       # messages that arrived
                   "lost": 1,            # seq numbers that never arrived
                   "app_tx_bps": 1080.0, # agent's own send rate, from
                                         # app_tx_bytes (None until 2 msgs)
                   "telemetry": {...}}   # latest message, as sent

   Loss is counted from gaps in seq: seq 5 arriving right after seq 3 means
   seq 4 was lost.

2. Port stats poller: every 2 s asks each switch for its port counters and
   turns each reply into rates and link utilization:

    port_stats[(1, 2)] = {"time": 812.4,       # port uptime reported by s1
                          "rx_bytes": 51200,   # counters as read
                          "tx_bytes": 9876543,
                          "rx_bps": 4.1e4,     # bit/s over the last interval
                          "tx_bps": 9.6e6,     # (None until 2 samples exist)
                          "rx_util": 0.4,      # % of the link's capacity
                          "tx_util": 96.0}

   rx is traffic the switch received on that port (sent by the peer), tx is
   traffic it sent out of that port (towards the peer). Utilization is

    Δbytes × 8 / (Δt × capacity_bps) × 100

   Capacities come from logs/topology.json, written by topology/topology.py.
   OVS can't supply them: it reports the raw speed of each virtual
   interface, not the bandwidth limit TCLink puts on it.

3. Heartbeat checker: every second, any node that has sent no telemetry for
   3 intervals (6 s) gets a CRITICAL "unreachable" alert; when its telemetry
   comes back the alert is resolved. Alerts are logged like this, so
   `grep ALERT` shows them all:

    ALERT CRITICAL unreachable h5: no telemetry for 6.4 s (3 intervals missed), last seq 57
    ALERT RESOLVED unreachable h5: telemetry is back (seq 1) (alert lasted 21 s)

Utilization alerts need no thread of their own: every port stats reply is
checked against static thresholds, WARNING from 70 % and CRITICAL from 90 %,
once per link direction ("s1->s3"):

    ALERT CRITICAL utilization s1->s3: s1-eth2 tx 96.5% of 10 Mbit/s (9.65 Mbit/s)
    ALERT RESOLVED utilization s1->s3: s1-eth2 tx 0.1% of 10 Mbit/s (0.01 Mbit/s), under 70% for 10 s (alert lasted 34 s)

A higher severity is logged at once. A lower one, or RESOLVED, only after the
link has stayed below the active severity for 10 s (the cooldown), so a link
hovering around a threshold doesn't log a new line every 2 s.

Correlation + mitigation also runs on every port stats reply, for the ports
hosts attach to. It compares two views of what a host sends:

    switch: rx rate of the host's port   = everything the host transmits
    agent:  rate of its app_tx_bytes     = what the application itself sent

When the port carries at least 60 % of the host link and the agent accounts
for under 10 % of that, the traffic is not explained by the application
(e.g. an hping3 flood):

    ALERT CRITICAL unexplained h1: traffic not explained by the application: s4-eth1 rx 95.80 Mbit/s (95.8% of 100 Mbit/s), agent sent 1.1 kbit/s (0.00%)

and the controller installs three flow rules for that port on the host's
switch, each with hard_timeout=30, so the switch deletes them by itself:

    priority 200  udp to 10.0.0.100:9999  -> port towards the monitor
    priority 200  arp for 10.0.0.100      -> port towards the monitor
    priority 100  anything else           -> drop

The host's telemetry keeps flowing, everything else it sends is dropped at
the first switch. All three outrank simple_switch_13's learned flows
(priority 1). While the traffic keeps arriving the rules are renewed just
before they expire; once it stops they time out within 30 s.

Run (from the project root, inside ryu-env):
    ryu-manager --ofp-tcp-listen-port 6653 controller/monitor_app.py
"""

import json
import logging
import os
import socket
import time

from ryu.app.simple_switch_13 import SimpleSwitch13
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, DEAD_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.lib import hub
from ryu.lib.packet import ether_types, in_proto


# 0.0.0.0 = every interface of the root namespace, including the monitor
# link's 10.0.0.100 (topology/topology.py).
TELEMETRY_PORT = 9999
TELEMETRY_ADDR = ("0.0.0.0", TELEMETRY_PORT)
MAX_DATAGRAM = 4096        # agent messages are ~200 bytes

# Every agent message has these fields (see agent/agent.py).
TELEMETRY_NUMBERS = ("timestamp", "cpu_pct", "mem_pct",
                     "tx_bytes", "rx_bytes", "app_tx_bytes")

# Written by topology/topology.py on every Mininet start. Absolute path, so
# ryu-manager can be started from any folder.
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOPOLOGY_JSON = os.path.join(PROJECT_DIR, "logs", "topology.json")

STATS_INTERVAL = 2         # seconds between port stats requests
LOG_MIN_UTIL = 1.0         # only log ports busier than this (%), so idle is quiet

TELEMETRY_INTERVAL = 2     # seconds between agent messages (--interval in topology.py)
MISSED_LIMIT = 3           # intervals without telemetry before a node is unreachable
HEARTBEAT_CHECK = 1        # seconds between heartbeat checks

# Link utilization (%) at which an alert is raised, most severe first.
UTIL_THRESHOLDS = (("CRITICAL", 90.0), ("WARNING", 70.0))

# Alert severity -> logging level of its log line. Logging levels are
# ordered (WARNING 30 < CRITICAL 50), so they also tell which is more severe.
ALERT_LEVELS = {"WARNING": logging.WARNING, "CRITICAL": logging.CRITICAL}

# Alert kind -> cooldown in seconds: how long its problem must stay below the
# active severity before the alert is lowered or resolved. Rates measured
# every 2 s are noisy, so without it a link hovering around 70 % would be
# raised and resolved over and over. Kinds not listed resolve at once: for
# "unreachable", a message arriving is not noise.
ALERT_COOLDOWN = {"utilization": 10, "unexplained": 10}

# Correlation rule: traffic from a host is "not explained by the application"
# when its switch port receives at least UNEXPLAINED_MIN_UTIL % of the host
# link and the agent's own traffic is under UNEXPLAINED_MAX_SHARE % of that.
# 60, not 50: a TCP flow to a host behind a 50 Mbit/s link reaches ~48 %,
# and normal (if unreported) traffic like that shouldn't be cut off.
UNEXPLAINED_MIN_UTIL = 60.0
UNEXPLAINED_MAX_SHARE = 10.0

# Mitigation flow rules. simple_switch_13's learned flows have priority 1.
ALLOW_PRIORITY = 200       # telemetry (and ARP) to the monitor still passes
DROP_PRIORITY = 100        # everything else from the host's port is dropped
MITIGATION_TIMEOUT = 30    # hard_timeout (s): the switch deletes the rules then
# While the traffic continues, install the rules again this long before they
# expire. Checks come every STATS_INTERVAL, so this window always gets one.
MITIGATION_RENEW = 2 * STATS_INTERVAL


def parse_telemetry(data):
    """Decode one datagram into a dict, or raise ValueError saying why."""
    # json.loads raises ValueError for bad JSON and for bytes that aren't
    # valid UTF-8 (both errors are ValueError subclasses).
    msg = json.loads(data)
    if not isinstance(msg, dict):
        raise ValueError("not a JSON object")
    missing = [f for f in ("node_id", "seq") + TELEMETRY_NUMBERS
               if f not in msg]
    if missing:
        raise ValueError("missing " + ", ".join(missing))
    if not isinstance(msg["node_id"], str) or not msg["node_id"]:
        raise ValueError("node_id is not a non-empty string")
    if not isinstance(msg["seq"], int) or msg["seq"] < 1:
        raise ValueError("seq is not a positive integer")
    for f in TELEMETRY_NUMBERS:
        if not isinstance(msg[f], (int, float)):
            raise ValueError(f"{f} is not a number")
    return msg


def new_node_state(msg):
    return {"last_seen": time.monotonic(), "last_seq": msg["seq"],
            "received": 1, "lost": 0, "app_tx_bps": None, "telemetry": msg}


def app_rate(prev, msg):
    """bit/s the agent sent between two of its messages, or None.

    app_tx_bytes is cumulative and the timestamps are the agent's own, so the
    rate stays right even when the messages in between were lost.
    """
    dt = msg["timestamp"] - prev["timestamp"]
    d_bytes = msg["app_tx_bytes"] - prev["app_tx_bytes"]
    if dt <= 0 or d_bytes < 0:
        return None
    return d_bytes * 8 / dt


def loss_pct(state):
    total = state["received"] + state["lost"]
    return 100.0 * state["lost"] / total if total else 0.0


def util_severity(util):
    """Severity of the highest threshold util (%) reaches, or None."""
    for severity, threshold in UTIL_THRESHOLDS:
        if util >= threshold:
            return severity
    return None


def ports_towards_monitor(topo):
    """dpid -> the port through which that switch reaches the monitor.

    Walks out from the monitor's switch: a switch first reached from its
    neighbour n reaches the monitor through its port to n. The topology is
    loop-free, so that is the only way there.
    """
    switches = topo["switches"]                   # "s1" -> dpid
    # (dpid, neighbour's dpid) -> port, for every switch-to-switch link
    links = {(p["dpid"], switches[p["peer"]]): p["port"]
             for p in topo["ports"] if p["peer"] in switches}
    monitor = topo["monitor"]
    towards = {monitor["dpid"]: monitor["port"]}
    reached = [monitor["dpid"]]
    for n in reached:          # grows while we loop over it: breadth-first
        for (dpid, peer), port in links.items():
            if peer == n and dpid not in towards:
                towards[dpid] = port
                reached.append(dpid)
    return towards


class MonitorApp(SimpleSwitch13):
    """simple_switch_13 forwarding + telemetry tracking + port utilization."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.nodes = {}          # node_id -> state dict (see module docstring)

        # Filled from logs/topology.json when switches connect.
        self.topology_mtime = None   # file's mtime at the last read
        self.capacity = {}       # (dpid, port) -> link capacity in bit/s
        self.port_peer = {}      # (dpid, port) -> node at the other end
        self.switch_names = {}   # dpid -> "s1"
        self.host_port = {}      # "h1" -> (dpid, port) it attaches to
        self.monitor_ip = None   # "10.0.0.100"
        self.monitor_port = {}   # dpid -> port towards the monitor

        self.datapaths = {}      # dpid -> Datapath, switches connected now
        self.port_stats = {}     # (dpid, port) -> state dict (see docstring)
        self.unknown_ports = set()   # ports with no capacity, already warned

        # "h1" -> {"dpid": 4, "port": 1, "expires": monotonic time} for every
        # host whose mitigation rules should be on its switch now.
        self.mitigations = {}

        # (kind, subject) -> {"severity": "CRITICAL", "since": monotonic time,
        #                     "seen": {"WARNING": t, "CRITICAL": t}}
        # for every alert active now, e.g. ("unreachable", "h5"). "seen" is
        # when each severity was last reported, for the cooldown. Kept apart
        # from self.nodes: an agent restart replaces its node state, and that
        # must not make the alert forget it was raised.
        self.alerts = {}

        # hub.spawn starts a green thread. Ryu runs on eventlet: all green
        # threads share one OS thread and switch only while one is waiting
        # (on recvfrom, hub.sleep, ...), so the dicts above need no locks.
        self.telemetry_thread = hub.spawn(self._telemetry_listener)
        self.stats_thread = hub.spawn(self._stats_poller)
        self.heartbeat_thread = hub.spawn(self._heartbeat_checker)

    # --- Telemetry ---------------------------------------------------------

    def _telemetry_listener(self):
        # A plain socket is fine: ryu-manager monkey-patches the socket
        # module, so recvfrom() yields to other green threads while waiting
        # instead of blocking OpenFlow handling.
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(TELEMETRY_ADDR)
        except OSError as e:
            self.logger.error("telemetry: cannot bind udp %s:%d (%s). "
                              "Is another listener (nc, python) still "
                              "running?", *TELEMETRY_ADDR, e)
            sock.close()
            return
        self.logger.info("telemetry: listening on udp %s:%d", *TELEMETRY_ADDR)

        while True:
            data, (ip, port) = sock.recvfrom(MAX_DATAGRAM)
            try:
                msg = parse_telemetry(data)
            except ValueError as e:
                self.logger.warning("telemetry: skipped malformed packet "
                                    "from %s:%d: %s", ip, port, e)
                continue
            self._update_node(msg)

    def _update_node(self, msg):
        node_id, seq = msg["node_id"], msg["seq"]
        state = self.nodes.get(node_id)

        if state is None:
            self.logger.info("telemetry: new node %s", node_id)
            state = self.nodes[node_id] = new_node_state(msg)
        elif seq > state["last_seq"]:
            # Every seq skipped since the previous message was lost on the way.
            state["lost"] += seq - state["last_seq"] - 1
            state["received"] += 1
            state["last_seq"] = seq
            state["last_seen"] = time.monotonic()
            state["app_tx_bps"] = app_rate(state["telemetry"], msg)
            state["telemetry"] = msg
        elif msg["timestamp"] > state["telemetry"]["timestamp"]:
            # Lower seq but a newer reading: the agent restarted (e.g. Mininet
            # was restarted while Ryu kept running). Start counting afresh.
            self.logger.info("telemetry: %s restarted (seq %d -> %d)",
                             node_id, state["last_seq"], seq)
            state = self.nodes[node_id] = new_node_state(msg)
        else:
            # An older reading arriving late (reordered or duplicated by the
            # network). Its seq has already been accounted for.
            self.logger.debug("telemetry: %s late seq %d ignored",
                              node_id, seq)
            return

        self.logger.info("telemetry: %-4s seq=%-5d received=%-5d lost=%-3d "
                         "loss=%.1f%%", node_id, seq, state["received"],
                         state["lost"], loss_pct(state))

    # --- Heartbeats ---------------------------------------------------------

    def _heartbeat_checker(self):
        # Checking every second (not every 2 s) means a silent node is
        # reported 6-7 s after its last message instead of 6-8 s.
        while True:
            hub.sleep(HEARTBEAT_CHECK)
            self._check_heartbeats(time.monotonic())

    def _check_heartbeats(self, now):
        """Raise or resolve the "unreachable" alert of every node seen so far.

        Both happen here, based only on last_seen, so a node that comes back
        is resolved the same way whether its agent kept counting or restarted
        from seq 1. Resolving waits for the next check, at most 1 s.
        """
        limit = MISSED_LIMIT * TELEMETRY_INTERVAL
        for node_id, state in self.nodes.items():
            silent = now - state["last_seen"]
            if silent > limit:
                self._raise_alert(
                    "unreachable", node_id, "CRITICAL",
                    "no telemetry for %.1f s (%d intervals missed), "
                    "last seq %d" % (silent, silent // TELEMETRY_INTERVAL,
                                     state["last_seq"]))
            else:
                self._resolve_alert("unreachable", node_id,
                                    "telemetry is back (seq %d)"
                                    % state["last_seq"])

    # --- Alerts ------------------------------------------------------------

    def _raise_alert(self, kind, subject, severity, text):
        """Report that a problem is present now, at this severity.

        kind + subject name the problem ("unreachable", "h5"). Checkers call
        this on every check that finds the problem, not only the first, so
        the alert knows when each severity was last seen. Logged:
        - a new alert, or a higher severity than the active one: at once;
        - a lower severity: once nothing higher has been seen for the kind's
          cooldown (ALERT_COOLDOWN);
        - the active severity again: never (de-duplication).
        """
        key = (kind, subject)
        now = time.monotonic()
        active = self.alerts.get(key)
        if active is None:
            self.alerts[key] = {"severity": severity, "since": now,
                                "seen": {severity: now}}
        else:
            active["seen"][severity] = now
            if severity == active["severity"]:
                return
            if (ALERT_LEVELS[severity] < ALERT_LEVELS[active["severity"]]
                    and not self._cooled_down(kind, active, severity, now)):
                return
            active["severity"] = severity
        self.logger.log(ALERT_LEVELS[severity], "ALERT %-8s %s %s: %s",
                        severity, kind, subject, text)

    def _resolve_alert(self, kind, subject, text):
        """Report that a problem is gone now.

        The alert is resolved once no severity of it has been seen for the
        kind's cooldown, so at once for kinds without one. Does nothing if
        it isn't active.
        """
        key = (kind, subject)
        now = time.monotonic()
        active = self.alerts.get(key)
        if active is None or not self._cooled_down(kind, active, None, now):
            return
        del self.alerts[key]
        self.logger.info("ALERT RESOLVED %s %s: %s (alert lasted %.0f s)",
                         kind, subject, text, now - active["since"])

    def _cooled_down(self, kind, active, severity, now):
        """True if the alert has seen nothing more severe than `severity`
        (None: nothing at all) for the kind's cooldown."""
        floor = ALERT_LEVELS[severity] if severity else logging.NOTSET
        # Never empty: the active severity is in "seen" and is above floor.
        last = max(t for s, t in active["seen"].items()
                   if ALERT_LEVELS[s] > floor)
        return now - last >= ALERT_COOLDOWN.get(kind, 0)

    # --- Topology ----------------------------------------------------------

    def _load_topology(self):
        """(Re)read logs/topology.json if it changed since the last read.

        Called when a switch connects, not at startup: Ryu is normally
        started before Mininet, and topology.py rewrites the file just before
        its switches connect, so only then is the file known to be current.
        The mtime check means 7 switches connecting cause only one read.
        """
        try:
            mtime = os.path.getmtime(TOPOLOGY_JSON)
            if mtime == self.topology_mtime:
                return
            with open(TOPOLOGY_JSON) as f:
                topo = json.load(f)
            ports = topo["ports"]
            capacity = {(p["dpid"], p["port"]): p["bw_bps"] for p in ports}
            port_peer = {(p["dpid"], p["port"]): p["peer"] for p in ports}
            switch_names = {dpid: name
                            for name, dpid in topo["switches"].items()}
            host_port = {h: (a["dpid"], a["port"])
                         for h, a in topo["hosts"].items()}
            monitor_ip = topo["monitor"]["ip"]
            monitor_port = ports_towards_monitor(topo)
        except (OSError, ValueError, KeyError, TypeError) as e:
            self.logger.error("topology: cannot read %s (%s). Utilization "
                              "and mitigation are unavailable until it can "
                              "be read.", TOPOLOGY_JSON, e)
            return

        self.topology_mtime = mtime
        self.capacity, self.port_peer = capacity, port_peer
        self.switch_names, self.host_port = switch_names, host_port
        self.monitor_ip, self.monitor_port = monitor_ip, monitor_port
        self.unknown_ports.clear()
        self.logger.info("topology: loaded %d switches, %d hosts, %d ports "
                         "from %s", len(switch_names), len(host_port),
                         len(capacity), TOPOLOGY_JSON)

    def _port_name(self, dpid, port_no):
        # Same name as the Mininet interface: port 2 of s1 is s1-eth2.
        return "%s-eth%d" % (self.switch_names.get(dpid, "dpid%d" % dpid),
                             port_no)

    # --- Port statistics ---------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPStateChange,
                [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):
        """Keep self.datapaths in step with the switches connected now."""
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            # MAIN_DISPATCHER = the OpenFlow handshake is done.
            self._load_topology()
            self.datapaths[dp.id] = dp
            self.logger.info("switch: %s connected (dpid %d)",
                             self.switch_names.get(dp.id, "?"), dp.id)
        elif ev.state == DEAD_DISPATCHER and self.datapaths.get(dp.id) is dp:
            # (The "is dp" check skips a late disconnect of an old connection
            # when the same switch has already reconnected.)
            del self.datapaths[dp.id]
            # Forget its counters: after a Mininet restart its ports are new
            # and count from 0 again.
            for key in [k for k in self.port_stats if k[0] == dp.id]:
                del self.port_stats[key]
            # And its mitigations: a restarted switch has lost the rules, and
            # if it still has them, installing them again just replaces them.
            for host in [h for h, m in self.mitigations.items()
                         if m["dpid"] == dp.id]:
                del self.mitigations[host]
            self.logger.info("switch: %s disconnected (dpid %d)",
                             self.switch_names.get(dp.id, "?"), dp.id)

    def _stats_poller(self):
        while True:
            # list(): send_msg() can yield, and a switch may connect or
            # disconnect meanwhile, changing the dict.
            for dp in list(self.datapaths.values()):
                # OFPP_ANY = one request for the counters of every port.
                req = dp.ofproto_parser.OFPPortStatsRequest(
                    dp, 0, dp.ofproto.OFPP_ANY)
                dp.send_msg(req)
            hub.sleep(STATS_INTERVAL)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        dp = ev.msg.datapath
        for stat in ev.msg.body:
            # Above OFPP_MAX are reserved numbers; OVS includes LOCAL (the
            # bridge's own internal interface), which is not a link.
            if stat.port_no > dp.ofproto.OFPP_MAX:
                continue
            self._update_port(dp.id, stat)

    def _update_port(self, dpid, stat):
        key = (dpid, stat.port_no)
        # The port's uptime as the switch measured it when it read these
        # counters. Using it for Δt is more accurate than our own clock: the
        # reply may wait in queues for a while before this handler runs.
        now = stat.duration_sec + stat.duration_nsec / 1e9
        prev = self.port_stats.get(key)
        cur = self.port_stats[key] = {
            "time": now, "rx_bytes": stat.rx_bytes, "tx_bytes": stat.tx_bytes,
            "rx_bps": None, "tx_bps": None, "rx_util": None, "tx_util": None}
        if prev is None:
            return                  # first sample: nothing to compare with

        dt = now - prev["time"]
        d_rx = stat.rx_bytes - prev["rx_bytes"]
        d_tx = stat.tx_bytes - prev["tx_bytes"]
        if dt <= 0 or d_rx < 0 or d_tx < 0:
            # The port was recreated and its counters started over. This
            # sample becomes the new baseline.
            return
        cur["rx_bps"] = d_rx * 8 / dt
        cur["tx_bps"] = d_tx * 8 / dt

        capacity = self.capacity.get(key)
        if not capacity:
            if key not in self.unknown_ports:
                self.unknown_ports.add(key)
                self.logger.warning("util: no capacity for %s in %s, "
                                    "skipping it",
                                    self._port_name(*key), TOPOLOGY_JSON)
            return
        cur["rx_util"] = cur["rx_bps"] / capacity * 100
        cur["tx_util"] = cur["tx_bps"] / capacity * 100

        if max(cur["rx_util"], cur["tx_util"]) >= LOG_MIN_UTIL:
            self.logger.info("util: %-8s -> %-7s %3.0f Mbit/s link | "
                             "rx %5.1f%% %6.2f Mbit/s | tx %5.1f%% %6.2f "
                             "Mbit/s", self._port_name(*key),
                             self.port_peer.get(key, "?"), capacity / 1e6,
                             cur["rx_util"], cur["rx_bps"] / 1e6,
                             cur["tx_util"], cur["tx_bps"] / 1e6)
        self._check_util(key, cur, capacity)
        peer = self.port_peer.get(key)
        if peer in self.host_port:
            self._check_unexplained(peer, key, cur, capacity)

    def _check_util(self, key, cur, capacity):
        """Raise or resolve the utilization alerts of the link at this port.

        Alerts are per link direction, named "s1->s3". A link between two
        switches is measured at both ends (s1-eth2 tx and s3-eth3 rx count
        the same s1 -> s3 traffic), so each direction is judged only where
        it is sent, by tx; otherwise every busy link would be alerted twice.
        Hosts and the monitor report no port stats, so on their links the
        switch's rx is used too, for the direction towards the switch.
        """
        switch = self.switch_names.get(key[0], "dpid%d" % key[0])
        peer = self.port_peer.get(key, "?")
        directions = [("tx", switch, peer)]
        if peer not in self.switch_names.values():
            directions.append(("rx", peer, switch))

        for d, src, dst in directions:
            link = "%s->%s" % (src, dst)
            util = cur[d + "_util"]
            text = "%s %s %.1f%% of %.0f Mbit/s (%.2f Mbit/s)" % (
                self._port_name(*key), d, util, capacity / 1e6,
                cur[d + "_bps"] / 1e6)
            severity = util_severity(util)
            if severity:
                self._raise_alert("utilization", link, severity, text)
            else:
                self._resolve_alert(
                    "utilization", link, "%s, under %.0f%% for %d s" % (
                        text, UTIL_THRESHOLDS[-1][1],
                        ALERT_COOLDOWN["utilization"]))

    # --- Correlation + mitigation ------------------------------------------

    def _check_unexplained(self, host, key, cur, capacity):
        """Correlate the host's port stats with its telemetry.

        cur["rx_bps"] is everything the switch received from the host;
        app_tx_bps is what the host's agent says it sent itself. A busy port
        that the agent can't account for raises the "unexplained" alert and
        gets the mitigation rules. The switch keeps counting the host's
        packets even when a rule drops them, so the alert stays up as long
        as the traffic keeps coming, mitigated or not.
        """
        node = self.nodes.get(host)
        if node is None or node["app_tx_bps"] is None:
            return          # no rate from the agent yet: nothing to compare
        app_bps, rx_bps = node["app_tx_bps"], cur["rx_bps"]
        # % of the traffic from the host that its agent explains. An idle
        # port has nothing to explain.
        share = min(100.0, app_bps / rx_bps * 100) if rx_bps else 100.0
        text = ("%s rx %.2f Mbit/s (%.1f%% of %.0f Mbit/s), agent sent "
                "%.1f kbit/s (%.2f%%)" % (
                    self._port_name(*key), rx_bps / 1e6, cur["rx_util"],
                    capacity / 1e6, app_bps / 1e3, share))

        if (cur["rx_util"] >= UNEXPLAINED_MIN_UTIL
                and share < UNEXPLAINED_MAX_SHARE):
            self._raise_alert("unexplained", host, "CRITICAL",
                              "traffic not explained by the application: "
                              + text)
            self._mitigate(host, key)
            return

        self._resolve_alert("unexplained", host, text)
        active = self.mitigations.get(host)
        if active and time.monotonic() >= active["expires"]:
            # Nothing to send: the switch has already deleted the rules.
            del self.mitigations[host]
            self.logger.warning("MITIGATION %s: rules on %s expired "
                                "(hard_timeout %d s), its traffic is "
                                "forwarded normally again", host,
                                self._port_name(*key), MITIGATION_TIMEOUT)

    def _mitigate(self, host, key):
        """Put the mitigation rules on the host's port, or renew them."""
        now = time.monotonic()
        active = self.mitigations.get(host)
        if active and now < active["expires"] - MITIGATION_RENEW:
            return                  # in place, and not about to expire

        dpid, port_no = key
        dp = self.datapaths.get(dpid)
        out_port = self.monitor_port.get(dpid)
        if dp is None or out_port is None:
            self.logger.error("MITIGATION %s: cannot install on %s (switch "
                              "not connected or no path to the monitor)",
                              host, self._port_name(*key))
            return
        self._install_mitigation(dp, port_no, out_port)
        self.mitigations[host] = {"dpid": dpid, "port": port_no,
                                  "expires": now + MITIGATION_TIMEOUT}

        if active:
            self.logger.warning("MITIGATION %s: renewed on %s for %d s, "
                                "traffic still not explained", host,
                                self._port_name(*key), MITIGATION_TIMEOUT)
        else:
            self.logger.warning(
                "MITIGATION %s: installed on %s for %d s: drop everything "
                "from %s except telemetry (udp %s:%d) and arp for %s, sent "
                "out %s", host, self._port_name(*key), MITIGATION_TIMEOUT,
                host, self.monitor_ip, TELEMETRY_PORT, self.monitor_ip,
                self._port_name(dpid, out_port))

    def _install_mitigation(self, dp, in_port, out_port):
        """Send the three flow rules for in_port, each with hard_timeout.

        Installing a rule whose match and priority are already in the table
        replaces it (OpenFlow 1.3), with a fresh timeout: that is how
        renewing works.
        """
        ofp, parser = dp.ofproto, dp.ofproto_parser
        # The allow rules send packets straight out of the port towards the
        # monitor: there is one flow table, and the first matching rule ends
        # the lookup, so they can't hand packets on to the learned flows.
        to_monitor = [parser.OFPInstructionActions(
            ofp.OFPIT_APPLY_ACTIONS, [parser.OFPActionOutput(out_port)])]
        rules = [
            # Telemetry. OpenFlow only accepts a field if the layer below is
            # matched too: udp_dst needs ip_proto=UDP, which needs IPv4.
            (ALLOW_PRIORITY, parser.OFPMatch(
                in_port=in_port, eth_type=ether_types.ETH_TYPE_IP,
                ip_proto=in_proto.IPPROTO_UDP, ipv4_dst=self.monitor_ip,
                udp_dst=TELEMETRY_PORT), to_monitor),
            # ARP for the monitor. Nothing confirms a one-way UDP stream,
            # so Linux re-checks the monitor's MAC with an ARP request every
            # 15-45 s; if that were dropped, telemetry would stop too.
            (ALLOW_PRIORITY, parser.OFPMatch(
                in_port=in_port, eth_type=ether_types.ETH_TYPE_ARP,
                arp_tpa=self.monitor_ip), to_monitor),
            # Everything else from the port. No instructions = drop.
            (DROP_PRIORITY, parser.OFPMatch(in_port=in_port), []),
        ]
        for priority, match, inst in rules:
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp, priority=priority, match=match,
                instructions=inst, hard_timeout=MITIGATION_TIMEOUT))
