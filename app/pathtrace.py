#!/usr/bin/env python3
"""
Trace the path a packet takes through a running lab, from the routers' own
forwarding tables - a control-plane walk, hop by hop:

  1. At the current node, look the target up in the right table (a VRF or the
     global table): IOS-XE `show ip cef [vrf X] A.B.C.D detail`, IOS-XR
     `show cef [vrf X] A.B.C.D`, NX-OS `show ip route A.B.C.D [vrf X]`,
     FRR `show ip route [vrf X] A.B.C.D json`, Linux `ip route get`.
  2. The result gives next hop(s), outgoing interface and the labels imposed.
     The interface is mapped back to the clab port, the port to the link, the
     link to the neighbour - that is the next hop on the map.
  3. A VPN route resolves recursively to a BGP next hop (the remote PE's
     loopback) and carries a VPN label; from there the walk follows that
     next hop through the core (global table) with the VPN label kept at the
     bottom of the stack, and switches back into the VRF of the same name on
     the node that owns the next hop (the egress PE).
  4. Plain IP arriving on a PE is looked up in the VRF of the incoming port.
  5. It stops when a node owns the destination, when a router has no route, or
     after MAX_HOPS.

Equal-cost next hops are all reported; the walk follows the first one. SR
Linux is not walked (its FIB is not exposed in a form parsed here) - the
trace stops there and says so.

Router sessions are borrowed from the lab's live-state poller (livestate.py),
so a trace over VMs costs a few `show` commands, not new SSH logins.
"""

import ipaddress
import json
import re
import time

import devcfg
import livestate
import linkctl

MAX_HOPS = 24
IP_RE = r"\d+\.\d+\.\d+\.\d+"
ADDR_RE = r"(?:\d+\.\d+\.\d+\.\d+|[0-9a-fA-F:]*:[0-9a-fA-F:.]+)"


class TraceError(Exception):
    pass


# --------------------------------------------------------------------------
# per-platform lookups -> {"paths": [{nh, out_if, labels}], "via": bgp nh|None,
#                          "vpn_label": int|None, "connected": bool, "raw": text}
# labels: list of ints/strings as imposed on the wire; "pop" means PHP
# --------------------------------------------------------------------------

def _lab_int(x):
    x = str(x).strip()
    if x in ("3", "implicit-null", "ImplNull", "imp-null", "Pop", "pop"):
        return "pop"
    if x in ("None", "none", "no-label", "No", ""):
        return None
    return int(x) if x.isdigit() else x


def parse_xe_cef(text):
    res = {"paths": [], "via": None, "vpn_label": None, "connected": False}
    if re.search(r"attached to|receive for|, attached", text):
        res["connected"] = True
    m = re.search(r"recursive via (%s)(?: label (\S+))?" % IP_RE, text)
    if m:
        res["via"] = m.group(1)
        res["vpn_label"] = _lab_int(m.group(2)) if m.group(2) else None
    for m in re.finditer(r"^[ \t]*nexthop (%s) (\S+)(?: label (\S+))?(?:[ \t]+(\S+))?" % IP_RE, text, re.M):
        labels = []
        if m.group(3):
            # "16004-(local:16004)", "[16003|16003]-(local:16003)", "implicit-null"
            lab = re.sub(r"-\(local:.*", "", m.group(3)).strip("[]").split("|")[0]
            labels.append(_lab_int(lab))
        if m.group(4) and re.match(r"^\d+$", m.group(4)):
            labels.append(int(m.group(4)))
        elif res["vpn_label"] is not None:
            labels.append(res["vpn_label"])
        res["paths"].append({"nh": m.group(1), "out_if": m.group(2), "labels": [l for l in labels if l is not None]})
    if not res["paths"]:
        m = re.search(r"^\s*attached to (\S+)", text, re.M)
        if m:
            res["paths"].append({"nh": None, "out_if": m.group(1), "labels": []})
            res["connected"] = True
    return res


def parse_xr_cef(text):
    res = {"paths": [], "via": None, "vpn_label": None, "connected": False}
    if "Prefix not found" in text:
        return res
    m = re.search(r"SRv6 H\.(?:Encaps|Insert)\S*\s+SID-list \{([^}]*)\}", text)
    if m:
        res["srv6_sids"] = [x.strip() for x in m.group(1).replace(",", " ").split() if x.strip()]
    if re.search(r"attached|receive", text.split("\n", 3)[-1][:600]) and "via" not in text:
        res["connected"] = True
    m = re.search(r"via (%s)/32, \d+ dependencies, recursive" % IP_RE, text)
    if m and "bgp-ext" not in text[m.start():m.start() + 120]:
        res["via"] = m.group(1)
    for m in re.finditer(r"^\s*next hop (%s)/\d+ (\S+)\s+labels imposed \{([^}]*)\}" % IP_RE, text, re.M):
        labels = [_lab_int(x) for x in m.group(3).split()]
        res["paths"].append({"nh": m.group(1), "out_if": m.group(2), "labels": [l for l in labels if l is not None]})
    if not res["paths"]:
        # IGP route: "via 10.10.0.5/32, GigabitEthernet0/0/0/0, ... labels imposed {16001}"
        for m in re.finditer(r"^\s*via (%s)/\d+, (\S+?),.*?(?:labels imposed \{([^}]*)\})?" % IP_RE, text, re.M | re.S):
            blk = text[m.start():m.start() + 400]
            lm = re.search(r"labels imposed \{([^}]*)\}", blk)
            labels = [_lab_int(x) for x in lm.group(1).split()] if lm else []
            res["paths"].append({"nh": m.group(1), "out_if": m.group(2), "labels": [l for l in labels if l is not None]})
    if not res["paths"]:
        # a route whose next hop is directly reachable (a CE learned over eBGP):
        # only the load-distribution table names the interface
        for m in re.finditer(r"^\s*\d+\s+Y\s+(\S+)\s+(%s)" % ADDR_RE, text, re.M):
            if m.group(1) != "recursive":
                res["paths"].append({"nh": m.group(2), "out_if": m.group(1), "labels": []})
    if res["via"] and res["paths"]:
        bottom = [p["labels"][-1] for p in res["paths"] if len(p["labels"]) > 1]
        res["vpn_label"] = bottom[0] if bottom else None
    if not res["paths"] and re.search(r"attached|local adjacency", text):
        res["connected"] = True
    return res


def parse_nxos_route(text):
    res = {"paths": [], "via": None, "vpn_label": None, "connected": False}
    for m in re.finditer(r"\*via (%s), (\S+?),\s*\[\d+/\d+\].*?,\s*(\S+)" % IP_RE, text):
        if m.group(3).startswith(("direct", "local")):
            res["connected"] = True
        res["paths"].append({"nh": m.group(1), "out_if": m.group(2), "labels": []})
    return res


def parse_frr_route(js):
    res = {"paths": [], "via": None, "vpn_label": None, "connected": False}
    try:
        d = json.loads(js[js.index("{"):]) if "{" in js else {}
    except ValueError:
        return res
    routes = [r for lst in d.values() if isinstance(lst, list) for r in lst if r.get("selected")] \
        or [r for lst in d.values() if isinstance(lst, list) for r in lst]
    if not routes:
        return res
    r = routes[0]
    if r.get("protocol") in ("connected", "local"):
        res["connected"] = True
    for nh in r.get("nexthops") or []:
        if nh.get("recursive"):
            res["via"] = nh.get("ip")
            if nh.get("labels"):
                res["vpn_label"] = nh["labels"][-1]
            continue
        if not nh.get("fib") and not nh.get("active"):
            continue
        labels = [_lab_int(x) for x in nh.get("labels") or []]
        if nh.get("resolver") or res["via"]:
            if res["vpn_label"] is not None and (not labels or labels[-1] != res["vpn_label"]):
                labels.append(res["vpn_label"])
        if nh.get("directlyConnected") and not nh.get("ip"):
            res["connected"] = True
        res["paths"].append({"nh": nh.get("ip"), "out_if": nh.get("interfaceName"),
                             "labels": [l for l in labels if l is not None]})
    return res


# --------------------------------------------------------------------------
# the walk
# --------------------------------------------------------------------------

class Tracer:
    def __init__(self, lab, poller):
        self.lab = lab
        self.poller = poller
        self.nodes = {n["name"]: n for n in lab.get("nodes") or []}
        self.cont = {c["short"]: c for c in lab.get("containers") or [] if c.get("state") == "running"}
        self.ips = {}               # node -> set of its IPv4 addresses
        self.ifvrf = {}             # node -> {platform ifname: vrf}
        self.locators = {}          # node -> [ipaddress.IPv6Network] (SRv6)
        self.cmds = 0

    def platform(self, node):
        n = self.nodes.get(node) or {}
        p = devcfg.platform_of(n.get("kind"), n.get("image"))
        if p is None and n.get("kind") == "linux":
            return "linux"
        return p

    def run(self, node, cmd):
        self.cmds += 1
        return self.poller.command(self.lab, node, self.cont[node], self.platform(node), cmd)

    def nsrun(self, node, argv):
        pid = linkctl._pid(self.cont[node]["name"])
        rc, out, err = linkctl._run([linkctl.NSENTER, "-t", str(pid), "-n"] + argv, timeout=20)
        return out

    # which addresses a node owns (poller cache first)
    def owned(self, node):
        if node in self.ips:
            return self.ips[node]
        cached = (self.poller.proto.get(node) or {}).get("ips")
        if cached:
            self.ips[node] = set(cached)
            return self.ips[node]
        plat = self.platform(node)
        ips = set()
        try:
            if plat in ("linux", "frr"):
                ips = set(re.findall(r"inet (%s)/" % IP_RE, self.nsrun(node, [linkctl.IP, "-4", "-o", "addr"])))
                # FRR VRF addresses are in the same netns, ip addr lists them too
            elif plat == "cisco_xe":
                # Gi1 is vrnetlab's internal management NAT (10.0.0.15) - not a lab address
                out = self.run(node, "show ip interface brief")
                ips = set(livestate.parse_ips_ios("\n".join(l for l in out.splitlines()
                                                             if not l.startswith("GigabitEthernet1 "))))
            elif plat == "cisco_xr":
                ips = set(livestate.parse_ips_ios(self.run(node, "show ipv4 vrf all interface brief")))
            elif plat == "cisco_nxos":
                ips = set(livestate.parse_ips_ios(self.run(node, "show ip interface brief vrf all")))
        except Exception:                                  # noqa: BLE001
            pass
        ips.discard("127.0.0.1")
        self.ips[node] = ips
        return ips

    def srv6_locators(self, node):
        if node not in self.locators:
            nets = []
            if self.platform(node) == "cisco_xr":
                try:
                    out = self.run(node, "show segment-routing srv6 locator")
                    for pfx in re.findall(r"\s([0-9a-fA-F:]+/\d+)\s", out):
                        try:
                            nets.append(ipaddress.ip_network(pfx, strict=False))
                        except ValueError:
                            pass
                except Exception:                              # noqa: BLE001
                    pass
            self.locators[node] = nets
        return self.locators[node]

    def owns_sid(self, node, addr):
        try:
            a = ipaddress.ip_address(addr)
        except ValueError:
            return False
        return any(a in n for n in self.srv6_locators(node) if n.version == a.version)

    def owner_of(self, ip):
        for node in self.cont:
            if ip in self.owned(node):
                return node
        return None

    def vrf_of(self, node, ifname):
        """VRF of an interface (None = global)."""
        plat = self.platform(node)
        try:
            if plat in ("linux", "frr"):
                out = self.nsrun(node, [linkctl.IP, "-j", "-d", "link", "show", ifname])
                d = json.loads(out or "[]")
                master = d[0].get("master") if d else None
                if master:
                    info = self.nsrun(node, [linkctl.IP, "-j", "-d", "link", "show", master])
                    if '"vrf"' in info:
                        return master
                return None
            if plat == "cisco_xe":
                out = self.run(node, "show ip interface %s | include VPN Routing" % ifname)
                m = re.search(r'VPN Routing/Forwarding "([^"]+)"', out)
                return m.group(1) if m else None
            if plat == "cisco_xr":
                if node not in self.ifvrf:
                    out = self.run(node, "show ipv4 vrf all interface brief")
                    m = {}
                    for line in out.splitlines():
                        f = line.split()
                        if len(f) >= 5 and re.match(r"^[A-Z][A-Za-z-]+\d", f[0]):
                            m[f[0]] = f[-1]
                    self.ifvrf[node] = m
                v = self.ifvrf[node].get(ifname)
                return None if v in (None, "default") else v
            if plat == "cisco_nxos":
                out = self.run(node, "show vrf interface %s" % ifname)
                m = re.search(r"^%s\s+(\S+)" % re.escape(ifname), out, re.M | re.I)
                return None if not m or m.group(1) == "default" else m.group(1)
        except Exception:                                  # noqa: BLE001
            return None
        return None

    def lookup(self, node, vrf, target):
        plat = self.platform(node)
        if plat in ("linux",):
            argv = [linkctl.IP, "-j"] + (["-6"] if ":" in target else []) + ["route", "get"] \
                + (["vrf", vrf] if vrf else []) + [target]
            try:
                d = json.loads(self.nsrun(node, argv) or "[]")
            except ValueError:
                d = []
            if not d:
                return {"paths": [], "via": None, "vpn_label": None, "connected": False}
            r = d[0]
            return {"paths": [{"nh": r.get("gateway"), "out_if": r.get("dev"), "labels": []}],
                    "via": None, "vpn_label": None, "connected": r.get("type") == "local" or not r.get("gateway")}
        v6 = ":" in target
        if plat == "frr":
            cmd = "show %s route %s%s json" % ("ipv6" if v6 else "ip", ("vrf %s " % vrf) if vrf else "", target)
            rc, out = devcfg.ExecConn(self.cont[node]["name"], "frr").exec(["vtysh", "-c", cmd], timeout=20)
            self.cmds += 1
            return parse_frr_route(out)
        if plat == "cisco_xe":
            return parse_xe_cef(self.run(node, "show %s cef %s%s detail" % ("ipv6" if v6 else "ip",
                                                                         ("vrf %s " % vrf) if vrf else "", target)))
        if plat == "cisco_xr":
            return parse_xr_cef(self.run(node, "show cef %s%s%s" % (("vrf %s " % vrf) if vrf else "",
                                                                   "ipv6 " if v6 else "", target)))
        if plat == "cisco_nxos":
            return parse_nxos_route(self.run(node, "show ip route %s%s" % (target, (" vrf %s" % vrf) if vrf else "")))
        raise TraceError("%s (%s) cannot be traced through" % (node, self.nodes.get(node, {}).get("kind")))

    def link_out(self, node, out_if):
        """(link, far node, far port) for an outgoing platform interface."""
        plat = self.platform(node)
        port = out_if if plat in ("linux", "frr") else livestate.to_port(plat, out_if)
        for l in self.lab.get("links") or []:
            if l["a"] == node and l["a_if"] == port:
                return l, l["b"], l["b_if"]
            if l["b"] == node and l["b_if"] == port:
                return l, l["a"], l["a_if"]
        return None, None, None

    def trace(self, src, dst, src_vrf=None):
        if src not in self.cont:
            raise TraceError("%s is not running" % src)
        hops, node, vrf = [], src, src_vrf
        target, inner, vpn_vrf, stack = dst, dst, None, []
        srv6 = False
        in_port = None
        t0 = time.time()
        for _ in range(MAX_HOPS):
            hop = {"node": node, "in_port": in_port, "vrf": vrf, "lookup": target, "in_labels": list(stack)}
            hops.append(hop)
            owns = target in self.owned(node)
            if srv6 and self.owns_sid(node, target):
                # the SID belongs to this node's locator: decapsulate into the VRF
                hop["action"] = "SRv6 decap %s → VRF %s" % (target, vpn_vrf or "?")
                vrf, target, stack, srv6 = vpn_vrf, inner, [], False
                hop2 = {"node": node, "in_port": in_port, "vrf": vrf, "lookup": target, "in_labels": []}
                hops.append(hop2)
                hop = hop2
                owns = target in self.owned(node)
            elif owns and target != inner:
                # the BGP next hop is here: this is the egress PE, back into the VRF
                hop["action"] = "pop VPN label%s → VRF %s" % (
                    (" %s" % stack[-1]) if stack else "", vpn_vrf or "?")
                vrf, target, stack = vpn_vrf, inner, []
                hop2 = {"node": node, "in_port": in_port, "vrf": vrf, "lookup": target, "in_labels": []}
                hops.append(hop2)
                hop = hop2
                owns = target in self.owned(node)
            if owns:
                hop["action"] = "delivered"
                return self._done(hops, "reached", t0)
            try:
                res = self.lookup(node, vrf, target)
            except TraceError as exc:
                hop["action"] = str(exc)
                return self._done(hops, "unsupported", t0)
            if not res["paths"]:
                hop["action"] = "no route to %s%s" % (target, (" in VRF %s" % vrf) if vrf else "")
                return self._done(hops, "no-route", t0)
            p = res["paths"][0]
            if res.get("srv6_sids") and not srv6:
                # SRv6 VPN: the packet is encapsulated towards the service SID;
                # the core forwards on that IPv6 destination (its locator)
                hop["srv6"] = res["srv6_sids"]
                vpn_vrf, target, srv6 = vrf, res["srv6_sids"][0], True
                hop["out_if"], hop["nh"] = p["out_if"], p["nh"]
                hop["out_labels"] = []
                hop["ecmp"] = [{"out_if": x["out_if"], "nh": x["nh"], "labels": []} for x in res["paths"][1:]]
                hop["action"] = "SRv6 H.Encaps.Red → %s" % " ".join(res["srv6_sids"])
                hop["stack_out"] = ["DA " + target]
                link, far, far_port = self.link_out(node, p["out_if"])
                if link is None:
                    hop["action"] += " - %s leaves the lab" % p["out_if"]
                    return self._done(hops, "left-lab", t0)
                hop["link"] = link["id"]
                hop["ecmp_links"] = [x["id"] for x in (self.link_out(node, e["out_if"])[0] for e in hop["ecmp"]) if x]
                stack = [target]
                node, in_port = far, far_port
                vrf = None
                if far not in self.cont:
                    hops.append({"node": far, "in_port": far_port, "action": "not running"})
                    return self._done(hops, "down", t0)
                continue
            if res["via"] and res["via"] != target and not stack:
                # VPN (or recursive BGP) route: follow the BGP next hop from here on
                hop["via"] = res["via"]
                vpn_vrf, target = vrf, res["via"]
            hop["out_if"], hop["nh"] = p["out_if"], p["nh"]
            hop["out_labels"] = p["labels"]
            hop["ecmp"] = [{"out_if": x["out_if"], "nh": x["nh"], "labels": x["labels"]} for x in res["paths"][1:]]
            hop["action"] = ("forward on SRv6 DA %s" % target) if srv6 else _describe(stack, p["labels"])
            link, far, far_port = self.link_out(node, p["out_if"])
            if link is None:
                if res["connected"] and target == inner:
                    hop["action"] = "delivered on a directly connected network"
                    return self._done(hops, "reached", t0)
                hop["action"] += " - %s leaves the lab" % p["out_if"]
                return self._done(hops, "left-lab", t0)
            hop["link"] = link["id"]
            hop["ecmp_links"] = [x["id"] for x in (self.link_out(node, e["out_if"])[0] for e in hop["ecmp"]) if x]
            out = [l for l in p["labels"] if l != "pop"]
            # a label-switching router only swaps / pops the top label; what is
            # below it (the VPN label) travels on unchanged
            if srv6:
                hop["stack_out"] = ["DA " + target]
            else:
                stack = (out[:1] + stack[1:]) if stack else out
                hop["stack_out"] = list(stack)
            node, in_port = far, far_port
            if far not in self.cont:
                hops.append({"node": far, "in_port": far_port, "action": "not running"})
                return self._done(hops, "down", t0)
            # plain IP arriving on a router is looked up in its incoming port's VRF
            vrf = None if stack else self.vrf_of(far, self._platform_if(far, far_port))
            if not stack and target != inner:
                target = inner          # PHP popped the transport and there is no VPN label
        return self._done(hops, "loop", t0)

    def _platform_if(self, node, port):
        import topoedit
        plat = self.platform(node)
        if plat in ("linux", "frr"):
            return port
        return topoedit.iface_name(self.nodes[node]["kind"], topoedit.port_num(port))

    def _done(self, hops, result, t0):
        return {"hops": hops, "result": result, "seconds": round(time.time() - t0, 2), "commands": self.cmds}


def _describe(stack, out):
    out = [l for l in out if l is not None]
    if not stack and not out:
        return "forward"
    if not stack:
        return "push " + " ".join(str(l) for l in out if l != "pop") if any(l != "pop" for l in out) else "forward"
    top_out = out[0] if out else None
    if top_out == "pop" or top_out is None:
        return "pop %s (PHP)" % stack[0]
    if top_out == stack[0]:
        return "swap %s → %s" % (stack[0], top_out)
    return "swap %s → %s" % (stack[0], top_out)


def trace(lab, poller, src, dst, src_vrf=None):
    try:
        ipaddress.ip_address(dst)
    except ValueError:
        raise TraceError("destination must be an IPv4 address")
    return Tracer(lab, poller).trace(src, dst, src_vrf)


def node_address(lab, poller, node):
    """A sensible address for a node: its loopback, else its first data address."""
    t = Tracer(lab, poller)
    if node in t.cont:
        plat = t.platform(node)
        try:
            if plat in ("linux", "frr"):
                lo = re.findall(r"inet (%s)/" % IP_RE, t.nsrun(node, [linkctl.IP, "-4", "-o", "addr", "show", "dev", "lo"]))
                lo = [ip for ip in lo if not ip.startswith("127.")]
                if lo:
                    return lo[0]
            elif plat in ("cisco_xe", "cisco_xr", "cisco_nxos"):
                cmd = {"cisco_xe": "show ip interface brief", "cisco_xr": "show ipv4 vrf all interface brief",
                       "cisco_nxos": "show ip interface brief vrf all"}[plat]
                m = re.search(r"^(?:Loopback0|Lo0|loopback0)\s+(%s)" % IP_RE, t.run(node, cmd), re.M)
                if m:
                    return m.group(1)
        except Exception:                                  # noqa: BLE001
            pass
    ips = sorted(t.owned(node), key=lambda ip: (not ip.startswith("10.0."), ip))
    mg = {c.get("ipv4") for c in lab.get("containers") or []}
    ips = [ip for ip in ips if ip not in mg and not ip.startswith("172.")]
    return ips[0] if ips else None
