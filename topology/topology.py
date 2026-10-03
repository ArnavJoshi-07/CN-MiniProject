#!/usr/bin/env python3
"""
Custom Mininet topology for the network monitoring project.

    h1 ──┐                  ┌── h4
    h2 ──┼── s1 ══ s2 ══ s3 ┼── h5
    h3 ──┘  (bottleneck)    └── h6
          │
       monitor (10.0.0.100, root namespace — where Ryu receives telemetry)

Port numbers (Mininet assigns them in the order links are added):
    s1: 1=h1  2=h2  3=h3  4=s2  5=monitor
    s2: 1=s1  2=s3
    s3: 1=h4  2=h5  3=h6  4=s2

Run (with Ryu already running in another terminal):
    sudo python3 topology/topology.py

Verify the monitor link, in a normal terminal:
    nc -u -l 9999
and at the mininet> prompt:
    h1 echo hi | nc -u -w1 10.0.0.100 9999
"""

import os

from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import RemoteController, OVSKernelSwitch, Node
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info


# --- Link capacities (Mbit/s) ---------------------------------------------
# The controller needs these exact numbers to compute utilization,
# so keep them as named constants instead of magic numbers in addLink().
HOST_LINK_BW = 100         # host <-> switch links (and the monitor link)
TRUNK_LINK_BW = 50         # s2 <-> s3
BOTTLENECK_BW = 10         # s1 <-> s2

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


class MonitoringTopo(Topo):
    """3 switches in a line, 3 hosts on each edge switch."""

    def build(self):
        # Switch names like "s1" give automatic datapath IDs (s1 -> dpid 1),
        # which is what Ryu shows in its logs and stats.
        s1 = self.addSwitch("s1", protocols="OpenFlow13")
        s2 = self.addSwitch("s2", protocols="OpenFlow13")
        s3 = self.addSwitch("s3", protocols="OpenFlow13")

        # Hosts h1..h6 at 10.0.0.1..6
        hosts = [self.addHost(f"h{i}", ip=f"10.0.0.{i}/8") for i in range(1, 7)]

        # h1-h3 on s1, h4-h6 on s3. Order matters: it decides port numbers.
        for h in hosts[:3]:
            self.addLink(h, s1, bw=HOST_LINK_BW)
        for h in hosts[3:]:
            self.addLink(h, s3, bw=HOST_LINK_BW)

        # Switch-to-switch links; s1 <-> s2 is the deliberate bottleneck.
        self.addLink(s1, s2, bw=BOTTLENECK_BW)
        self.addLink(s2, s3, bw=TRUNK_LINK_BW)


def add_monitor_node(net):
    """Connect the root namespace (where Ryu runs) to s1 as 10.0.0.100.

    Same pattern as Mininet's examples/sshd.py (connectToRootNS). Must be
    called before net.start() so s1 picks up the new port when it starts.
    """
    root = Node("monitor", inNamespace=False)
    intf = net.addLink(root, net["s1"], bw=HOST_LINK_BW).intf1
    root.setIP(MONITOR_IP, intf=intf)
    return root


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
