#!/usr/bin/env python3
"""
UDP telemetry agent. Runs inside a Mininet host (system python3 + psutil).

Every --interval seconds it sends one JSON datagram to the monitor:
    {"node_id": "h1", "seq": 42, "timestamp": 1790000000.0,
     "cpu_pct": 12.5, "mem_pct": 40.1,
     "tx_bytes": 123456, "rx_bytes": 654321, "app_tx_bytes": 7140}

- seq increases by 1 per message, so the receiver can count gaps as loss.
- tx_bytes / rx_bytes are cumulative counters of the host's interface.
- app_tx_bytes is the cumulative bytes sent by this agent only (on-the-wire
  size), so the controller can tell "traffic the application explains" apart
  from everything else leaving the host.

Usage (normally started by topology/topology.py):
    python3 agent/agent.py --node-id h1 --monitor-ip 10.0.0.100 --port 9999 --interval 2
"""

import argparse
import json
import signal
import socket
import sys
import time

try:
    import psutil
except ImportError:
    sys.exit("psutil not found: sudo apt install python3-psutil")


# Ethernet (14) + IPv4 (20) + UDP (8) headers. Adding these to the JSON length
# gives the size the switch actually counts, so app_tx_bytes is comparable
# with OpenFlow port byte counters.
HEADER_OVERHEAD = 14 + 20 + 8


def log(msg):
    # flush=True: stdout is redirected to a log file, which is block-buffered
    # by default; without flushing, nothing appears until the agent exits.
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def interface_counters(node_id):
    """Return (tx_bytes, rx_bytes) for this host's interface.

    Inside a Mininet host, /proc/net/dev (which psutil reads) only shows that
    host's namespace: "lo" and "<node_id>-eth0". Prefer the eth0 interface;
    fall back to the sum of all non-loopback interfaces.
    """
    nics = psutil.net_io_counters(pernic=True)
    main = nics.get(f"{node_id}-eth0")
    if main is not None:
        return main.bytes_sent, main.bytes_recv
    others = [c for name, c in nics.items() if name != "lo"]
    return sum(c.bytes_sent for c in others), sum(c.bytes_recv for c in others)


def parse_args():
    p = argparse.ArgumentParser(description="UDP telemetry agent")
    p.add_argument("--node-id", required=True, help="e.g. h1")
    p.add_argument("--monitor-ip", default="10.0.0.100")
    p.add_argument("--port", type=int, default=9999)
    p.add_argument("--interval", type=float, default=2.0, help="seconds")
    return p.parse_args()


def main():
    args = parse_args()
    dest = (args.monitor_ip, args.port)

    # The topology stops agents with `kill` (SIGTERM). Turn that into a normal
    # exit so the finally block below still runs, same as Ctrl-C.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))

    # SOCK_DGRAM = UDP: no connection, each sendto() is one independent packet.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # The first cpu_percent() call always returns 0.0; it just sets a baseline.
    psutil.cpu_percent(interval=None)

    seq = 0
    app_tx_bytes = 0
    next_send = time.monotonic()
    log(f"{args.node_id}: sending to {dest[0]}:{dest[1]} every {args.interval}s")

    try:
        while True:
            seq += 1
            tx_bytes, rx_bytes = interface_counters(args.node_id)
            msg = {
                "node_id": args.node_id,
                "seq": seq,
                "timestamp": time.time(),
                "cpu_pct": psutil.cpu_percent(interval=None),
                "mem_pct": psutil.virtual_memory().percent,
                "tx_bytes": tx_bytes,
                "rx_bytes": rx_bytes,
                "app_tx_bytes": app_tx_bytes,
            }
            data = json.dumps(msg).encode()

            try:
                sock.sendto(data, dest)
                app_tx_bytes += len(data) + HEADER_OVERHEAD
            except OSError as e:
                # e.g. "Network is unreachable". Keep going: seq was still
                # incremented, so the receiver will see this as a lost message.
                log(f"send failed (seq={seq}): {e}")

            if seq == 1:
                log(f"first message sent ({len(data)} bytes)")

            # Schedule against a fixed timeline so sends don't drift later
            # and later by the time each loop iteration takes.
            next_send += args.interval
            time.sleep(max(0.0, next_send - time.monotonic()))
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        log(f"{args.node_id}: stopped after {seq} messages")


if __name__ == "__main__":
    main()
