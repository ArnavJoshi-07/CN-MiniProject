#!/usr/bin/env python3
"""
Guided demo of the whole monitoring system, one scenario at a time.

Normally started by ./demo.sh, which first starts the Ryu controller and the
dashboard. Run by hand (Ryu already running in another terminal):
    sudo python3 topology/demo.py            # pauses before every step
    sudo python3 topology/demo.py --auto     # no pauses (rehearsal)

It builds the same network as topology.py, starts the agents, and then for
each scenario:
    1. explains what it is about to do and what should happen,
    2. waits for Enter (so you can talk; skipped with --auto),
    3. causes the problem (iperf, hping3, a busy loop, packet loss, killing
       an agent, pulling a link),
    4. prints the controller's alerts as they arrive, read from the same
       SQLite database the dashboard shows (logs/monitor.db),
    5. checks that the expected alert really came: PASS or CHECK.

Scenarios
    0  Start-up       pingall, all 12 agents reporting
    1  Baseline       normal telemetry only -> no alerts
    2  Congestion     iperf h1 -> h10 over the 10 Mbit/s s1-s3 link
                      -> CRITICAL utilization s1->s3, then RESOLVED
    3  Flood          hping3 flood h1 -> h3 -> CRITICAL unexplained h1,
                      drop rules on s4, h2 -> h3 unaffected
    4  CPU stress     busy loop on h5 -> CRITICAL cpu h5 "host problem"
    5  Lossy link     ~33 % packet loss on h7's link -> loss h7 alert
    6  Agent failure  kill h8's agent while h8 keeps pinging
                      -> unreachable h8, cause: agent failure
    7  Link failure   s3-s7 link down -> link s3-s7 alert, h10-h12
                      unreachable, cause: that link
At the end: a PASS/CHECK summary, an optional Mininet CLI for questions,
and everything saved to the results folder.
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time

from mininet.net import Mininet
from mininet.node import RemoteController, OVSKernelSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel
from mininet.util import quietRun

import topology as topo

DB_PATH = os.path.join(topo.PROJECT_DIR, "logs", "monitor.db")
STAGE_FILE = os.path.join(topo.LOG_DIR, "demo_stage.json")

# Terminal colours (ANSI escape codes).
BOLD, DIM, RED, GREEN, YELLOW, BLUE, MAGENTA, RESET = (
    "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[34m",
    "\033[35m", "\033[0m")
ANSI = re.compile(r"\033\[[0-9;]*m")

LOG_FILE = None             # set in main(): <out>/demo.log
AUTO = False                # --auto: no pauses


# --- Output -----------------------------------------------------------------

def say(text=""):
    """Print, and append to demo.log without the colour codes."""
    print(text, flush=True)
    if LOG_FILE:
        with open(LOG_FILE, "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {ANSI.sub('', text)}\n")


def heading(number, title):
    say()
    say(f"{BOLD}{BLUE}{'=' * 72}{RESET}")
    say(f"{BOLD}{BLUE} {number}. {title}{RESET}")
    say(f"{BOLD}{BLUE}{'=' * 72}{RESET}")


def explain(*lines):
    for line in lines:
        say(f"  {line}")


def set_stage(title, detail):
    """Tell the dashboard which step is running (shown as a banner)."""
    try:
        with open(STAGE_FILE, "w") as f:
            json.dump({"title": title, "detail": detail, "ts": time.time()},
                      f)
        os.chmod(STAGE_FILE, 0o644)
    except OSError:
        pass


def wait_enter(prompt="Press Enter to start"):
    if AUTO:
        return
    try:
        input(f"\n  {DIM}[{prompt}]{RESET} ")
    except EOFError:
        pass


def run(host, cmd, show=True):
    """Run a command on a Mininet host, echoing it like the CLI would."""
    if show:
        say(f"  {DIM}{host.name}>{RESET} {cmd}")
    return host.cmd(cmd)


# --- Alerts from the controller's database ----------------------------------

class Events:
    """Follows the controller's event table (logs/monitor.db) and prints
    every new alert / mitigation as it arrives."""

    COLOURS = {"CRITICAL": RED, "WARNING": YELLOW, "ACTION": MAGENTA}

    def __init__(self, path):
        self.path = path
        self.history = []           # every event seen during the demo
        self.mark_index = 0         # where the current scenario starts
        self.last_id = self._query("SELECT COALESCE(MAX(id), 0) FROM events")[0][0]

    def _query(self, sql, params=()):
        # Read-only, and a fresh connection each time: always sees the
        # controller's latest commit, never locks it out.
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True,
                               timeout=2)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def poll(self):
        """Print and return the events written since the last poll."""
        try:
            rows = self._query("SELECT id, type, kind, subject, severity, "
                               "text FROM events WHERE id > ? ORDER BY id",
                               (self.last_id,))
        except sqlite3.Error:
            return []           # controller busy recreating the file
        new = []
        for id_, type_, kind, subject, severity, text in rows:
            self.last_id = id_
            event = {"type": type_, "kind": kind, "subject": subject,
                     "severity": severity, "text": text}
            self.history.append(event)
            new.append(event)
            if type_ == "RESOLVED":
                colour, label = GREEN, "RESOLVED"
            elif type_ == "MITIGATION":
                colour, label = MAGENTA, "MITIGATION"
            else:
                colour, label = self.COLOURS.get(severity, ""), severity
            say(f"    {colour}{BOLD}{label:<10}{RESET} {colour}{kind} "
                f"{subject}{RESET}: {text}")
        return new

    def sleep(self, seconds):
        """Wait, printing events as they come."""
        end = time.time() + seconds
        while time.time() < end:
            self.poll()
            time.sleep(min(0.5, max(0, end - time.time())))
        self.poll()

    def wait_for(self, timeout, **match):
        """Wait until an event matching all of `match` arrives (or has
        arrived since this scenario started, see mark()). Returns it, or
        None after `timeout` seconds.

        match values: exact strings, except `text`, which is a substring;
        `severity` may be a tuple of accepted values."""
        end = time.time() + timeout
        while True:
            for event in self.history[self.mark_index:]:
                if matches(event, match):
                    return event
            if time.time() >= end:
                return None
            self.poll()
            time.sleep(0.5)

    def mark(self):
        """Start a scenario: wait_for() only looks at events from here on."""
        self.poll()
        self.mark_index = len(self.history)

    def seen(self, since_index=0, **match):
        return any(matches(e, match) for e in self.history[since_index:])

    def nodes(self):
        return self._query("SELECT node_id, status FROM nodes")


def matches(event, match):
    for key, want in match.items():
        have = event.get(key)
        if key == "text":
            if want not in (have or ""):
                return False
        elif isinstance(want, tuple):
            if have not in want:
                return False
        elif have != want:
            return False
    return True


# --- Scenario helpers ----------------------------------------------------------

RESULTS = []                # (scenario, passed, detail)


def check(scenario, passed, detail):
    RESULTS.append((scenario, passed, detail))
    mark = f"{GREEN}{BOLD}PASS{RESET}" if passed else f"{YELLOW}{BOLD}CHECK{RESET}"
    say(f"  -> {mark} {detail}")


def mitigation_rules():
    out = quietRun("ovs-ofctl -O OpenFlow13 dump-flows s4")
    rules = []
    for line in out.splitlines():
        if re.search(r"priority=(100|200)\b", line):
            # Drop the noisy fields so each rule fits on one line.
            line = re.sub(r"(cookie|duration|table|idle_age|hard_age)=[^,]*, ?",
                          "", line.strip())
            rules.append(line)
    return rules


def udp_loss(output):
    """Loss % from the server report of an `iperf -u` client run."""
    found = re.findall(r"\(([\d.]+)%\)", output)
    return float(found[-1]) if found else None


# --- Scenarios ------------------------------------------------------------------

def scenario_startup(net, ev):
    heading(0, "Start-up: network, agents, controller")
    explain("Mininet built a 3-tier tree: core s1, aggregation s2-s3, edge s4-s7,",
            "12 hosts. s3-s1 is the 10 Mbit/s bottleneck. Every host runs a UDP",
            "telemetry agent sending JSON to the monitor (10.0.0.100:9999) every 2 s.",
            "The Ryu controller forwards traffic (learning switch), polls port",
            "statistics every 2 s and writes everything to SQLite for the dashboard.")
    set_stage("0. Start-up", "pingall, then all 12 agents reporting")
    ev.mark()
    say()
    loss = net.pingAll(timeout=1)
    say(f"  pingall: {loss:.0f} % dropped")
    say("  waiting for all 12 agents to report ...")
    end = time.time() + 30
    ok = 0
    while time.time() < end:
        ok = sum(1 for _, status in ev.nodes() if status == "ok")
        if ok == len(net.hosts):
            break
        time.sleep(1)
    check("0 Start-up", loss == 0 and ok == len(net.hosts),
          f"pingall {loss:.0f} % dropped, {ok}/{len(net.hosts)} nodes reporting")


def scenario_baseline(net, ev):
    heading(1, "Baseline: normal operation")
    explain("Only telemetry is flowing. Every link is far below 70 %, every agent",
            "reports on time, CPU is low: the controller should raise nothing.")
    set_stage("1. Baseline", "Normal telemetry only: no alerts expected")
    wait_enter()
    ev.mark()
    start = len(ev.history)
    ev.sleep(10)
    raised = [e for e in ev.history[start:] if e["type"] in ("RAISED", "CHANGED")]
    check("1 Baseline", not raised,
          "no alerts in 10 s" if not raised else f"{len(raised)} alert(s) raised")


def scenario_congestion(net, ev, out):
    h1, h10 = net["h1"], net["h10"]
    heading(2, "Congestion on the bottleneck link")
    explain("h1 (under s2) sends TCP to h10 (under s3) as fast as it can. The",
            "path h1-s4-s2-s1-s3-s7-h10 crosses the 10 Mbit/s s1-s3 link, so that",
            "link fills up. Port statistics show s1-eth2 transmitting ~96 % of",
            "10 Mbit/s -> CRITICAL utilization s1->s3. The other links on the path",
            "(50 Mbit/s) only reach ~19 %. 10 s after the transfer ends: RESOLVED.")
    set_stage("2. Congestion", "iperf h1 -> h10 across the 10 Mbit/s s1-s3 link")
    wait_enter()
    ev.mark()
    run(h10, "iperf -s >/dev/null 2>&1 &")
    run(h1, f"iperf -c 10.0.0.10 -t 15 > {out}/iperf-congestion.txt 2>&1 &")
    started = time.time()
    raised = ev.wait_for(12, type="RAISED", kind="utilization", subject="s1->s3")
    ev.sleep(max(0, started + 16 - time.time()))   # until iperf is done
    # The client prints its bandwidth summary only when it exits, which can
    # be a second or two after -t 15 (it waits for the last data to be acked).
    summary = None
    for _ in range(10):
        summary = next((line.strip() for line in
                        open(f"{out}/iperf-congestion.txt")
                        if "bits/sec" in line), None)
        if summary:
            break
        ev.sleep(0.5)
    if summary:
        say(f"  {DIM}iperf: {summary}{RESET}")
    say("  transfer finished, waiting for RESOLVED (10 s cooldown) ...")
    resolved = ev.wait_for(25, type="RESOLVED", kind="utilization",
                           subject="s1->s3")
    run(h10, "kill %iperf", show=False)
    others = {e["subject"] for e in ev.history[ev.mark_index:]
              if e["kind"] == "utilization" and e["type"] == "RAISED"} - {"s1->s3"}
    check("2 Congestion", bool(raised and resolved),
          "CRITICAL utilization s1->s3 raised and resolved"
          + (f" (also: {', '.join(sorted(others))})" if others else
             ", no other link alerted")
          if raised else "no utilization alert on s1->s3")


def scenario_flood(net, ev):
    h1, h2, h3 = net["h1"], net["h2"], net["h3"]
    heading(3, "Unexplained traffic: flood from h1 -> mitigation")
    explain("h1 floods h3 with UDP (hping3, ~100 Mbit/s). The switch port s4-eth1",
            "receives ~100 % of h1's link, but h1's agent reports it sent only",
            "~1 kbit/s itself. Traffic not explained by the application ->",
            "CRITICAL unexplained h1, and the controller installs 3 OpenFlow rules",
            "on s4 for h1's port (hard_timeout 30 s):",
            "  prio 200: UDP to 10.0.0.100:9999 -> towards monitor (telemetry lives)",
            "  prio 200: ARP for 10.0.0.100     -> towards monitor",
            "  prio 100: everything else from h1 -> DROP",
            "h2 -> h3 traffic stays loss-free. s4-eth1 itself also crosses 90 %",
            "(the switch counts packets before the drop rule throws them away),",
            "so a CRITICAL utilization h1->s4 alert appears as well.")
    set_stage("3. Flood", "hping3 flood h1 -> h3: unexplained traffic -> drop rules on s4")
    wait_enter()
    ev.mark()
    run(h3, "iperf -s -u >/dev/null 2>&1 &")
    run(h1, "hping3 --flood --udp -p 5001 -d 1200 10.0.0.3 >/dev/null 2>&1 &")
    alert = ev.wait_for(15, type="RAISED", kind="unexplained", subject="h1")
    installed = ev.wait_for(5, type="MITIGATION", subject="h1", text="installed")
    ev.sleep(1)
    rules = mitigation_rules()
    say(f"  {DIM}ovs-ofctl dump-flows s4 (mitigation rules):{RESET}")
    for rule in rules:
        say(f"    {rule}")
    say("  h2 -> h3 while h1 is still flooding (30 Mbit/s UDP, 5 s):")
    loss = udp_loss(run(h2, "iperf -u -c 10.0.0.3 -b 30M -l 1200 -t 5"))
    say(f"  h2 -> h3 loss: {loss} %")
    run(h1, "pkill hping3")
    say("  flood stopped: alert resolves after the 10 s cooldown, the rules")
    say("  expire by themselves within 30 s (checked at the end of the demo).")
    ev.wait_for(20, type="RESOLVED", kind="unexplained", subject="h1")
    run(h3, "kill %iperf", show=False)
    check("3 Flood", bool(alert and installed and len(rules) == 3
                          and loss is not None and loss < 1),
          f"unexplained alert {'yes' if alert else 'NO'}, "
          f"{len(rules)} rules on s4, h2 -> h3 loss {loss} %")


def scenario_cpu(net, ev):
    h5 = net["h5"]
    heading(4, "CPU stress on h5 (host problem)")
    explain("A busy loop runs on h5 for 15 s. Its agent measures the CPU used by",
            "the processes inside h5 (~100 % of one core) -> CRITICAL cpu h5.",
            "The controller correlates it with h5's link: the link is quiet, so",
            "the alert says 'host problem, not the network'.")
    set_stage("4. CPU stress", "busy loop on h5: cpu alert, host link quiet -> host problem")
    wait_enter()
    ev.mark()
    run(h5, "timeout 15 sh -c 'while :; do :; done' &")
    alert = ev.wait_for(10, kind="cpu", subject="h5",
                        type=("RAISED", "CHANGED"))
    ev.sleep(13)
    say("  busy loop ended, waiting for RESOLVED (10 s cooldown) ...")
    resolved = ev.wait_for(20, type="RESOLVED", kind="cpu", subject="h5")
    check("4 CPU stress", bool(alert and "host problem" in alert["text"]
                               and resolved),
          "cpu alert on h5 with 'host problem', then resolved" if alert
          else "no cpu alert on h5")


def scenario_loss(net, ev):
    h7 = net["h7"]
    heading(5, "Lossy link: ~33 % packet loss on h7")
    explain("Linux traffic control (netem) now drops about a third of the packets",
            "h7 sends: after each packet that gets through, the next one is dropped",
            "with probability 50 %, but never two in a row. (Plain random loss would",
            "sometimes drop 3 messages in a row = 6 s of silence, and the controller",
            "would rightly call h7 unreachable instead.)",
            "The controller counts gaps in h7's telemetry sequence numbers over a",
            "30 s window: above 5 % -> WARNING, above 20 % -> CRITICAL loss h7.")
    set_stage("5. Lossy link", "~33 % loss on h7's link: telemetry loss alert")
    wait_enter()
    ev.mark()
    # Mininet's config() rebuilds h7's qdiscs (HTB for 100 Mbit/s with a netem
    # child, handle 10:); its loss= option only knows plain random loss, so
    # the netem qdisc is then switched to netem's 4-state loss model:
    # p13 = 50 % (good -> loss), p31 = 100 % (loss -> good, i.e. no bursts).
    h7.intf().config(bw=topo.HOST_LINK_BW, loss=1)
    h7.cmd("tc qdisc change dev h7-eth0 handle 10: netem loss state 50% 100%")
    netem = re.search(r"netem.*?(loss.*?)( seed|$)",
                      h7.cmd("tc qdisc show dev h7-eth0"), re.M)
    say(f"  {DIM}tc: h7-eth0 htb 100 Mbit + netem "
        f"{netem.group(1) if netem else '(not found!)'}{RESET}")
    # Loss is random: keep it on until the alert reaches CRITICAL (usually
    # 10-20 s), or 40 s at most, then report the highest severity reached.
    ev.wait_for(40, kind="loss", subject="h7", severity="CRITICAL",
                type=("RAISED", "CHANGED"))
    alerts = [e for e in ev.history[ev.mark_index:]
              if e["kind"] == "loss" and e["subject"] == "h7"
              and e["type"] in ("RAISED", "CHANGED")]
    alert = next((e for e in alerts if e["severity"] == "CRITICAL"),
                 alerts[-1] if alerts else None)
    ev.sleep(2)
    h7.intf().config(bw=topo.HOST_LINK_BW)
    say(f"  {DIM}tc: h7-eth0 loss removed{RESET} (the alert clears once the 30 s")
    say("  window is clean again, checked at the end of the demo)")
    check("5 Lossy link", alert is not None,
          f"{alert['severity']} loss h7: {alert['text']}" if alert
          else "no loss alert on h7")


def scenario_agent_failure(net, ev):
    h8 = net["h8"]
    heading(6, "Agent failure on h8 (host still alive)")
    explain("h8 pings h9 in the background, then its agent is killed. No telemetry",
            "for 3 intervals (6 s) -> CRITICAL unreachable h8. The controller looks",
            "at h8's switch port: it is up and still receiving h8's pings, so the",
            "host is alive and the cause is 'agent failure'. Restarting the agent",
            "resolves the alert.")
    set_stage("6. Agent failure", "h8's agent killed while h8 keeps pinging")
    wait_enter()
    ev.mark()
    run(h8, "ping -i 0.2 10.0.0.9 >/dev/null 2>&1 &")
    time.sleep(2)
    run(h8, "kill %python3")
    alert = ev.wait_for(15, type="RAISED", kind="unreachable", subject="h8")
    ev.sleep(2)
    say("  restarting h8's agent")
    say(f"  {DIM}h8>{RESET} python3 agent/agent.py --node-id h8 "
        f"--monitor-ip 10.0.0.100 ... &")
    topo.start_agent(h8, append=True)
    resolved = ev.wait_for(10, type="RESOLVED", kind="unreachable",
                           subject="h8")
    run(h8, "kill %ping", show=False)
    check("6 Agent failure", bool(alert and "agent failure" in alert["text"]
                                  and resolved),
          "unreachable h8, cause agent failure, resolved after restart" if alert
          else "no unreachable alert for h8")


def scenario_link_failure(net, ev):
    heading(7, "Link failure: s3-s7 goes down")
    explain("The link between s3 and s7 is taken down. Both switches send an",
            "OpenFlow PortStatus message -> CRITICAL link s3-s7. h10-h12 (behind",
            "s7) go silent -> unreachable, and this time the controller finds a",
            "down link on their path to the monitor, so the cause is 'link s3-s7",
            "is down'. Bringing it back resolves everything.")
    set_stage("7. Link failure", "s3-s7 down: link alert + h10-h12 unreachable")
    wait_enter()
    ev.mark()
    say(f"  {DIM}mininet>{RESET} link s3 s7 down")
    net.configLinkStatus("s3", "s7", "down")
    link = ev.wait_for(5, type="RAISED", kind="link", subject="s3-s7")
    hosts_down = []
    for h in ("h10", "h11", "h12"):
        e = ev.wait_for(15, type="RAISED", kind="unreachable", subject=h)
        if e and "link s3-s7" in e["text"]:
            hosts_down.append(h)
    ev.sleep(3)
    say(f"  {DIM}mininet>{RESET} link s3 s7 up")
    net.configLinkStatus("s3", "s7", "up")
    resolved = ev.wait_for(5, type="RESOLVED", kind="link", subject="s3-s7")
    back = [h for h in ("h10", "h11", "h12")
            if ev.wait_for(10, type="RESOLVED", kind="unreachable", subject=h)]
    check("7 Link failure", bool(link and resolved) and len(hosts_down) == 3
          and len(back) == 3,
          f"link alert {'yes' if link else 'NO'}, unreachable with cause "
          f"link s3-s7: {', '.join(hosts_down) or 'none'}, recovered: "
          f"{', '.join(back) or 'none'}")


def final_checks(ev):
    """Things that finish in the background after their scenario moved on."""
    heading(8, "Summary")
    say("  waiting for the last background events (rule expiry, loss window) ...")
    end = time.time() + 40
    while time.time() < end and not (
            ev.seen(type="MITIGATION", subject="h1", text="expired")
            and ev.seen(type="RESOLVED", kind="loss", subject="h7")):
        ev.sleep(1)
    expired = ev.seen(type="MITIGATION", subject="h1", text="expired")
    loss_ok = ev.seen(type="RESOLVED", kind="loss", subject="h7")
    check("3b Rules expired", expired,
          "h1's drop rules expired by hard_timeout" if expired
          else "no 'expired' event for h1")
    check("5b Loss resolved", loss_ok,
          "loss h7 resolved after the loss stopped" if loss_ok
          else "loss h7 not resolved yet")
    remaining = ev._query("SELECT kind, subject, severity FROM active_alerts")
    say()
    for scenario, passed, detail in RESULTS:
        mark = f"{GREEN}PASS {RESET}" if passed else f"{YELLOW}CHECK{RESET}"
        say(f"  {mark} {scenario:<18} {DIM}{detail}{RESET}")
    passed = sum(1 for _, p, _ in RESULTS if p)
    say()
    say(f"  {BOLD}{passed}/{len(RESULTS)} checks passed{RESET}"
        + (f", still active: {remaining}" if remaining else
           ", no alerts left active"))


def save_outputs(out):
    """Copy the database, agent logs and topology.json into the results
    folder and hand the files to the user who ran sudo."""
    try:
        src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        dst = sqlite3.connect(os.path.join(out, "monitor.db"))
        src.backup(dst)
        rows = dst.execute("SELECT ts, type, kind, subject, severity, text "
                           "FROM events ORDER BY id").fetchall()
        with open(os.path.join(out, "events.log"), "w") as f:
            for ts, type_, kind, subject, severity, text in rows:
                f.write(f"{time.strftime('%H:%M:%S', time.localtime(ts))} "
                        f"{type_:<10} {severity:<8} {kind} {subject}: {text}\n")
        src.close()
        dst.close()
    except sqlite3.Error as e:
        say(f"  could not copy the database: {e}")
    for name in os.listdir(topo.LOG_DIR):
        if name.startswith("agent-") or name == "topology.json":
            shutil.copy(os.path.join(topo.LOG_DIR, name), out)
    uid = int(os.environ.get("SUDO_UID", 0))
    gid = int(os.environ.get("SUDO_GID", 0))
    for name in os.listdir(out):
        os.chown(os.path.join(out, name), uid, gid)


# --- Main -------------------------------------------------------------------------

def controller_ready():
    """True if the controller is running and writing the database."""
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2)
        row = conn.execute("SELECT value FROM meta WHERE key IN "
                           "('flushed', 'started') ORDER BY key = 'flushed' "
                           "DESC LIMIT 1").fetchone()
        conn.close()
        return row is not None and time.time() - float(row[0]) < 10
    except (sqlite3.Error, ValueError):
        return False


def main():
    global LOG_FILE, AUTO
    p = argparse.ArgumentParser(description="Guided demo of the SDN monitor")
    p.add_argument("--auto", action="store_true",
                   help="don't pause before each scenario")
    p.add_argument("--out", help="results folder (default: "
                   "results/demo-<date>-<time>)")
    p.add_argument("--no-cli", action="store_true",
                   help="don't offer the Mininet CLI at the end")
    args = p.parse_args()
    AUTO = args.auto

    if os.geteuid() != 0:
        sys.exit("Mininet needs root: sudo python3 topology/demo.py")
    if not controller_ready():
        sys.exit("The controller isn't running (no fresh logs/monitor.db).\n"
                 "Start it first: ryu-manager --ofp-tcp-listen-port 6653 "
                 "controller/monitor_app.py   (or use ./demo.sh)")

    out = args.out or os.path.join(topo.PROJECT_DIR, "results",
                                   time.strftime("demo-%Y%m%d-%H%M"))
    os.makedirs(out, exist_ok=True)
    LOG_FILE = os.path.join(out, "demo.log")
    open(LOG_FILE, "w").close()

    # Mininet's own output (link setup, harmless HTB warnings) would bury
    # the demo text: only show real problems while building.
    setLogLevel("critical")
    net = Mininet(topo=topo.MonitoringTopo(), switch=OVSKernelSwitch,
                  link=TCLink, controller=None, autoSetMacs=True)
    net.addController("c0", controller=RemoteController,
                      ip=topo.CONTROLLER_IP, port=topo.CONTROLLER_PORT)
    topo.add_monitor_node(net)
    topo.export_topology(net, topo.TOPOLOGY_JSON)
    say(f"{BOLD}Building the network ...{RESET}")
    net.start()
    # "output": Mininet's normal command output (pingall, the CLI) shows,
    # its progress messages ("info") don't.
    setLogLevel("output")
    ev = Events(DB_PATH)
    os.chdir(topo.PROJECT_DIR)      # so "agent/agent.py" in the echo is right
    try:
        topo.start_agents(net)
        time.sleep(5)               # switches connect, agents send twice
        scenario_startup(net, ev)
        scenario_baseline(net, ev)
        scenario_congestion(net, ev, out)
        scenario_flood(net, ev)
        scenario_cpu(net, ev)
        scenario_loss(net, ev)
        scenario_agent_failure(net, ev)
        scenario_link_failure(net, ev)
        final_checks(ev)
        set_stage("Demo finished", "Mininet CLI open for questions"
                  if not (AUTO or args.no_cli) else "")
        say(f"\n  Results saved in {os.path.relpath(out, topo.PROJECT_DIR)}/")
        if not (AUTO or args.no_cli):
            say("  Opening the Mininet CLI for questions (type exit to finish).")
            CLI(net)
    except KeyboardInterrupt:
        say("\n  Interrupted.")
    finally:
        for h in net.hosts:
            h.cmd("kill %iperf %ping %hping3 2>/dev/null")
        quietRun("pkill hping3")
        topo.stop_agents(net)
        setLogLevel("critical")
        net.stop()
        try:
            os.remove(STAGE_FILE)
        except OSError:
            pass
        save_outputs(out)


if __name__ == "__main__":
    main()
