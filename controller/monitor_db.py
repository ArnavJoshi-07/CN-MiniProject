"""
SQLite storage for the monitor: the controller writes, the dashboard reads.

The Ryu app (monitor_app.py) owns one MonitorDB. It never blocks on the
database while handling telemetry or port stats: the add_* methods only
append to in-memory lists, and flush(), called every 2 s by the app's writer
thread, writes them plus a snapshot of the current state in one transaction.

Tables
    History (one row per sample, pruned after KEEP_SECONDS):
        telemetry   ts, node_id, seq, cpu_pct, mem_pct, app_tx_bps
        link_util   ts, link ("s1->s3"), util (%), bps, capacity_bps
    Event log (kept for the whole run):
        events      id, ts, type (RAISED / CHANGED / RESOLVED / MITIGATION),
                    kind, subject, severity, text
    Snapshots of "now" (replaced on every flush):
        nodes          one row per host: status, seq, loss, cpu, ...
        active_alerts  one row per active alert
        mitigations    one row per host whose drop rules are installed
        links          one row per link: both directions' utilization, up/down
        meta           key/value: when the controller started, last flush

The file is recreated every time the controller starts, so each demo run
starts empty. WAL mode lets the dashboard read while the controller writes.

All timestamps are Unix time (time.time()), so the dashboard can show them
as clock times.
"""

import os
import sqlite3
import time

KEEP_SECONDS = 30 * 60      # history older than this is deleted
PRUNE_EVERY = 60            # seconds between prunes

SCHEMA = """
CREATE TABLE telemetry (
    ts REAL, node_id TEXT, seq INTEGER,
    cpu_pct REAL, mem_pct REAL, app_tx_bps REAL);
CREATE INDEX telemetry_ts ON telemetry (ts);

CREATE TABLE link_util (
    ts REAL, link TEXT, util REAL, bps REAL, capacity_bps REAL);
CREATE INDEX link_util_ts ON link_util (ts);

CREATE TABLE events (
    id INTEGER PRIMARY KEY, ts REAL, type TEXT,
    kind TEXT, subject TEXT, severity TEXT, text TEXT);

CREATE TABLE nodes (
    node_id TEXT PRIMARY KEY, status TEXT, last_seen REAL,
    last_seq INTEGER, received INTEGER, lost INTEGER,
    loss_pct REAL, window_loss_pct REAL,
    cpu_pct REAL, mem_pct REAL, app_tx_bps REAL);

CREATE TABLE active_alerts (
    kind TEXT, subject TEXT, severity TEXT, since REAL, text TEXT,
    PRIMARY KEY (kind, subject));

CREATE TABLE mitigations (
    host TEXT PRIMARY KEY, port TEXT, expires REAL);

CREATE TABLE links (
    name TEXT PRIMARY KEY, a TEXT, b TEXT, capacity_bps REAL, up INTEGER,
    util_ab REAL, util_ba REAL, bps_ab REAL, bps_ba REAL);

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


class MonitorDB:
    def __init__(self, path):
        """Create a fresh database at `path`, deleting any previous one."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        # NORMAL: commits don't wait for the disk; on a crash the last commit
        # may be lost, which is fine for monitoring data.
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.execute("INSERT INTO meta VALUES ('started', ?)",
                          (str(time.time()),))
        self.conn.commit()
        self.last_prune = time.monotonic()

        # Rows waiting for the next flush.
        self.pending_telemetry = []
        self.pending_util = []
        self.pending_events = []

    # --- Buffered history (cheap: no SQL until flush) ---------------------

    def add_telemetry(self, msg, app_tx_bps):
        self.pending_telemetry.append(
            (time.time(), msg["node_id"], msg["seq"], msg["cpu_pct"],
             msg["mem_pct"], app_tx_bps))

    def add_link_util(self, link, util, bps, capacity_bps):
        self.pending_util.append((time.time(), link, util, bps, capacity_bps))

    def add_event(self, type_, kind, subject, severity, text):
        self.pending_events.append(
            (time.time(), type_, kind, subject, severity, text))

    # --- Flush: buffered rows + snapshots, one transaction ----------------

    def flush(self, nodes, alerts, mitigations, links):
        """Write everything buffered and replace the snapshot tables.

        nodes:       list of tuples matching the `nodes` columns
        alerts:      list of (kind, subject, severity, since, text)
        mitigations: list of (host, port, expires)
        links:       list of tuples matching the `links` columns
        """
        c = self.conn
        with c:     # one transaction: commit at the end, rollback on error
            c.executemany("INSERT INTO telemetry VALUES (?,?,?,?,?,?)",
                          self.pending_telemetry)
            c.executemany("INSERT INTO link_util VALUES (?,?,?,?,?)",
                          self.pending_util)
            c.executemany("INSERT INTO events (ts, type, kind, subject, "
                          "severity, text) VALUES (?,?,?,?,?,?)",
                          self.pending_events)
            c.execute("DELETE FROM nodes")
            c.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          nodes)
            c.execute("DELETE FROM active_alerts")
            c.executemany("INSERT INTO active_alerts VALUES (?,?,?,?,?)",
                          alerts)
            c.execute("DELETE FROM mitigations")
            c.executemany("INSERT INTO mitigations VALUES (?,?,?)",
                          mitigations)
            c.execute("DELETE FROM links")
            c.executemany("INSERT INTO links VALUES (?,?,?,?,?,?,?,?,?)",
                          links)
            c.execute("INSERT OR REPLACE INTO meta VALUES ('flushed', ?)",
                      (str(time.time()),))
            if time.monotonic() - self.last_prune >= PRUNE_EVERY:
                cutoff = time.time() - KEEP_SECONDS
                c.execute("DELETE FROM telemetry WHERE ts < ?", (cutoff,))
                c.execute("DELETE FROM link_util WHERE ts < ?", (cutoff,))
                self.last_prune = time.monotonic()
        # Only cleared once the transaction succeeded; on an error they are
        # retried with the next flush.
        self.pending_telemetry.clear()
        self.pending_util.clear()
        self.pending_events.clear()
