"""
Live dashboard for the SDN network monitor.

Reads logs/monitor.db, which the Ryu controller (controller/monitor_app.py)
rewrites every 2 s, and redraws itself every 2 s. It never talks to the
controller or to Mininet directly: the database is the only link, so the
dashboard can be started, stopped or reloaded at any time.

Run (from the project root):
    cd dashboard && uv run streamlit run app.py
then open http://localhost:8501.

What it shows, top to bottom:
    1. Summary numbers: nodes reporting, active alerts, mitigations, links down
    2. Topology: every switch, host and link, coloured by its state
    3. Active alerts, and the current demo step (written by topology/demo.py)
    4. Link utilization over time for the busiest links (70 / 90 % lines)
    5. Per-node table: status, seq, telemetry loss, CPU, agent send rate
    6. Host CPU over time, and the event log (alerts raised / resolved,
       mitigation rules installed / expired)
"""

import json
import os
import sqlite3
import time
from datetime import datetime

import altair as alt
import pandas as pd
import streamlit as st

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("MONITOR_DB",
                         os.path.join(PROJECT_DIR, "logs", "monitor.db"))
DEMO_STAGE = os.path.join(PROJECT_DIR, "logs", "demo_stage.json")
REFRESH = 2                 # seconds, same as the controller's DB writes

# Colours shared by the graph and the tables. Fills are light enough for
# black text in both the light and the dark Streamlit theme.
STATUS_COLOURS = {
    "ok": "#9be3a7", "warning": "#ffe08a", "critical": "#ffb37a",
    "mitigated": "#c9b3ff", "unreachable": "#ff8a8a", "no data": "#d0d0d0",
}
SEVERITY_COLOURS = {"CRITICAL": "#ff8a8a", "WARNING": "#ffe08a",
                    "ACTION": "#c9b3ff", "INFO": "#d0e8ff"}
TYPE_COLOURS = {"RESOLVED": "#9be3a7", "MITIGATION": "#c9b3ff"}


def util_colour(util):
    """Edge colour for a link utilization (%), None = no data yet."""
    if util is None or util < 1:
        return "#9a9a9a"        # idle
    if util < 70:
        return "#2e9e4f"        # in use
    if util < 90:
        return "#e69500"        # WARNING range
    return "#d62828"            # CRITICAL range


# --- Data -------------------------------------------------------------------

def connect():
    """Read-only connection, or None if the controller hasn't made the
    database yet. A new connection per refresh is cheap with SQLite and
    always sees the controller's latest commit."""
    if not os.path.exists(DB_PATH):
        return None
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=1)


def query(conn, sql, params=()):
    return pd.read_sql_query(sql, conn, params=params)


def num(series):
    return pd.to_numeric(series, errors="coerce")


def clock(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else ""


def demo_stage():
    """The step topology/demo.py is showing now, or None."""
    try:
        with open(DEMO_STAGE) as f:
            stage = json.load(f)
    except (OSError, ValueError):
        return None
    # A file left over from an earlier run is not "now".
    return stage if time.time() - stage.get("ts", 0) < 15 * 60 else None


# --- Topology graph -----------------------------------------------------------

def topology_dot(nodes, links):
    """Graphviz DOT source of the network, coloured by current state.

    Hosts take the colour of their status; links the colour of their busier
    direction, thicker for faster links, dashed red when down. Graphviz lays
    the tree out by itself: the core on top, hosts at the bottom.
    """
    status = dict(zip(nodes["node_id"], nodes["status"]))
    lines = [
        "graph G {",
        '  graph [bgcolor="transparent", nodesep=0.15, ranksep=0.55];',
        '  node [style="filled", fontname="Helvetica", fontsize=11, '
        'fontcolor="black", penwidth=0];',
        '  edge [fontname="Helvetica", fontsize=9, fontcolor="#888888"];',
    ]
    names = set(links["a"]) | set(links["b"])
    for name in sorted(names):
        if name == "monitor":
            lines.append('  monitor [shape=box, style="rounded,filled", '
                         'fillcolor="#d0e8ff", label="monitor\\n10.0.0.100"];')
        elif name.startswith("s"):
            lines.append(f'  {name} [shape=box, style="rounded,filled", '
                         f'fillcolor="#e6e6e6"];')
        else:
            colour = STATUS_COLOURS.get(status.get(name, "no data"))
            lines.append(f'  {name} [shape=ellipse, fillcolor="{colour}", '
                         f'width=0.5, height=0.35];')

    for link in links.itertuples():
        # Switches above hosts: draw each edge from the upper end down.
        a, b = link.a, link.b
        if a.startswith("h"):
            a, b = b, a
        mbit = link.capacity_bps / 1e6
        width = {100: 3.0, 50: 2.0}.get(round(mbit), 1.2)
        values = [u for u in (link.util_ab, link.util_ba) if pd.notna(u)]
        util = max(values) if values else None
        if not link.up:
            attrs = (f'color="#d62828", style=dashed, penwidth={width}, '
                     f'label=" DOWN", fontcolor="#d62828"')
        else:
            label = f" {mbit:.0f}M"
            if util is not None and util >= 1:
                label += f"\\n {util:.0f}%"
            attrs = (f'color="{util_colour(util)}", penwidth={width}, '
                     f'label="{label}"')
        lines.append(f"  {a} -- {b} [{attrs}];")
    lines.append("}")
    return "\n".join(lines)


# --- Page -------------------------------------------------------------------

st.set_page_config(page_title="SDN Network Monitor", page_icon="📡",
                   layout="wide")
st.title("SDN Network Monitor")
st.caption("Mininet + Ryu (OpenFlow 1.3). UDP telemetry from 12 hosts, "
           "correlated with switch port statistics. Live from "
           f"`{os.path.relpath(DB_PATH, PROJECT_DIR)}`, refreshed every "
           f"{REFRESH} s.")

with st.sidebar:
    st.header("View")
    window_min = st.slider("Chart window (minutes)", 1, 15, 3)
    top_links = st.slider("Links in the utilization chart", 3, 12, 6)
    st.divider()
    st.markdown("**Host colours**")
    for name, colour in STATUS_COLOURS.items():
        st.markdown(f'<span style="background:{colour};color:black;'
                    f'padding:1px 8px;border-radius:4px">{name}</span>',
                    unsafe_allow_html=True)
    st.markdown("**Link colours**: grey idle, green in use, "
                "orange ≥ 70 %, red ≥ 90 %, dashed red = down")


@st.fragment(run_every=REFRESH)
def live():
    conn = connect()
    if conn is None:
        st.info("Waiting for the controller: start it with `ryu-manager "
                "--ofp-tcp-listen-port 6653 controller/monitor_app.py` "
                "(or `./demo.sh`).")
        return
    try:
        render(conn)
    except (sqlite3.Error, pd.errors.DatabaseError) as e:
        # e.g. the controller restarted and is recreating the file.
        st.warning(f"Database not readable right now ({e}); retrying.")
    finally:
        conn.close()


def render(conn):
    now = time.time()
    since = now - window_min * 60
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    nodes = query(conn, "SELECT * FROM nodes")
    alerts = query(conn, "SELECT * FROM active_alerts "
                         "ORDER BY severity = 'WARNING', since")
    mitigations = query(conn, "SELECT * FROM mitigations")
    links = query(conn, "SELECT * FROM links")

    # 1. Summary
    flushed = float(meta.get("flushed", 0))
    reporting = int((~nodes["status"].isin(["unreachable", "no data"])).sum())
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Nodes reporting", f"{reporting} / {len(nodes)}")
    c2.metric("Critical alerts", int((alerts["severity"] == "CRITICAL").sum()))
    c3.metric("Warnings", int((alerts["severity"] == "WARNING").sum()))
    c4.metric("Mitigations active", len(mitigations))
    c5.metric("Links down", int((links["up"] == 0).sum()) if len(links) else 0,
              help="From OpenFlow PortStatus messages")
    if flushed and now - flushed > 3 * REFRESH:
        st.error(f"No update from the controller for {now - flushed:.0f} s. "
                 "Is ryu-manager still running?")

    stage = demo_stage()
    if stage:
        st.info(f"**Demo — {stage.get('title', '')}**  \n"
                f"{stage.get('detail', '')}")

    # 2 + 3. Topology and alerts side by side
    left, right = st.columns([3, 2])
    with left:
        st.subheader("Topology")
        if len(links):
            st.graphviz_chart(topology_dot(nodes, links), width="stretch")
        else:
            st.caption("No topology yet: start Mininet.")
    with right:
        st.subheader("Active alerts")
        if len(alerts):
            # One callout per alert, so the full text wraps instead of
            # being cut off in a table cell.
            for a in alerts.itertuples():
                box = st.error if a.severity == "CRITICAL" else st.warning
                box(f"**{a.severity} {a.kind} {a.subject}** "
                    f"(since {clock(a.since)}, {now - a.since:.0f} s)  \n"
                    f"{a.text}")
        else:
            st.success("No active alerts")
        if len(mitigations):
            st.markdown("**Mitigation rules installed**")
            st.dataframe(pd.DataFrame({
                "host": mitigations["host"], "switch port": mitigations["port"],
                "expires in": [f"{max(0, e - now):.0f} s"
                               for e in mitigations["expires"]]}),
                hide_index=True, width="stretch")

    # 4. Link utilization over time
    st.subheader("Link utilization")
    util = query(conn, "SELECT ts, link, util FROM link_util WHERE ts >= ?",
                 (since,))
    busiest = (util.groupby("link")["util"].max().sort_values(ascending=False)
               if len(util) else pd.Series(dtype=float))
    busiest = busiest[busiest >= 1].head(top_links)
    if len(busiest):
        data = util[util["link"].isin(busiest.index)].copy()
        data["time"] = pd.to_datetime(data["ts"], unit="s", utc=True) \
            .dt.tz_convert(datetime.now().astimezone().tzinfo)
        lines_chart = alt.Chart(data).mark_line(interpolate="monotone").encode(
            x=alt.X("time:T", title=None),
            y=alt.Y("util:Q", title="utilization %",
                    scale=alt.Scale(domain=[0, 105])),
            color=alt.Color("link:N", sort=list(busiest.index), title="link"),
            tooltip=["link", alt.Tooltip("util:Q", format=".1f"),
                     alt.Tooltip("time:T", format="%H:%M:%S")])
        rules = alt.Chart(pd.DataFrame({"y": [70, 90]})).mark_rule(
            strokeDash=[4, 4], color="#888888").encode(y="y:Q")
        st.altair_chart(lines_chart + rules, width="stretch")
        st.caption("Busiest link directions in the window. Dashed lines: "
                   "WARNING 70 %, CRITICAL 90 %.")
    else:
        st.caption(f"All links idle (under 1 %) in the last {window_min} min.")

    # 5. Nodes, with host CPU beside them
    left, right = st.columns([3, 2])
    with left:
        st.subheader("Nodes")
        render_nodes(nodes, now)
    with right:
        st.subheader("Host CPU")
        render_cpu(conn, since)

    # 6. Event log, full width so the details fit
    st.subheader("Event log")
    render_events(conn)

    started = float(meta.get("started", now))
    st.caption(f"Controller started {clock(started)}, last write "
               f"{clock(flushed)}.")


def render_nodes(nodes, now):
    if len(nodes):
        table = pd.DataFrame({
            "node": nodes["node_id"],
            "status": nodes["status"],
            "seq": num(nodes["last_seq"]),
            "last seen": [f"{now - t:.0f} s ago" if pd.notna(t) else ""
                          for t in nodes["last_seen"]],
            # Columns may hold None (no value yet): make them numeric (NaN)
            # so they can be formatted; NaN shows as an empty cell.
            "loss (30 s) %": num(nodes["window_loss_pct"]),
            "loss total %": num(nodes["loss_pct"]),
            "cpu % (1 core)": num(nodes["cpu_pct"]),
            "agent kbit/s": num(nodes["app_tx_bps"]) / 1e3,
        })
        styled = table.style.map(
            lambda v: f"background-color:{STATUS_COLOURS.get(v, '')};"
                      f"color:black" if v in STATUS_COLOURS else "",
            subset=["status"])
        number = st.column_config.NumberColumn
        # Tall enough for every row (35 px each + header), no scrolling.
        st.dataframe(styled, hide_index=True, width="stretch",
                     height=35 * (len(table) + 1) + 3, placeholder="–",
                     column_config={
                         "status": st.column_config.TextColumn(
                             width="medium"),
                         "seq": number(format="%d"),
                         "loss (30 s) %": number(format="%.1f"),
                         "loss total %": number(format="%.1f"),
                         "cpu % (1 core)": number("cpu %", format="%.1f",
                                                  help="% of one core"),
                         "agent kbit/s": number("agent kbit/s",
                                                format="%.2f")})


def render_cpu(conn, since):
    cpu = query(conn, "SELECT ts, node_id, cpu_pct FROM telemetry "
                      "WHERE ts >= ?", (since,))
    busy = (cpu.groupby("node_id")["cpu_pct"].max() if len(cpu)
            else pd.Series(dtype=float))
    busy = busy[busy >= 5]
    if not len(busy):
        st.caption("All hosts under 5 % CPU in the window.")
        return
    data = cpu[cpu["node_id"].isin(busy.index)].copy()
    data["time"] = pd.to_datetime(data["ts"], unit="s", utc=True) \
        .dt.tz_convert(datetime.now().astimezone().tzinfo)
    chart = alt.Chart(data).mark_line().encode(
        x=alt.X("time:T", title=None),
        y=alt.Y("cpu_pct:Q", title="cpu % of one core",
                scale=alt.Scale(domain=[0, 105])),
        color=alt.Color("node_id:N", title="host"),
        tooltip=["node_id", alt.Tooltip("cpu_pct:Q", format=".0f")])
    rules = alt.Chart(pd.DataFrame({"y": [80, 95]})).mark_rule(
        strokeDash=[4, 4], color="#888888").encode(y="y:Q")
    st.altair_chart(chart + rules, width="stretch")
    st.caption("Hosts above 5 % in the window. Dashed lines: WARNING 80 %, "
               "CRITICAL 95 %.")


def render_events(conn):
    events = query(conn, "SELECT * FROM events ORDER BY id DESC LIMIT 100")
    if not len(events):
        st.caption("No events yet.")
        return
    table = pd.DataFrame({
        "time": events["ts"].map(clock),
        "event": events["type"],
        "alert": events["kind"] + " " + events["subject"],
        "severity": [f"(was {s})" if t == "RESOLVED" else s
                     for t, s in zip(events["type"], events["severity"])],
        "details": events["text"]})

    def colour(row):
        c = TYPE_COLOURS.get(row["event"]) or \
            SEVERITY_COLOURS.get(row["severity"], "")
        return [f"background-color:{c};color:black" if c else ""] * 2 \
            + [""] * (len(row) - 2)
    st.dataframe(table.style.apply(colour, axis=1), hide_index=True,
                 width="stretch", height=380,
                 column_config={"details": st.column_config.TextColumn(
                     width="large")})
    st.caption("Newest first. RAISED / CHANGED / RESOLVED alerts and "
               "MITIGATION actions, as logged by the controller.")


live()
