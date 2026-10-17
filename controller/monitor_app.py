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


# 0.0.0.0 = every interface of the root namespace, including the monitor
# link's 10.0.0.100 (topology/topology.py).
TELEMETRY_ADDR = ("0.0.0.0", 9999)
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

# Alert severity -> logging level of its log line.
ALERT_LEVELS = {"WARNING": logging.WARNING, "CRITICAL": logging.CRITICAL}


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
            "received": 1, "lost": 0, "telemetry": msg}


def loss_pct(state):
    total = state["received"] + state["lost"]
    return 100.0 * state["lost"] / total if total else 0.0


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
        self.host_port = {}      # "h1" -> (dpid, port) it attaches to (task 7)

        self.datapaths = {}      # dpid -> Datapath, switches connected now
        self.port_stats = {}     # (dpid, port) -> state dict (see docstring)
        self.unknown_ports = set()   # ports with no capacity, already warned

        # (kind, subject) -> {"severity": "CRITICAL", "since": monotonic time}
        # for every alert active now, e.g. ("unreachable", "h5"). Kept apart
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
        """Log an alert once. Repeats are ignored until it is resolved.

        kind + subject name the problem ("unreachable", "h5"). Raising it
        again with a different severity is logged as a change, so a WARNING
        can later turn into a CRITICAL (task 6).
        """
        key = (kind, subject)
        active = self.alerts.get(key)
        if active is not None and active["severity"] == severity:
            return
        if active is None:
            self.alerts[key] = {"severity": severity,
                                "since": time.monotonic()}
        else:
            active["severity"] = severity
        self.logger.log(ALERT_LEVELS[severity], "ALERT %-8s %s %s: %s",
                        severity, kind, subject, text)

    def _resolve_alert(self, kind, subject, text):
        """Log that an alert is over. Does nothing if it isn't active."""
        active = self.alerts.pop((kind, subject), None)
        if active is None:
            return
        self.logger.info("ALERT RESOLVED %s %s: %s (alert lasted %.0f s)",
                         kind, subject, text,
                         time.monotonic() - active["since"])

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
        except (OSError, ValueError, KeyError, TypeError) as e:
            self.logger.error("topology: cannot read %s (%s). Utilization "
                              "is unavailable until it can be read.",
                              TOPOLOGY_JSON, e)
            return

        self.topology_mtime = mtime
        self.capacity, self.port_peer = capacity, port_peer
        self.switch_names, self.host_port = switch_names, host_port
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
