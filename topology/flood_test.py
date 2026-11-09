#!/usr/bin/env python3
"""
Automated D1 test 5: flood -> correlation alert -> mitigation rules.

Builds the same network as topology.py, starts the agents, then without any
typing at the mininet> prompt:
  1. Diagnostic: h1 floods h11 and h4 for 5 s each and prints h1's send rate.
     In this setup a host can only send as fast as the narrowest link on its
     path (10 Mbit/s towards h11, 50 Mbit/s towards h4), so a flood across
     the bottleneck never reaches the 60 % trigger.
  2. Test 5: h1 floods h3 (same edge switch, 100 Mbit/s path). Checks the 3
     mitigation rules on s4, that h2's 30 Mbit/s UDP to h3 is loss-free while
     h1 is blocked, that the rules expire, and that h1 can ping h3 afterwards.
Ends with "RESULT: PASS" or "RESULT: CHECK" and writes mininet.log, the agent
logs and topology.json to results/d1-flood/.

Run (Ryu already running, its output saved to results/d1-flood/controller.log):
    sudo python3 topology/flood_test.py
Don't run `sudo mn -c` while Ryu is up: it kills ryu-manager too.
"""

import os
import re
import shutil
import time

from mininet.net import Mininet
from mininet.node import RemoteController, OVSKernelSwitch
from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.util import quietRun

import topology as topo

OUT_DIR = os.path.join(topo.PROJECT_DIR, "results", "d1-flood")
LOG = os.path.join(OUT_DIR, "mininet.log")

H3 = "10.0.0.3"
# h2's test traffic: 30 Mbit/s UDP stays under the 60 % trigger, so it is
# never itself taken for unexplained traffic (a full-speed TCP iperf would be).
H2_CHECK = f"iperf -u -c {H3} -b 30M -l 1200 -t 10"


def log(text=""):
    """Print with a timestamp and append to the run's mininet.log."""
    line = f"{time.strftime('%H:%M:%S')} {text}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def run(host, cmd):
    """Run a command on a host and log it like the mininet> prompt would."""
    log(f"{host.name}> {cmd}")
    out = host.cmd(cmd)
    for line in out.rstrip().splitlines():
        log(f"    {line}")
    return out


def flood(dst):
    return f"hping3 --flood --udp -p 5001 -d 1200 {dst} >/dev/null 2>&1 &"


def mitigation_rules():
    """The controller's mitigation rules on s4 (priorities 200 and 100)."""
    out = quietRun("ovs-ofctl -O OpenFlow13 dump-flows s4")
    return [line.strip() for line in out.splitlines()
            if re.search(r"priority=(100|200)\b", line)]


def show_rules():
    log("sh> ovs-ofctl -O OpenFlow13 dump-flows s4 | grep priority=(100|200)")
    rules = mitigation_rules()
    for rule in rules:
        log(f"    {rule}")
    if not rules:
        log("    (no mitigation rules)")
    return rules


def wait_for_rules(timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if mitigation_rules():
            return True
        time.sleep(1)
    return False


def tx_rate_mbps(host, seconds=2.0):
    """What the host actually puts on its link, from its interface counter."""
    path = f"/sys/class/net/{host.name}-eth0/statistics/tx_bytes"
    before = int(host.cmd(f"cat {path}"))
    time.sleep(seconds)
    after = int(host.cmd(f"cat {path}"))
    return (after - before) * 8 / seconds / 1e6


def udp_loss(out):
    """Loss % from the server report of an `iperf -u` client run."""
    m = re.findall(r"\(([\d.]+)%\)", out)
    return float(m[-1]) if m else None


def rate_check(net):
    """How fast can h1 flood, depending on the narrowest link on the path?"""
    h1 = net["h1"]
    log("=== Diagnostic: h1 flood rate per destination (5 s each) ===")
    for name, dst, path in (("h11", "10.0.0.11", "10 Mbit s1-s3 on path"),
                            ("h4", "10.0.0.4", "50 Mbit s4-s2 on path")):
        run(h1, flood(dst))
        time.sleep(2)
        log(f"    h1 -> {name} ({path}): h1-eth0 tx "
            f"{tx_rate_mbps(h1, 3):.1f} Mbit/s")
        run(h1, "pkill hping3")
        log("    waiting 15 s for alerts to resolve")
        time.sleep(15)


def flood_test(net):
    """Test 5, flooding a host on the same edge switch (s4) as h1."""
    h1, h2, h3 = net["h1"], net["h2"], net["h3"]
    results = {}

    log("=== T5 flood: h1 -> h3 (same edge switch s4) ===")
    run(h3, "iperf -s -u >/dev/null 2>&1 &")
    results["baseline_loss_pct"] = udp_loss(run(h2, H2_CHECK))

    run(h1, flood(H3))
    time.sleep(1)
    results["h1_rate"] = round(tx_rate_mbps(h1), 1)
    log(f"    h1 -> h3: h1-eth0 tx {results['h1_rate']} Mbit/s "
        f"(trigger is 60 Mbit/s)")
    if not wait_for_rules():
        log("    no mitigation rules after 15 s")
        run(h1, "pkill hping3")
        log(f"RESULT: FAIL {results}")
        return results

    time.sleep(2)
    results["rules"] = len(show_rules())
    # h1 is still flooding here; its traffic is dropped at s4.
    results["during_mitigation_loss_pct"] = udp_loss(run(h2, H2_CHECK))
    show_rules()

    run(h1, "pkill hping3")
    log("    waiting 45 s for RESOLVED + rule expiry (hard_timeout 30)")
    time.sleep(45)
    results["rules_after"] = len(show_rules())
    ping = run(h1, f"ping -c3 {H3}")
    results["ping_ok"] = " 0% packet loss" in ping
    run(h3, "kill %iperf")

    loss = results["during_mitigation_loss_pct"]
    ok = (results["rules"] == 3 and results["rules_after"] == 0
          and results["ping_ok"] and loss is not None and loss < 1)
    log(f"RESULT: {'PASS' if ok else 'CHECK'} {results}")
    return results


def save_outputs():
    """Copy agent logs + topology.json next to mininet.log, owned by the user."""
    for name in os.listdir(topo.LOG_DIR):
        if name.startswith("agent-") or name == "topology.json":
            shutil.copy(os.path.join(topo.LOG_DIR, name), OUT_DIR)
    uid = int(os.environ.get("SUDO_UID", 0))
    gid = int(os.environ.get("SUDO_GID", 0))
    for name in os.listdir(OUT_DIR):
        os.chown(os.path.join(OUT_DIR, name), uid, gid)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    open(LOG, "w").close()
    net = Mininet(topo=topo.MonitoringTopo(), switch=OVSKernelSwitch,
                  link=TCLink, controller=None, autoSetMacs=True)
    net.addController("c0", controller=RemoteController,
                      ip=topo.CONTROLLER_IP, port=topo.CONTROLLER_PORT)
    topo.add_monitor_node(net)
    topo.export_topology(net, topo.TOPOLOGY_JSON)

    net.start()
    try:
        topo.start_agents(net)
        log("waiting 5 s for switches, then pingall")
        time.sleep(5)
        log(f"pingall: {net.pingAll()}% dropped")
        # The correlation rule needs two telemetry messages per host.
        log("waiting 20 s so every agent has sent several messages")
        time.sleep(20)
        rate_check(net)
        flood_test(net)
    finally:
        quietRun("pkill hping3")
        topo.stop_agents(net)
        net.stop()
        save_outputs()


if __name__ == "__main__":
    setLogLevel("info")
    main()
