# Demo walkthrough

Assumes setup is done (`docs/SETUP.md`). Use **three or four terminals**, all in the repo root. Always start the controller first: the switches forward nothing until a controller gives them rules.

| Terminal | Command |
|---|---|
| 1 controller | `bash scripts/run_controller.sh` |
| 2 network | `bash scripts/run_network.sh` (gives the `mininet>` prompt) |
| 3 helper | rules, logs, captures |
| 4 Wireshark | `sudo wireshark &` |

Terminal 1 must print `switch s1 connected` to `switch s4 connected`. If not, see Troubleshooting at the end.

Everything in sections 1, 2, 5, 6 and 7 is typed at `mininet>` unless it says Terminal 3.

---

## 1. Check the network

```
mininet> pingall
```

Expect `0% dropped`. In Terminal 3 look at what Ryu installed:

```bash
sudo ovs-ofctl -O OpenFlow13 dump-groups s1
sudo ovs-ofctl -O OpenFlow13 dump-flows s1
```

The group should read:

```
group_id=1,type=ff,bucket=watch_port:4,actions=output:4,bucket=watch_port:5,actions=output:5
```

"Send out of port 4 (path A) while it is up, otherwise port 5 (path B)." The rule for `10.0.0.100` ends in `actions=group:1`. `-O OpenFlow13` is needed because the switches speak OpenFlow 1.3.

## 2. Start the server and the clients

```
mininet> srv python3 -m nettwin.server.app > /tmp/server.log 2>&1 &
mininet> c1 curl -s http://10.0.0.100:5000/health
```

Wait a few seconds if the first curl prints nothing. Expect `{"status":"ok"}`. Then:

```
mininet> c1 ./scripts/client.sh c1 > /tmp/c1.log 2>&1 &
mininet> c2 ./scripts/client.sh c2 > /tmp/c2.log 2>&1 &
mininet> c3 ./scripts/client.sh c3 > /tmp/c3.log 2>&1 &
```

Watch them in Terminal 3: `tail -f /tmp/c1.log /tmp/c2.log /tmp/c3.log`. A steady stream of `request OK (HTTP 200)` is the normal state.

If a client prints `Permission denied`, run it as `c1 bash scripts/client.sh c1 ...` (file permissions can be lost when the repo passes through Windows).

## 3. Inspect packets with Wireshark

Every switch port is a normal interface in Wireshark (start Mininet first, then Wireshark).

| Interface | What you see |
|---|---|
| `s1-eth1` | Only c1's traffic |
| `s1-eth4` | Everything leaving s1 on path A |
| `s1-eth5` | Everything leaving s1 on path B (quiet until a failover) |
| `s4-eth3` | All traffic to and from the server |

Useful display filters: `http`, `tcp.port == 5000`, `ip.src == 10.0.0.2`, `icmp`.

First look: select `s1-eth4` and `s1-eth5` together (Ctrl-click), then Start. While healthy, only `s1-eth4` shows traffic.

## 4. Deny and allow a client (Router Blocking & Dynamic Re-Routing)

Two different things can be cut: a whole link (a cable breaking, section 5) or one client's access (a router policy or ACL).

### Scenario A: Core Router blocks a client (Router 2 / s2 blocks c2)

Open the **Router 2 (s2)** terminal window (or at `netwin>` prompt):

```
Router-2(s2)# block 10.0.0.2
```

*(Or from `netwin>`: `block s2 10.0.0.2`, or `sudo ovs-ofctl -O OpenFlow13 add-flow s2 "priority=100,ip,nw_src=10.0.0.2,nw_dst=10.0.0.100,actions=drop"`)*

**What happens:**
1. Router 2 (s2) on Path A drops packets from `10.0.0.2`.
2. The **Ryu Controller** detects the block on Router 2.
3. The controller dynamically installs bypass rules on **s1** (output to Port 5 / Path B) and **s4** (return via Port 2 / Path B), steering `10.0.0.2` around Router 2 through Router 3 (Path B).
4. Client `c2`'s traffic continues to succeed (`HTTP 200 OK`) because it was re-routed through Path B!
5. In the **Ryu Controller Live Telemetry Monitor** terminal (or `tail -f /tmp/ryu.log`), the controller displays the active re-routing path and prints a comprehensive risk assessment of possible issues:
   - **Security Policy Bypass**: Traffic was re-routed around the blocking router; if Router 2 blocked `10.0.0.2` as an attacker/firewall rule, that policy was circumvented.
   - **Asymmetric Routing**: `c2` takes Path B while `c1` and `c3` take Path A, breaking stateful inspection on Router 2.
   - **Backup Path Congestion**: Diverting blocked traffic onto Path B increases load on `s3`.
   - **Switch TCAM Overhead**: Microflow rules consume hardware flow table capacity.
   - **Loss of Redundancy**: `c2` is now on its backup path; if Path B link breaks, `c2` has zero remaining paths.

Type `reroutes` or `issues` at any router console or Mininet prompt to inspect:
```
Router-2(s2)# reroutes
Router-2(s2)# issues
```

Unblock `10.0.0.2` to restore default Fast-Failover group routing:
```
Router-2(s2)# allow 10.0.0.2
```

### Scenario B: Ingress Edge Router blocks a client (Router 1 / s1 blocks c2)

```
Router-1(s1)# block 10.0.0.2
```

**What happens:**
- Switch s1 is the physical access gateway directly connected to `c2`.
- The controller detects the block and recognizes that **re-routing is physically impossible** (first-hop ingress isolation).
- Client `c2` turns to `FAILED` while `c1` and `c3` stay `OK`.
- The controller logs: `IMPOSSIBLE: INGRESS EDGE ISOLATION`.

Unblock `c2`:
```
Router-1(s1)# allow 10.0.0.2
```


## 5. Main demo: cut a link and watch it heal

Start a fast ping so you can measure the gap. `-W 1` makes ping finish one second after the last packet even when replies are lost:

```
mininet> c1 ping -i 0.01 -c 1000 -W 1 10.0.0.100 > /tmp/failover.txt &
```

That is 100 pings per second for about 10 seconds. Each missing reply is about 10 ms of outage. About a second later:

```
mininet> link s1 s2 down
```

What happens: s1's group sees port 4 go down and instantly uses port 5 (path B); the controller prints `LINK DOWN` and `preferred path: B (via s3)`; it re-orders s4's group too, so replies return the same way. Terminal 1 shows something like:

```
LINK DOWN  s1 port 4
preferred path: B (via s3)
LINK DOWN  s2 port 1
preferred path: B (via s3)
```

In Wireshark, traffic moves from `s1-eth4` to `s1-eth5`. Client logs stay `OK`, possibly with one or two `FAILED` lines at the instant of the cut.

Read the result after the ping ends (about 12 seconds from the start):

```
mininet> sh tail -4 /tmp/failover.txt
```

`1000 packets transmitted, N received`: lost packets times 10 ms is the approximate recovery time. Run it a few times and note the typical value.

Bring the link back; traffic returns to path A by itself:

```
mininet> link s1 s2 up
```

Repeat with the far half of path A:

```
mininet> link s2 s4 down
mininet> link s2 s4 up
```

Here s4 notices the failure directly but s1 does not, so the controller must tell s1. This is the test that shows why the controller is needed on top of the switch's own failover.

## 6. Before and after (for the graph)

"After" is section 5. For "before", force s1 onto path A only so there is no self-healing:

```bash
sudo ovs-ofctl -O OpenFlow13 add-flow s1 "priority=50,ip,nw_dst=10.0.0.100,actions=output:4"
```

Repeat section 5 with `link s1 s2 down`. The ping now fails until you bring the link back up, so losses are large. Remove the forcing rule afterwards:

```bash
sudo ovs-ofctl -O OpenFlow13 --strict del-flows s1 "priority=50,ip,nw_dst=10.0.0.100"
```

Write down lost packets for both cases (times 10 ms), do each three times, average, and plot two bars with Matplotlib (`sudo apt install python3-matplotlib`).

## 7. The link is fine but the server crashed

The second failure type from the report:

```
mininet> srv pkill -f nettwin.server
mininet> c1 ping -c 3 10.0.0.100
mininet> c1 curl -s -m 2 http://10.0.0.100:5000/health
```

Ping still works (the network is healthy) but curl gets no answer and client logs turn `FAILED`. A link monitor misses this; it is the gap an application-level health check would close. Restart the server with the command from section 2.

## 8. Shutting down

```
mininet> exit
```

Press Ctrl+C in Terminal 1, then:

```bash
bash scripts/cleanup.sh
```

Cleanup removes leftover virtual links and switches. It also kills any running `ryu-manager` and deletes `/tmp/*.log`, so run it **after** stopping everything and **before** the next start.

---

## Troubleshooting

| Problem | Likely cause and fix |
|---|---|
| Terminal 1 never prints `switch s1 connected` | Ryu was not running, or Mininet started first. Stop both, run `bash scripts/cleanup.sh`, start Ryu, then Mininet. |
| `pingall` shows 100% dropped | Ryu not connected or on another port. The topology expects Ryu on `127.0.0.1:6653`. |
| `pingall` works but `curl` returns nothing | The server needs a few seconds. Check `cat /tmp/server.log`. |
| Ping works but curl always hangs | Corrupt TCP checksums on the virtual links (mostly with `--datapath user`). Inside the hosts: `mininet> c1 ethtool -K c1-eth0 tx off rx off`, same for `c2`, `c3` (`c2-eth0`, `c3-eth0`) and `srv` (`srv-eth0`). Run it on the host interfaces, not the switch ones. |
| `ryu-manager: command not found` | Use `bash scripts/run_controller.sh`; it activates the environment. |
| `ImportError: ALREADY_HANDLED` or `is_timeout` from Ryu | Wrong eventlet or Python version. Delete `.ryu-env/` and run `bash scripts/setup_kali.sh` again. |
| `RTNETLINK answers: File exists` starting Mininet | A previous run was not cleaned. `bash scripts/cleanup.sh`. |
| After `link s1 s2 down` everything stops | Check Terminal 1 for `LINK DOWN` lines. If missing, Ryu gets no port events: restart Ryu and Mininet in order. |
| Wireshark lists no `s1-eth` interfaces | Mininet is not running, or Wireshark started first. Start Mininet, then reopen Wireshark. |
| `Permission denied` on a script | Run it with `bash scripts/<name>.sh`. |

## Cheat sheet

| What | Command |
|---|---|
| Start Ryu | `bash scripts/run_controller.sh` |
| Start network | `bash scripts/run_network.sh` |
| Test connectivity | `mininet> pingall` |
| Start server | `mininet> srv python3 -m nettwin.server.app > /tmp/server.log 2>&1 &` |
| Start a client | `mininet> c1 ./scripts/client.sh c1 > /tmp/c1.log 2>&1 &` |
| Cut / restore primary path | `mininet> link s1 s2 down` / `up` |
| Deny c2 | `sudo ovs-ofctl -O OpenFlow13 add-flow s1 "priority=100,ip,nw_src=10.0.0.2,nw_dst=10.0.0.100,actions=drop"` |
| Allow c2 | `sudo ovs-ofctl -O OpenFlow13 --strict del-flows s1 "priority=100,ip,nw_src=10.0.0.2,nw_dst=10.0.0.100"` |
| Show rules / groups / group use | `sudo ovs-ofctl -O OpenFlow13 dump-flows s1` / `dump-groups s1` / `dump-group-stats s1` |
| Clean up | `bash scripts/cleanup.sh` |

## How this matches the project report

| Report topic | Where you see it |
|---|---|
| Virtual campus network with a backup path | `nettwin/netmodel.py`, `nettwin/network/builder.py` |
| Ryu controller watching the network | `nettwin/controller/app.py` (`on_port_status`) |
| OpenFlow as the southbound API | Every rule and group in `nettwin/controller/programmer.py` |
| Fast Failover Groups | `add_failover_group` / `update_failover_group`, shown with `dump-groups` |
| Fault detection (link down) | `LINK DOWN` lines in the controller log |
| Server health (server crash, link fine) | Section 7 |
| Demonstration with Wireshark | Section 3 and section 5 |
| Before and after recovery graph | Section 6 |
## Application health telemetry
The controller probes the server's `/health` endpoint every two seconds. A healthy response is logged with latency. If the network paths remain healthy but the Flask process is stopped, the controller logs `SERVER_UNRESPONSIVE`. If the paths fail, it logs `NETWORK_PATH_UNAVAILABLE` or `NETWORK_AND_SERVER_UNAVAILABLE`. The topology contains one server, so this detects and classifies the failure but does not replace the server.
