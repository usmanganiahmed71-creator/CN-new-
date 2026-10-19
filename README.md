# NetTwin-SDN

A self-healing network demo for the Computer Networks lab (BACSE203). One server and three clients run on a virtual network of four OpenFlow switches. A **Ryu** controller watches link health, and **Fast Failover Groups** move traffic to a backup path when the primary path breaks. **Wireshark** shows the packets.

Everything runs on **one Linux machine** (a Kali or Ubuntu VM is fine). Mininet creates all hosts and switches virtually, so no second VM is needed.

```
                 path A (primary)
          +====== s2 ======+
          |                |
 c1,c2,c3 == s1            s4 == srv
          |                |
          +====== s3 ======+
                 path B (backup)
```

| Device | Role | IP |
|---|---|---|
| c1, c2, c3 | Clients | 10.0.0.1, 10.0.0.2, 10.0.0.3 |
| srv | Flask web server (port 5000) | 10.0.0.100 |
| s1 | Switch next to the clients | none |
| s2 / s3 | Core switches, path A / path B | none |
| s4 | Switch next to the server | none |

Switch ports (Wireshark interfaces are named after them, e.g. `s1-eth4` is port 4 of s1):

| Link | Ports |
|---|---|
| c1, c2, c3 to s1 | s1 ports 1, 2, 3 |
| s1 to s2 (path A) | s1:4 to s2:1 |
| s2 to s4 (path A) | s2:2 to s4:1 |
| s1 to s3 (path B) | s1:5 to s3:1 |
| s3 to s4 (path B) | s3:2 to s4:2 |
| srv to s4 | s4 port 3 |

## How the self-healing works

1. s1 and s4 each have a **Fast Failover Group**: "use the port to path A while it is up, otherwise path B". The switch flips by itself, so recovery is almost instant.
2. If only one half of path A breaks (say s1-s2), s1 notices but s4 does not, and s4 would keep sending replies into the break. The **controller** listens for link-down events and re-orders the group on **both** s1 and s4 so both directions agree.
3. All forwarding rules are installed up front (proactively), so no packet ever waits for the controller.

## Quick start

For Kali (Ubuntu works the same), one VM. Commands run from the repo root.

**One-time setup** (installs Mininet, Open vSwitch, Wireshark, Flask and a private Python 3.9 for Ryu):

```bash
bash scripts/setup_kali.sh
```

**Option A: All-in-One Automated Demo (Starts Controller, Network & 7 Terminals)**:

```bash
bash scripts/demo_all.sh
```

This launches the Ryu controller, starts the virtual Mininet network, and automatically pops up **7 dedicated terminals**:
- **3 Systems**: System 1 (`c1`), System 2 (`c2`), System 3 (`c3`)
- **3 Routers**: Router 1 (`s1` Ingress), Router 2 (`s2` Path A Core), Router 3 (`s3` Path B Core)
- **1 Main Server**: Server (`srv` 10.0.0.100:5000)
- Plus a live **Ryu Controller Telemetry Monitor** window

**Option B: Manual Step-by-Step Run**:

```bash
# Terminal 1: controller (leave it running, it prints every link event)
bash scripts/run_controller.sh

# Terminal 2: network (with 7 GUI terminals automatically arranged)
bash scripts/run_network.sh --terminals
```

*(Or use `--tmux` instead of `--terminals` to run in a 4-window tmux dashboard!)*

Inside the `netwin>` prompt:
```
netwin> pingall
netwin> startserver
netwin> starttraffic
netwin> cutlink        # Cuts primary link s1-s2 (triggers instant failover to Path B)
netwin> restorelink    # Restores link s1-s2 (returns to Path A)
netwin> terminals      # Re-opens or spawns terminals at any time
```

Open Wireshark with `sudo wireshark &` and capture on `s1-eth4` and `s1-eth5` to watch traffic move from path A to path B.

Full walkthrough (Wireshark, deny/allow a client, failover timing, before/after graph, server-crash demo, troubleshooting): **[docs/DEMO.md](docs/DEMO.md)**. Manual install steps behind the setup script: **[docs/SETUP.md](docs/SETUP.md)**.

When finished: type `exit` at `netwin>`, press Ctrl+C in Terminal 1, then `bash scripts/cleanup.sh`.

## Repository layout

```
nettwin/
  netmodel.py          Single source of truth: hosts, switches, links, ports, paths
  terminals.py         Multi-terminal manager (3 systems, 3 routers, 1 server)
  router_cli.py        Interactive router console (ports, flows, groups, block/allow)
  system_env.sh        Environment & aliases for client system terminals
  server_env.sh        Environment & aliases for main server terminal
  network/             Mininet side: builder.py (topology) and __main__.py (CLI entry)
  controller/          Ryu side
    app.py             Ryu events only: switch connected, port up/down
    failover.py        LinkHealth: which links are down, which path is best (pure logic)
    programmer.py      The only code that builds OpenFlow messages
  server/app.py        Flask server that runs on srv
scripts/               demo_all.sh, launch_terminals.sh, run_controller.sh, run_network.sh, client.sh, cleanup.sh
tests/                 Unit tests for pure logic, netmodel, and router CLI
docs/                  SETUP.md, DEMO.md
```

## Languages and tools

| What | Used for |
|---|---|
| Python 3 | Topology (Mininet), controller (Ryu, runs in its own Python 3.9), server (Flask), tests |
| Bash | Setup, run and client scripts |
| OpenFlow 1.3 | Switch programming (flow rules and Fast Failover Groups) |
| Markdown | Documentation |

Tools: Mininet (virtual network), Open vSwitch (the switches), Ryu (controller), Flask (server), curl (client requests), Wireshark (packet inspection), `ovs-ofctl` (inspect and edit rules by hand).

## Design principles

| Principle | Where it shows |
|---|---|
| DRY | `netmodel.py` declares ports and paths once; Mininet builder, flow rules, failover ports and path health are all derived from it |
| SRP / SoC | `app.py` handles events, `failover.py` decides the best path, `programmer.py` speaks OpenFlow, `builder.py` builds the network, `server/` serves HTTP |
| Dependency inversion | `LinkHealth` receives the path-to-ports map instead of importing the topology |
| KISS / YAGNI | No REST API, database or UI; proactive rules only; features not needed for the demo are left out |

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

Standard library only, works on any Python 3.9+.

## Scope and known limits

Implemented: virtual network, controller, Fast Failover with controller-assisted path agreement, per-client deny/allow, Wireshark inspection, failover measurement.

Not implemented (described in the design notes as extensions): TLS mutual authentication on the OpenFlow channel, an active server health-check, controller redundancy. OpenFlow cannot see an application crash behind a live link; Section 11 of `docs/DEMO.md` demonstrates exactly that gap.


## Server health telemetry
The Ryu controller probes the Flask `/health` endpoint every two seconds and records response latency when the server is healthy. It distinguishes `HEALTHY`, `SERVER_UNRESPONSIVE`, `NETWORK_PATH_UNAVAILABLE`, and `NETWORK_AND_SERVER_UNAVAILABLE`. A path change cannot repair the only server, so server replacement remains outside this single-server topology.
