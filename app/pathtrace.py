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
MAX_BRANCHES = 8            # equal-cost paths followed per trace
MAX_LOOKUPS = 120
IP_RE = r"\d+\.\d+\.\d+\.\d+"
ADDR_RE = r"(?:\d+\.\d+\.\d+\.\d+|[0-9a-fA-F:]*:[0-9a-fA-F:.]+)"


class TraceError(Exception):
    pass


def norm_ip(ip):
    """Canonical text form (IPv6 lower-case, compressed); other text unchanged."""
    try:
        return str(ipaddress.ip_address(ip))
    except ValueError:
        return ip


parse_ips6 = livestate.parse_ips6


def _loopback_block(text):
    """The lines of a `show ... interface brief` listing that belong to
    Loopback0 (IOS family IPv6 output puts the addresses on the lines below)."""
    lines = text.splitlines()
    for i, l in enumerate(lines):
        if re.match(r"(?:Loopback0|Lo0|loopback0|lo0)\b", l, re.I):
            block = [l]
            for nxt in lines[i + 1:]:
                if nxt[:1] not in (" ", "\t"):
                    break
                block.append(nxt)
            return "\n".join(block)
    return ""


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


def _split_backups(res):
    """Move repair (LFA / TI-LFA) next hops out of the forwarding paths."""
    res["backups"] = [p for p in res["paths"] if p.get("backup")]
    res["paths"] = [p for p in res["paths"] if not p.get("backup")]
    return res


def parse_xe_cef(text):
    res = {"paths": [], "via": None, "vpn_label": None, "connected": False}
    if re.search(r"attached to|receive for|, attached", text):
        res["connected"] = True
    m = re.search(r"recursive via (%s)(?: label (\S+))?" % IP_RE, text)
    if m:
        res["via"] = m.group(1)
        res["vpn_label"] = _lab_int(m.group(2)) if m.group(2) else None
    # "  nexthop 10.1.34.2 GigabitEthernet3 label 16002-(local:16002), repair" is
    # the LFA / TI-LFA backup of the path above it, not an equal-cost path
    for m in re.finditer(r"^[ \t]*nexthop (%s) (\S+)(?: label (\S+))?(?:[ \t]+(\S+))?" % IP_RE, text, re.M):
        line = text[m.start():text.find("\n", m.start()) if "\n" in text[m.start():] else len(text)]
        backup = bool(re.search(r",\s*repair\s*$", line))
        labels = []
        if m.group(3):
            # "16004-(local:16004)", "[16003|16003]-(local:16003)", "implicit-null"
            lab = re.sub(r"-\(local:.*", "", m.group(3)).strip("[]").split("|")[0]
            labels.append(_lab_int(lab))
        if m.group(4) and re.match(r"^\d+$", m.group(4)):
            labels.append(int(m.group(4)))
        elif res["vpn_label"] is not None:
            labels.append(res["vpn_label"])
        p = {"nh": m.group(1), "out_if": m.group(2), "labels": [l for l in labels if l is not None]}
        if backup:
            p.update(backup=True, kind="TI-LFA" if "TI-LFA" in line or "tunnel" in line.lower() else "repair")
        res["paths"].append(p)
    if not res["paths"]:
        m = re.search(r"^\s*attached to (\S+)", text, re.M)
        if m:
            res["paths"].append({"nh": None, "out_if": m.group(1), "labels": []})
            res["connected"] = True
    return _split_backups(res)


_XR_BACKUP = re.compile(r"(?<!-)\bbackup(?: \(([^)]*)\))?")


def _xr_via_line(text, pos):
    """The `via ...` line a position in `show cef` output belongs to."""
    start = None
    for m in re.finditer(r"^[ \t]*via .*$", text, re.M):
        if m.start() > pos:
            break
        start = m
    return start.group(0) if start else ""


def parse_xr_cef(text):
    res = {"paths": [], "via": None, "vpn_label": None, "connected": False}
    if "Prefix not found" in text:
        return res
    for m in re.finditer(r"SRv6 H\.(?:Encaps|Insert)\S*\s+SID-list \{([^}]*)\}", text):
        # a TI-LFA repair path carries its own SID list - that is not the service encap
        if not _XR_BACKUP.search(_xr_via_line(text, m.start())):
            res["srv6_sids"] = [x.strip() for x in m.group(1).replace(",", " ").split() if x.strip()]
            break
    if re.search(r"attached|receive", text.split("\n", 3)[-1][:600]) and "via" not in text:
        res["connected"] = True
    m = re.search(r"via (%s)/32, \d+ dependencies, recursive" % IP_RE, text)
    if m and "bgp-ext" not in text[m.start():m.start() + 120]:
        res["via"] = m.group(1)
    m = re.search(r"^\s*Updated (\w{3} +\d+ [\d:.]+)", text, re.M)
    if m:
        res["updated"] = m.group(1)

    def mark(p, via_line):
        # "via 10.1.13.2/32, TenGigE0/0/0/1, ..., backup (Local-LFA) [flags 0x300]"
        # "..., protected, ECMP-backup (Local-LFA)": an equal-cost path that is
        # also the repair of the others - it forwards, so it is not a backup
        bm = _XR_BACKUP.search(via_line)
        if bm:
            p.update(backup=True, kind=bm.group(1) or "backup")
        elif "protected" in via_line:
            p["protected"] = True
            em = re.search(r"ECMP-backup(?: \(([^)]*)\))?", via_line)
            if em:
                p["ecmp_backup"] = em.group(1) or "LFA"
        return p

    for m in re.finditer(r"^\s*next hop (%s)/\d+ (\S+)\s+labels imposed \{([^}]*)\}" % ADDR_RE, text, re.M):
        labels = [_lab_int(x) for x in m.group(3).split()]
        res["paths"].append(mark({"nh": m.group(1), "out_if": m.group(2),
                                  "labels": [l for l in labels if l is not None]}, _xr_via_line(text, m.start())))
    if not res["paths"]:
        # IGP route: "via 10.10.0.5/32, GigabitEthernet0/0/0/0, ... labels imposed {16001}"
        vias = list(re.finditer(r"^\s*via (%s)/\d+, (\S+?),.*$" % ADDR_RE, text, re.M))
        for i, m in enumerate(vias):
            end = vias[i + 1].start() if i + 1 < len(vias) else min(len(text), m.start() + 600)
            blk = text[m.start():end]
            lm = re.search(r"labels imposed \{([^}]*)\}", blk)
            labels = [_lab_int(x) for x in lm.group(1).split()] if lm else []
            sm = re.search(r"SID-list \{([^}]*)\}", blk)
            if not labels and sm and _XR_BACKUP.search(m.group(0)):
                labels = [x for x in sm.group(1).replace(",", " ").split() if x]   # SRv6 TI-LFA segments
            res["paths"].append(mark({"nh": m.group(1), "out_if": m.group(2),
                                      "labels": [l for l in labels if l is not None]}, m.group(0)))
    if not res["paths"]:
        # a route whose next hop is directly reachable (a CE learned over eBGP):
        # only the load-distribution table names the interface
        for m in re.finditer(r"^\s*\d+\s+Y\s+(\S+)\s+(%s)" % ADDR_RE, text, re.M):
            if m.group(1) != "recursive":
                res["paths"].append({"nh": m.group(2), "out_if": m.group(1), "labels": []})
    # TI-LFA: "next hop fe80::.../128, Repair Node(s): 2001:db8::12[, ...]"
    for m in re.finditer(r"Repair Node\(s\): ([^\n]+)", text):
        via = _xr_via_line(text, m.start())
        nodes = [norm_ip(x) for x in re.findall(ADDR_RE, m.group(1))]
        for p in res["paths"]:
            if p.get("backup") and re.search(r"via %s/" % re.escape(p["nh"]), via, re.I):
                p["repair_nodes"] = nodes
    if res["via"] and res["paths"]:
        bottom = [p["labels"][-1] for p in res["paths"] if len(p["labels"]) > 1]
        res["vpn_label"] = bottom[0] if bottom else None
    if not res["paths"] and re.search(r"attached|local adjacency", text):
        res["connected"] = True
    return _split_backups(res)


def parse_nxos_route(text):
    res = {"paths": [], "via": None, "vpn_label": None, "connected": False}
    for m in re.finditer(r"\*via (%s), (\S+?),\s*\[\d+/\d+\].*?,\s*(\S+)" % IP_RE, text):
        if m.group(3).startswith(("direct", "local")):
            res["connected"] = True
        res["paths"].append({"nh": m.group(1), "out_if": m.group(2), "labels": []})
    return _split_backups(res)


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
    # a next hop whose interface just went down can still say "fib": true for a
    # moment - when some are marked active, only those forward
    any_active = any(nh.get("active") for nh in r.get("nexthops") or [] if not nh.get("recursive"))
    for nh in r.get("nexthops") or []:
        if not nh.get("recursive") and any_active and not nh.get("active"):
            continue
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
    # TI-LFA in FRR: repair paths are listed apart from the next hops
    res["backups"] = [{"nh": nh.get("ip"), "out_if": nh.get("interfaceName"), "backup": True, "kind": "TI-LFA",
                       "labels": [l for l in (_lab_int(x) for x in nh.get("labels") or []) if l is not None]}
                      for nh in r.get("backupNexthops") or [] if nh.get("interfaceName")]
    return res


# --------------------------------------------------------------------------
# the walk
# --------------------------------------------------------------------------

class Tracer:
    def __init__(self, lab, poller, linkstate=None):
        self.lab = lab
        self.linkstate = linkstate or {}    # link id -> {failed, down, igp_state} (live poller + linkctl)
        self.poller = poller
        self.nodes = {n["name"]: n for n in lab.get("nodes") or []}
        self.cont = {c["short"]: c for c in lab.get("containers") or [] if c.get("state") == "running"}
        self.ips = {}               # node -> set of its IPv4 addresses
        self.ips6 = {}              # node -> set of its global IPv6 addresses (fetched on demand)
        self.ifvrf = {}             # node -> {platform ifname: vrf}
        self.locators = {}          # node -> [ipaddress.IPv6Network] (SRv6)
        self.cache = {}             # (node, vrf, target) -> lookup result
        self.lookups = 0
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
        cached = [ip for ip in (self.poller.proto.get(node) or {}).get("ips") or [] if ":" not in ip]
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

    IP6_CMDS = {"cisco_xe": "show ipv6 interface brief",
                "cisco_xr": "show ipv6 vrf all interface brief",
                "cisco_nxos": "show ipv6 interface brief vrf all"}

    def owned6(self, node):
        if node not in self.ips6:
            plat = self.platform(node)
            ips = set()
            try:
                if plat in ("linux", "frr"):
                    ips = parse_ips6(" ".join(re.findall(r"inet6 (\S+)", self.nsrun(node, [linkctl.IP, "-6", "-o", "addr"]))))
                elif plat in self.IP6_CMDS:
                    ips = parse_ips6(self.run(node, self.IP6_CMDS[plat]))
            except Exception:                                  # noqa: BLE001
                pass
            self.ips6[node] = ips
        return self.ips6[node]

    def owns(self, node, ip):
        return ip in (self.owned6(node) if ":" in ip else self.owned(node))

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

    def sid_owner(self, sid):
        """The node whose SRv6 locator holds this SID."""
        for node in self.cont:
            if self.platform(node) == "cisco_xr" and self.owns_sid(node, sid):
                return node
        return None

    def owner_of(self, ip):
        for node in self.cont:
            if self.owns(node, ip):
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

    def trace(self, src, dst, src_vrf=None, branches=True):
        """The path from src to dst. With branches, every equal-cost next hop
        is followed too (each fork re-walks from the router where it splits);
        the first path is `hops`, the others `branches`."""
        if src not in self.cont:
            raise TraceError("%s is not running" % src)
        t0 = time.time()
        start = {"node": src, "in_port": None, "vrf": src_vrf, "target": dst, "inner": dst,
                 "vpn_vrf": None, "stack": [], "srv6": False}
        queue, done, seen = [(start, [], None)], [], set()
        while queue and len(done) < MAX_BRANCHES and self.lookups < MAX_LOOKUPS:
            st, hops, force = queue.pop(0)
            hops, result = self._walk(dict(st, stack=list(st["stack"])), hops, force,
                                      queue if branches else None)
            key = tuple((h["node"], h.get("link")) for h in hops)
            if key in seen:
                continue
            seen.add(key)
            done.append((hops, result))
        out = self._done(done[0][0], done[0][1], t0)
        out["branches"] = [{"hops": h, "result": r} for h, r in done[1:]]
        if queue:
            out["branches_capped"] = True
        return out

    def _lookup(self, node, vrf, target):
        k = (node, vrf, target)
        if k not in self.cache:
            self.lookups += 1
            self.cache[k] = self.lookup(node, vrf, target)
        return self.cache[k]

    def _walk(self, st, hops, force, forks):
        node, in_port, vrf = st["node"], st["in_port"], st["vrf"]
        target, inner, vpn_vrf, stack, srv6 = st["target"], st["inner"], st["vpn_vrf"], st["stack"], st["srv6"]
        for _ in range(MAX_HOPS):
            n0 = len(hops)
            here = {"node": node, "in_port": in_port, "vrf": vrf, "target": target, "inner": inner,
                    "vpn_vrf": vpn_vrf, "stack": list(stack), "srv6": srv6}
            hop = {"node": node, "in_port": in_port, "vrf": vrf, "lookup": target, "in_labels": list(stack)}
            hops.append(hop)
            owns = self.owns(node, target)
            if srv6 and self.owns_sid(node, target):
                # the SID belongs to this node's locator: decapsulate into the VRF
                hop["action"] = "SRv6 decap %s → VRF %s" % (target, vpn_vrf or "?")
                vrf, target, stack, srv6 = vpn_vrf, inner, [], False
                hop2 = {"node": node, "in_port": in_port, "vrf": vrf, "lookup": target, "in_labels": []}
                hops.append(hop2)
                hop = hop2
                owns = self.owns(node, target)
            elif owns and target != inner:
                # the BGP next hop is here: this is the egress PE, back into the VRF
                hop["action"] = "pop VPN label%s → VRF %s" % (
                    (" %s" % stack[-1]) if stack else "", vpn_vrf or "?")
                vrf, target, stack = vpn_vrf, inner, []
                hop2 = {"node": node, "in_port": in_port, "vrf": vrf, "lookup": target, "in_labels": []}
                hops.append(hop2)
                hop = hop2
                owns = self.owns(node, target)
            if owns:
                hop["action"] = "delivered"
                return hops, "reached"
            try:
                res = self._lookup(node, vrf, target)
            except TraceError as exc:
                hop["action"] = str(exc)
                return hops, "unsupported"
            if not res["paths"]:
                hop["action"] = "no route to %s%s" % (target, (" in VRF %s" % vrf) if vrf else "")
                return hops, "no-route"
            # this branch was forked onto one of the equal-cost next hops
            p = force or res["paths"][0]
            if force is None and forks is not None:
                for alt in res["paths"][1:]:
                    forks.append((here, [dict(h) for h in hops[:n0]], alt))
            force = None
            others = [x for x in res["paths"] if x is not p]
            if res.get("updated"):
                hop["fib_updated"] = res["updated"]
            if res.get("srv6_sids") and not srv6:
                # SRv6 VPN: the packet is encapsulated towards the service SID;
                # the core forwards on that IPv6 destination (its locator)
                hop["srv6"] = res["srv6_sids"]
                vpn_vrf, target, srv6 = vrf, res["srv6_sids"][0], True
                hop["out_if"], hop["nh"] = p["out_if"], p["nh"]
                hop["out_labels"] = []
                hop["ecmp"] = [{"out_if": x["out_if"], "nh": x["nh"], "labels": []} for x in others]
                hop["action"] = "SRv6 H.Encaps.Red → %s" % " ".join(res["srv6_sids"])
                hop["stack_out"] = ["DA " + target]
                link, far, far_port = self.link_out(node, p["out_if"])
                if link is None:
                    hop["action"] += " - %s leaves the lab" % p["out_if"]
                    return hops, "left-lab"
                hop["link"] = link["id"]
                hop["ecmp_links"] = [x["id"] for x in (self.link_out(node, e["out_if"])[0] for e in hop["ecmp"]) if x]
                # the repair path is held on the SID's locator route in the
                # global table, not on the VRF route that did the encap
                try:
                    core = self._lookup(node, None, target)
                except TraceError:
                    core = None
                self._protection(hop, node, core if core and core["paths"] else res)
                if self._check(hop, link):
                    return hops, "blackhole"
                stack = [target]
                node, in_port = far, far_port
                vrf = None
                if far not in self.cont:
                    hops.append({"node": far, "in_port": far_port, "action": "not running"})
                    return hops, "down"
                continue
            if res["via"] and res["via"] != target and not stack:
                # VPN (or recursive BGP) route: follow the BGP next hop from here on
                hop["via"] = res["via"]
                vpn_vrf, target = vrf, res["via"]
            hop["out_if"], hop["nh"] = p["out_if"], p["nh"]
            hop["out_labels"] = p["labels"]
            hop["ecmp"] = [{"out_if": x["out_if"], "nh": x["nh"], "labels": x["labels"]} for x in others]
            hop["action"] = ("forward on SRv6 DA %s" % target) if srv6 else _describe(stack, p["labels"])
            link, far, far_port = self.link_out(node, p["out_if"])
            if link is None:
                if res["connected"] and target == inner:
                    hop["action"] = "delivered on a directly connected network"
                    return hops, "reached"
                hop["action"] += " - %s leaves the lab" % p["out_if"]
                return hops, "left-lab"
            hop["link"] = link["id"]
            hop["ecmp_links"] = [x["id"] for x in (self.link_out(node, e["out_if"])[0] for e in hop["ecmp"]) if x]
            self._protection(hop, node, res)
            if self._check(hop, link):
                return hops, "blackhole"
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
                return hops, "down"
            # plain IP arriving on a router is looked up in its incoming port's VRF
            vrf = None if stack else self.vrf_of(far, self._platform_if(far, far_port))
            if not stack and target != inner:
                target = inner          # PHP popped the transport and there is no VPN label
        return hops, "loop"

    def _protection(self, hop, node, res):
        """The repair path (LFA / TI-LFA) the router holds for this hop, if any."""
        bks = res.get("backups") or []
        if not bks:
            # XR "ECMP-backup": the other equal-cost path is this one's repair
            mine = next((x for x in res["paths"] if x["out_if"] == hop.get("out_if")), None)
            if mine and mine.get("ecmp_backup"):
                bks = [dict(x, kind=mine["ecmp_backup"] + " (ECMP)") for x in res["paths"] if x is not mine][:1]
        if bks:
            b = bks[0]
            link, far, _ = self.link_out(node, b["out_if"])
            hop["backup"] = {"out_if": b["out_if"], "nh": b["nh"], "labels": b.get("labels") or [],
                             "kind": b.get("kind") or "backup", "link": link["id"] if link else None, "to": far}
            if b.get("repair_nodes"):
                hop["backup"]["repair_nodes"] = b["repair_nodes"]
        elif self.platform(node) in ("cisco_xe", "cisco_xr"):
            hop["unprotected"] = True

    def blocked(self, link_id):
        """(drop reason, warning) for forwarding out of this link right now.

        The walk reads the routers' forwarding tables; they can still point at
        a link that is dead - before the IGP notices (a `cut` on a VM router is
        only seen when hellos time out) - so the live link state is checked too.
        """
        st = self.linkstate.get(link_id) or {}
        f = st.get("failed")
        if st.get("down"):
            return ("the link is down%s" % (" (failed from the dashboard, %s)" % f.get("mode") if f else ""), None)
        if st.get("igp_state") in ("lost", "down"):
            # the live view polls adjacencies every ~10 s: right after a link
            # comes back it still says "lost" while the router already forwards
            # over it again. A link that is really dead shows as down above.
            return (None, "the live view last saw the IGP adjacency on this link down, but the router "
                          "forwards over it - either it has just come back, or the router has not converged yet")
        if f:
            return (None, "marked failed from the dashboard (%s), but the interfaces and the IGP adjacency "
                          "are still up - the failure did not take effect" % f.get("mode"))
        return (None, None)

    def _check(self, hop, link):
        """True when the packet is dropped on this link."""
        reason, warn = self.blocked(link["id"])
        if warn:
            hop["warn"] = warn
        if reason:
            hop["blocked"] = reason
            return True
        return False

    def _platform_if(self, node, port):
        import topoedit
        plat = self.platform(node)
        if plat in ("linux", "frr"):
            return port
        name = topoedit.node_iface(self.lab, node, self.nodes[node]["kind"], port)
        if plat == "cisco_xr":
            # the box may call it TenGigE0/0/0/N (vmxnet3), not GigabitEthernet:
            # use whatever interface it really has that maps to this port
            self.vrf_of(node, name)
            for real in self.ifvrf.get(node) or {}:
                if livestate.to_port(plat, real) == port:
                    return real
        return name

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


def trace(lab, poller, src, dst, src_vrf=None, linkstate=None, branches=True):
    try:
        dst = str(ipaddress.ip_address(dst))
    except ValueError:
        raise TraceError("destination must be an IPv4 or IPv6 address")
    return Tracer(lab, poller, linkstate).trace(src, dst, src_vrf, branches)


def node_addresses(lab, poller, node, t=None):
    """Candidate addresses for a node, best first: Loopback0 IPv4, Loopback0
    IPv6, then its other data addresses (IPv4, then IPv6)."""
    t = t or Tracer(lab, poller)
    lo4, lo6 = [], []
    if node in t.cont:
        plat = t.platform(node)
        try:
            if plat in ("linux", "frr"):
                lo4 = re.findall(r"inet (%s)/" % IP_RE, t.nsrun(node, [linkctl.IP, "-4", "-o", "addr", "show", "dev", "lo"]))
                lo4 = [ip for ip in lo4 if not ip.startswith("127.")]
                lo6 = sorted(parse_ips6(" ".join(re.findall(r"inet6 (\S+)", t.nsrun(
                    node, [linkctl.IP, "-6", "-o", "addr", "show", "dev", "lo"])))))
            elif plat in ("cisco_xe", "cisco_xr", "cisco_nxos"):
                cmd = {"cisco_xe": "show ip interface brief", "cisco_xr": "show ipv4 vrf all interface brief",
                       "cisco_nxos": "show ip interface brief vrf all"}[plat]
                m = re.search(r"^(?:Loopback0|Lo0|loopback0)\s+(%s)" % IP_RE, t.run(node, cmd), re.M)
                lo4 = [m.group(1)] if m else []
                lo6 = sorted(parse_ips6(_loopback_block(t.run(node, Tracer.IP6_CMDS[plat]))))
        except Exception:                                  # noqa: BLE001
            pass
    mg = {c.get("ipv4") for c in lab.get("containers") or []}
    v4 = sorted(t.owned(node), key=lambda ip: (not ip.startswith("10.0."), ip))
    v4 = [ip for ip in v4 if ip not in mg and not ip.startswith("172.")]
    v6 = sorted(t.owned6(node)) if node in t.cont else []
    out = []
    for ip in lo4 + lo6 + v4 + v6:
        if ip not in out:
            out.append(ip)
    return out


def node_address(lab, poller, node, src=None, family=None):
    """A sensible address for a node: its loopback, else its first data address.
    With `src`, the first candidate src actually has a route to - an IPv6-only
    core (SRv6) routes the IPv6 loopback, not the IPv4 one the box also has.
    `family` (4 or 6) limits it to one address family."""
    t = Tracer(lab, poller)
    cands = node_addresses(lab, poller, node, t)
    if family:
        cands = [ip for ip in cands if (":" in ip) == (family == 6)]
    if src and src != node and src in t.cont and len(cands) > 1:
        for ip in cands[:4]:
            try:
                if t.lookup(src, None, ip)["paths"]:
                    return ip
            except Exception:                              # noqa: BLE001
                continue
    return cands[0] if cands else None
