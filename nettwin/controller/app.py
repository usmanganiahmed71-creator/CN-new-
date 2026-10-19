"""NetTwin Ryu application: reacts to OpenFlow events and delegates the work.

Run with:  scripts/run_controller.sh

    * netmodel         decides *what* the network looks like and which ports matter
    * LinkHealth       decides *which path* is best right now
    * PolicyReRouteManager decides *how to steer around router blocks* & risk analysis
    * SwitchProgrammer decides *how* to say it in OpenFlow
This class wires those components to Ryu's OpenFlow events and background monitor.
"""
import time
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.ofproto import ofproto_v1_3

from nettwin import netmodel, policy
from nettwin.controller.failover import LinkHealth, PolicyReRouteManager
from nettwin.controller.programmer import SwitchProgrammer
from nettwin.controller.server_health import ServerHealthProbe, classify_fault


class NetTwin(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._programmers = {}  # switch name -> SwitchProgrammer
        self._health = LinkHealth(
            {name: netmodel.path_ports(name) for name in netmodel.PATHS})
        self._policy_mgr = PolicyReRouteManager(target_ip="10.0.0.100")
        self._active_decisions = {}     # src_ip -> PolicyDecision
        self._installed_bypasses = set()  # set of src_ip currently having bypass rules
        self._last_block_signature = None
        self._server_probe = ServerHealthProbe(
            "http://%s:%d/health" % (netmodel.HOSTS[netmodel.SERVER], netmodel.SERVER_PORT))
        self._last_fault = None

        # Start the policy and application health monitor threads.
        self._policy_thread = hub.spawn(self._policy_monitor_loop)
        self._server_monitor = hub.spawn(self._monitor_server)

    def _monitor_server(self):
        """Log application health independently from OpenFlow link health."""
        while True:
            result = self._server_probe.check()
            network_healthy = any(
                self._health.is_path_healthy(path) for path in netmodel.PATHS)
            fault = classify_fault(result.healthy, network_healthy)
            if fault != self._last_fault:
                if result.latency_ms is None:
                    self.logger.warning("server health: %s", fault)
                else:
                    self.logger.info("server health: %s (%.1f ms)",
                                     fault, result.latency_ms)
                self._last_fault = fault
            hub.sleep(2)

    # ------------------------------------------------ a switch connects
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def on_switch_connected(self, ev):
        datapath = ev.msg.datapath
        try:
            switch = netmodel.switch_name(datapath.id)
        except KeyError:
            self.logger.warning("ignoring unknown switch dpid=%s", datapath.id)
            return

        programmer = SwitchProgrammer(datapath)
        self._programmers[switch] = programmer
        self._health.forget_switch(datapath.id)  # drop state from an earlier run
        self.logger.info("switch %s connected", switch)

        programmer.reset()
        self._install_forwarding(switch, programmer)
        self._check_policy_and_reroute(force=True)

    def _install_forwarding(self, switch, programmer):
        """Proactive rules: installed up front so no packet waits for Ryu."""
        if netmodel.is_edge_switch(switch):
            programmer.add_failover_group(self._uplink_ports(switch))
        for ip, port in netmodel.direct_routes(switch).items():
            programmer.route_to_port(ip, port)
        for ip in netmodel.failover_destinations(switch):
            programmer.route_to_group(ip)

    # ------------------------------------------------ a link goes up/down
    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def on_port_status(self, ev):
        msg = ev.msg
        dpid, port = msg.datapath.id, msg.desc.port_no
        is_down = bool(msg.desc.state & msg.datapath.ofproto.OFPPS_LINK_DOWN)

        self._health.set_link_down(dpid, port, is_down)
        self.logger.info("%s s%d port %d", "LINK DOWN " if is_down else "LINK UP   ",
                         dpid, port)
        self._refresh_failover_groups()
        self._check_policy_and_reroute(force=True)

    def _refresh_failover_groups(self):
        """Keep both edge switches agreeing on the best path.

        A switch only notices its own dead port; the far edge switch would keep
        sending replies into the break. Re-ordering both groups fixes that.
        """
        for edge in netmodel.edge_switches():
            programmer = self._programmers.get(edge)
            if programmer is not None:
                programmer.update_failover_group(self._uplink_ports(edge))
        best = self._health.ranked_paths()[0]
        self.logger.info("preferred path: %s (via %s)", best,
                         ", ".join(netmodel.PATHS[best][1:-1]))

    def _uplink_ports(self, edge):
        return [netmodel.uplink_port(edge, path) for path in self._health.ranked_paths()]

    # ------------------------------------------------ flow stats audit
    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def on_flow_stats_reply(self, ev):
        """Audit drop flows on switches to catch raw ovs-ofctl blocks."""
        datapath = ev.msg.datapath
        try:
            switch = netmodel.switch_name(datapath.id)
        except KeyError:
            return

        for stat in ev.msg.body:
            # Check for priority 100 drop rules
            if stat.priority == 100:
                is_drop = False
                if not stat.instructions:
                    is_drop = True
                else:
                    for inst in stat.instructions:
                        if hasattr(inst, "actions") and not inst.actions:
                            is_drop = True
                if is_drop:
                    ipv4_src = stat.match.get("ipv4_src")
                    if ipv4_src:
                        policy.add_block(switch, ipv4_src, stat.match.get("ipv4_dst", "10.0.0.100"))

    # ------------------------------------------------ policy monitor & rerouting
    def _policy_monitor_loop(self):
        poll_count = 0
        while True:
            hub.sleep(0.5)
            poll_count += 1
            # Periodically query flow tables from all connected switches
            if poll_count % 4 == 0:
                for sw, prog in list(self._programmers.items()):
                    try:
                        dp = prog._dp
                        req = dp.ofproto_parser.OFPFlowStatsRequest(dp)
                        dp.send_msg(req)
                    except Exception:
                        pass
            self._check_policy_and_reroute()

    def _check_policy_and_reroute(self, force=False):
        """Check policy state file and re-evaluate path selection and bypasses."""
        blocks = policy.load_policy()
        # Create a signature to detect changes
        sig = tuple(sorted((b.get("switch", ""), b.get("src_ip", "")) for b in blocks))

        if sig == self._last_block_signature and not force:
            return
        self._last_block_signature = sig

        self._policy_mgr.sync_blocks(blocks)
        new_decisions = self._policy_mgr.evaluate_all(self._health)

        # 1. Check for unblocked IPs
        old_ips = set(self._active_decisions.keys())
        new_ips = set(new_decisions.keys())
        unblocked_ips = old_ips - new_ips

        for ip in unblocked_ips:
            if ip in self._installed_bypasses:
                self._remove_bypass(ip)
                self._installed_bypasses.discard(ip)
            self._log_unblock_event(ip)

        # 2. Check for newly blocked or changed IPs
        for ip, dec in new_decisions.items():
            old_dec = self._active_decisions.get(ip)
            has_changed = (
                old_dec is None
                or old_dec.status != dec.status
                or old_dec.active_path != dec.active_path
                or force
            )
            if has_changed:
                if dec.reroute_rules:
                    self._apply_bypass(ip, dec)
                    self._installed_bypasses.add(ip)
                else:
                    if ip in self._installed_bypasses:
                        self._remove_bypass(ip)
                        self._installed_bypasses.discard(ip)
                self._log_policy_event(dec)

        self._active_decisions = new_decisions

        # 3. Save latest reroutes and issues analysis to shared file
        reroute_export = {
            ip: dec.to_dict() for ip, dec in new_decisions.items()
        }
        policy.save_reroutes({
            "reroutes": reroute_export,
            "last_evaluated": time.strftime("%H:%M:%S"),
            "preferred_core_path": self._health.ranked_paths()[0],
        })

    def _apply_bypass(self, src_ip, decision):
        """Install high-priority OpenFlow bypass flows on edge routers."""
        rules = decision.reroute_rules
        s1_rule = rules.get("s1")
        if s1_rule and "s1" in self._programmers:
            self._programmers["s1"].reroute_src_to_port(
                src_ip, decision.dst_ip, s1_rule["out_port"], priority=60)
        s4_rule = rules.get("s4")
        if s4_rule and "s4" in self._programmers:
            self._programmers["s4"].reroute_dst_to_port(
                src_ip, s4_rule["out_port"], priority=60)

    def _remove_bypass(self, src_ip, dst_ip="10.0.0.100"):
        """Remove high-priority OpenFlow bypass flows on edge routers."""
        if "s1" in self._programmers:
            self._programmers["s1"].remove_reroute_src(src_ip, dst_ip, priority=60)
        if "s4" in self._programmers:
            self._programmers["s4"].remove_reroute_dst(src_ip, priority=60)

    def _log_policy_event(self, dec):
        """Log formatted telemetry banner for a policy and re-routing event."""
        lines = []
        lines.append("\033[1;36m" + "=" * 76 + "\033[0m")
        lines.append("\033[1;33m>>> [SDN CONTROLLER TELEMETRY] ROUTER POLICY & RE-ROUTING EVENT <<<\033[0m")
        lines.append("\033[1;36m" + "-" * 76 + "\033[0m")
        lines.append(f"  \033[1;37mBlocked Target :\033[0m \033[1;33m{dec.src_ip}\033[0m -> Destination: {dec.dst_ip}")
        lines.append(f"  \033[1;37mBlocked On     :\033[0m \033[1;31m{', '.join(dec.blocked_on)}\033[0m")
        status_color = "\033[1;32m" if "RE-ROUTED" in dec.status else "\033[1;31m"
        lines.append(f"  \033[1;37mAction Status  :\033[0m {status_color}{dec.status}\033[0m")
        lines.append(f"  \033[1;37mActive Path    :\033[0m Path {dec.active_path}")
        lines.append(f"  \033[1;37mForwarding Path:\033[0m \033[1;34m{dec.flow_path}\033[0m")
        lines.append("\033[1;36m" + "-" * 76 + "\033[0m")
        lines.append("  \033[1;31m[!] POSSIBLE ISSUES & RISK ASSESSMENT:\033[0m")
        for idx, issue in enumerate(dec.issues, 1):
            lines.append(f"    \033[1;31m{idx}. {issue['title']}\033[0m \033[0;37m[{issue['category']}]\033[0m")
            lines.append(f"       \033[0;37m{issue['desc']}\033[0m")
        lines.append("\033[1;36m" + "=" * 76 + "\033[0m")
        msg = "\n" + "\n".join(lines)
        self.logger.info(msg)

    def _log_unblock_event(self, src_ip):
        """Log formatted telemetry banner when an IP block is removed."""
        lines = []
        lines.append("\033[1;32m" + "=" * 76 + "\033[0m")
        lines.append("\033[1;32m>>> [SDN CONTROLLER TELEMETRY] POLICY UNBLOCKED: RESTORING PATH <<<\033[0m")
        lines.append("\033[1;32m" + "-" * 76 + "\033[0m")
        lines.append(f"  \033[1;37mUnblocked Host :\033[0m \033[1;33m{src_ip}\033[0m")
        lines.append("  \033[1;37mAction         :\033[0m Removed bypass microflow overrides on s1 and s4")
        lines.append("  \033[1;37mStatus         :\033[0m \033[1;32mNormal Fast-Failover group forwarding restored\033[0m")
        lines.append("  \033[1;37mRisk Status    :\033[0m All policy evasion and path asymmetry risks cleared")
        lines.append("\033[1;32m" + "=" * 76 + "\033[0m")
        msg = "\n" + "\n".join(lines)
        self.logger.info(msg)
