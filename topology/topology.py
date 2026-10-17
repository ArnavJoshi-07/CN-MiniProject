#!/usr/bin/env python3
r"""
Custom Mininet topology for the network monitoring project.

3-tier tree (no loops, so the learning switch can't cause broadcast storms):

                        s1 (core) ── monitor (10.0.0.100, root namespace)
                      /    ║
                 50 Mbit   ║ 10 Mbit (bottleneck)
                    /      ║
              s2 (agg)    s3 (agg)
              /    \       /    \
            s4     s5    s6     s7          (edge, 50 Mbit uplinks)
          h1-h3  h4-h6 h7-h9  h10-h12       (100 Mbit host links)

Port numbers (Mininet assigns them in the order links are added):
    s1: 1=s2  2=s3  3=monitor
    s2: 1=s4  2=s5  3=s1
    s3: 1=s6  2=s7  3=s1
    s4: 1=h1   2=h2   3=h3   4=s2
    s5: 1=h4   2=h5   3=h6   4=s2
    s6: 1=h7   2=h8   3=h9   4=s3
    s7: 1=h10  2=h11  3=h12  4=s3
The controller doesn't rely on this table: on startup the script writes the
real port numbers and capacities to logs/topology.json.

Run (with Ryu already running in another terminal):
    sudo python3 topology/topology.py

Verify the monitor link, in a normal terminal:
    nc -u -l 9999
and at the mininet> prompt:
    h1 echo hi | nc -u -w1 10.0.0.100 9999
"""

import json
import os

from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import RemoteController, OVSKernelSwitch, Node, Switch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info


# --- Tree shape -----------------------------------------------------------
# Change these to grow the tree; names, IPs and ports follow automatically.
NUM_AGG = 2                # aggregation switches under the core
EDGES_PER_AGG = 2          # edge switches under each aggregation switch
HOSTS_PER_EDGE = 3         # hosts on each edge switch

# --- Link capacities (Mbit/s) ---------------------------------------------
# The controller needs these exact numbers to compute utilization; they are
# exported to logs/topology.json so it never has to hardcode them.
HOST_LINK_BW = 100         # host <-> edge (and the monitor link)
EDGE_LINK_BW = 50          # edge <-> aggregation
CORE_LINK_BW = 50          # aggregation <-> core
BOTTLENECK_BW = 10         # one aggregation <-> core link
BOTTLENECK_AGG = "s3"      # whose core uplink is the bottleneck

# Controller location (Ryu)
CONTROLLER_IP = "127.0.0.1"
CONTROLLER_PORT = 6653

# Monitor node + telemetry (fixed conventions from CLAUDE.md)
MONITOR_ADDR = "10.0.0.100"
MONITOR_IP = MONITOR_ADDR + "/8"
TELEMETRY_PORT = 9999
AGENT_INTERVAL = 2         # seconds

# Absolute paths, so the script works no matter which folder you run it from.
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_PATH = os.path.join(PROJECT_DIR, "agent", "agent.py")
LOG_DIR = os.path.join(PROJECT_DIR, "logs")
TOPOLOGY_JSON = os.path.join(LOG_DIR, "topology.json")


class MonitoringTopo(Topo):
    """Core s1, then aggregation switches, then edge switches with hosts."""

    def build(self):
        # Switch names like "s1" give automatic datapath IDs (s1 -> dpid 1),
        # which is what Ryu shows in its logs and stats.
        core = self.addSwitch("s1", protocols="OpenFlow13")
        aggs = [self.addSwitch(f"s{2 + i}", protocols="OpenFlow13")
                for i in range(NUM_AGG)]

        # Edge switches are numbered after the aggregation switches, and
        # hosts h1, h2, ... at 10.0.0.1, 10.0.0.2, ... left to right.
        next_switch = 2 + NUM_AGG
        next_host = 1
        for agg in aggs:
            for _ in range(EDGES_PER_AGG):
                edge = self.addSwitch(f"s{next_switch}", protocols="OpenFlow13")
                next_switch += 1
                # Hosts first so they get ports 1..HOSTS_PER_EDGE on the edge
                # switch; the uplink comes last.
                for _ in range(HOSTS_PER_EDGE):
                    host = self.addHost(f"h{next_host}",
                                        ip=f"10.0.0.{next_host}/8")
                    self.addLink(host, edge, bw=HOST_LINK_BW)
                    next_host += 1
                self.addLink(edge, agg, bw=EDGE_LINK_BW)

        # Uplinks to the core last, so each aggregation switch has its
        # downlinks on the low ports and the uplink on the highest one.
        for agg in aggs:
            bw = BOTTLENECK_BW if agg == BOTTLENECK_AGG else CORE_LINK_BW
            self.addLink(agg, core, bw=bw)


def add_monitor_node(net):
    """Connect the root namespace (where Ryu runs) to s1 as 10.0.0.100.

    Same pattern as Mininet's examples/sshd.py (connectToRootNS). Must be
    called before net.start() so s1 picks up the new port when it starts.
    """
    root = Node("monitor", inNamespace=False)
    intf = net.addLink(root, net["s1"], bw=HOST_LINK_BW).intf1
    root.setIP(MONITOR_IP, intf=intf)
    return root


def export_topology(net, path):
    """Write switch ports, capacities and host locations to a JSON file.

    Read from the built network (not the docstring table), so the numbers are
    exactly what the switches will report. Called before net.start(): the
    switches only connect to Ryu after start, so the file is ready by then.
    """
    switches = {sw.name: int(sw.dpid, 16) for sw in net.switches}
    ports = []      # one entry per switch port: what it connects to + capacity
    hosts = {}      # host name -> where it attaches (for mitigation rules)
    monitor = None

    for link in net.links:
        bw_bps = int(link.intf1.params["bw"] * 1_000_000)
        # Look at the link from both ends; keep the ends that are switches.
        for intf, peer_intf in ((link.intf1, link.intf2),
                                (link.intf2, link.intf1)):
            sw, peer = intf.node, peer_intf.node
            if not isinstance(sw, Switch):
                continue
            port = sw.ports[intf]
            ports.append({"switch": sw.name, "dpid": switches[sw.name],
                          "port": port, "peer": peer.name, "bw_bps": bw_bps})
            where = {"switch": sw.name, "dpid": switches[sw.name], "port": port}
            if peer in net.hosts:
                hosts[peer.name] = {"ip": peer.IP(), "mac": peer.MAC(), **where}
            elif peer.name == "monitor":
                monitor = {"ip": MONITOR_ADDR, **where}

    ports.sort(key=lambda p: (p["dpid"], p["port"]))
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump({"switches": switches, "hosts": hosts, "monitor": monitor,
                   "ports": ports}, f, indent=2)
    info(f"*** Wrote {len(ports)} switch ports to {path}\n")


def start_agents(net):
    """Start one telemetry agent per host, in the background."""
    if not os.path.exists(AGENT_PATH):
        info(f"*** {AGENT_PATH} not found, skipping agents\n")
        return
    os.makedirs(LOG_DIR, exist_ok=True)
    for h in net.hosts:
        info(f"*** Starting agent on {h.name}\n")
        # The trailing & is essential: without it, h.cmd() blocks forever.
        h.cmd(f"python3 {AGENT_PATH} --node-id {h.name} "
              f"--monitor-ip {MONITOR_ADDR} --port {TELEMETRY_PORT} "
              f"--interval {AGENT_INTERVAL} "
              f"> {LOG_DIR}/agent-{h.name}.log 2>&1 &")


def stop_agents(net):
    """Kill the agents. Each host has its own bash, so %python3 is per-host."""
    for h in net.hosts:
        h.cmd("kill %python3")


def run():
    net = Mininet(
        topo=MonitoringTopo(),
        switch=OVSKernelSwitch,
        link=TCLink,           # enables bw/delay/loss on links
        controller=None,       # Ryu is added explicitly below
        autoSetMacs=True,      # h1 -> 00:00:00:00:00:01, readable flow tables
    )
    net.addController("c0", controller=RemoteController,
                      ip=CONTROLLER_IP, port=CONTROLLER_PORT)
    add_monitor_node(net)
    export_topology(net, TOPOLOGY_JSON)

    net.start()
    try:
        start_agents(net)
        CLI(net)
    finally:
        # Runs even if the CLI crashes, so no agents or links are left behind.
        stop_agents(net)
        net.stop()


if __name__ == "__main__":
    # "info" makes Mininet print what it's doing (adding hosts, links, ...)
    setLogLevel("info")
    run()
