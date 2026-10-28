#!/usr/bin/env python3
"""
Trace the path a packet takes through a running lab through each router's own
dataplane, hop by hop - the packet is carried as it is on the wire:

  * IP: the destination is looked up in the table of the VRF the packet
    arrived in (IOS-XE `show ip cef`, IOS-XR `show cef`, NX-OS `show ip
    route`, FRR `show ip route ... json`, Linux `ip route get`). The result
    forwards it, pushes labels, or encapsulates it in SRv6 - with the SR
    policy's inserted SIDs when the route is steered into one (XR shows the
    binding SID and both SID lists); the SIDs are packed into uSID containers
    as the headend does.
  * SRv6: the DA is looked up in the global table. A local SID applies its
    behaviour as the router reports it - uN / uA (shift) move to the next
    uSID (uA out of its own adjacency), uB6 inserts the policy's SIDs, uDT4/6
    decapsulates into the VRF, the last segment with USD decapsulates; any
    other DA is forwarded on.
  * MPLS: the top label is looked up in the router's label table (FRR `show
    mpls table`): swap, PHP pop, or pop the VPN label into its VRF. On
    platforms whose label table is not read here (IOS-XE / IOS-XR / NX-OS)
    the walk follows the route to the FEC (the BGP next hop) and rewrites the
    top label as that route says.

The interface a router sends on is mapped to the clab port, the port to the
link, the link to the neighbour - the next hop on the map. Every equal-cost
next hop is followed (each fork re-walks from the router where it splits).
A trace ends when the packet is delivered, dropped (no route / no label), it
leaves the lab, or after MAX_HOPS links. SR Linux is not walked.

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

MAX_HOPS = 24               # links crossed
MAX_STEPS = 64              # lookups along one path (several per router: shift, decap, ...)
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
    for m in re.finditer(r"SRv6 H\.(Encaps|Insert)\S*\s+SID-list \{([^}]*)\}", text):
        # a TI-LFA repair path carries its own SID list - that is not the service encap
        if not _XR_BACKUP.search(_xr_via_line(text, m.start())):
            key = "srv6_sids" if m.group(1) == "Encaps" else "srv6_insert"
            res.setdefault(key, [norm_ip(x.strip()) for x in m.group(2).replace(",", " ").split() if x.strip()])
    # "5f00:0:11::/48, version 5, SRv6 Endpoint uN (shift), internal ..." - a local SID
    m = re.search(r"^(\S+)/(\d+), version \d+, SRv6 Endpoint ([^,]+?), internal", text, re.M)
    if m:
        vm = re.search(r"next hop VRF - '([^']+)'", text)
        res["endpoint"] = {"sid": norm_ip(m.group(1)), "plen": int(m.group(2)), "behavior": m.group(3).strip(),
                           "vrf": vm.group(1) if vm and vm.group(1) != "default" else None}
    if re.search(r"^\S+, version \d+, SRv6 Headend", text, re.M):
        res["headend"] = True
    m = re.search(r"via local-srv6-sid (\S+?),", text)
    if m:
        res["bsid"] = norm_ip(m.group(1))        # steered into an SR policy by its binding SID
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
        seg6 = nh.get("seg6") or {}
        if seg6.get("segs"):
            segs = seg6["segs"] if isinstance(seg6["segs"], list) else str(seg6["segs"]).split("/")
            res.setdefault("srv6_sids", [norm_ip(x) for x in segs if x])
        loc = nh.get("seg6local") or {}
        if loc.get("action"):
            ctx = nh.get("seg6localContext") or {}
            res["endpoint"] = {"sid": None, "plen": r.get("prefixLen"), "behavior": loc["action"],
                               "vrf": ctx.get("vrfName") or ctx.get("table")}
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
# SRv6 uSID (NEXT-CSID, F3216) containers
# --------------------------------------------------------------------------

USID_BITS = 16
BLOCK_BITS = 32                 # F3216: 32-bit block, 16-bit uSIDs


def _usids(sid, block_bits=BLOCK_BITS):
    """(block, [uSIDs]) of a uSID container, or None when the address is not
    one (a classic 128-bit SID: non-zero bits after the first empty slot)."""
    a = int(ipaddress.IPv6Address(sid))
    rest_bits = 128 - block_bits
    rest = a & ((1 << rest_bits) - 1)
    out = []
    for i in range(rest_bits // USID_BITS):
        u = (rest >> (rest_bits - USID_BITS * (i + 1))) & 0xffff
        if u == 0:
            if rest & ((1 << (rest_bits - USID_BITS * (i + 1))) - 1):
                return None
            break
        out.append(u)
    return a >> rest_bits, out


def _container(block, usids, block_bits=BLOCK_BITS):
    rest_bits = 128 - block_bits
    v = block << rest_bits
    for i, u in enumerate(usids):
        v |= u << (rest_bits - USID_BITS * (i + 1))
    return str(ipaddress.IPv6Address(v))


def srv6_compose(sids, block_bits=BLOCK_BITS):
    """The (DA, [SRH segments]) a headend builds from a SID list: consecutive
    uSID containers of one block are packed into as few containers as fit
    (H.Encaps.Red / H.Insert.Red with NEXT-CSID); other SIDs stay one each."""
    per = (128 - block_bits) // USID_BITS
    segs, cur = [], None
    for sid in sids:
        u = _usids(sid, block_bits)
        if u and cur and cur[0] == u[0] and len(cur[1]) + len(u[1]) <= per:
            cur[1].extend(u[1])
            continue
        if cur:
            segs.append(_container(cur[0], cur[1], block_bits))
            cur = None
        if u:
            cur = [u[0], list(u[1])]
        else:
            segs.append(norm_ip(sid))
    if cur:
        segs.append(_container(cur[0], cur[1], block_bits))
    return (segs[0], segs[1:]) if segs else (None, [])


def srv6_shift(da, n, block_bits=BLOCK_BITS):
    """The DA after a uN / uA / uB6 (shift) consumed n uSIDs; None when the
    container is used up."""
    u = _usids(da, block_bits)
    if not u or len(u[1]) <= n:
        return None
    return _container(u[0], u[1][n:], block_bits)


# --------------------------------------------------------------------------
# label (LFIB) lookups -> {"paths": [{out_if, nh, out: [labels] | None,
#   vrf: pop and look up in this VRF | None, unlabelled: bool}], "backups": []}
# out [] = pop (PHP / implicit-null)
# --------------------------------------------------------------------------

def parse_frr_lfib(js):
    res = {"paths": [], "backups": []}
    try:
        d = json.loads(js[js.index("{"):]) if "{" in js else {}
    except ValueError:
        return res

    def conv(nh):
        stack = nh.get("outLabelStack") or ([nh["outLabel"]] if "outLabel" in nh else [])
        out = [int(x) for x in stack if str(x).isdigit() and int(x) != 3]
        if not nh.get("nexthop") and nh.get("type") == "BGP" and nh.get("interface"):
            # the VPN label: pop it and look the packet up in the VRF
            return {"out_if": None, "nh": None, "out": [], "vrf": nh["interface"], "unlabelled": False}
        return {"out_if": nh.get("interface"), "nh": nh.get("nexthop"), "out": out, "vrf": None,
                "unlabelled": False, "type": nh.get("type")}
    for nh in d.get("nexthops") or []:
        if nh.get("installed") is False:
            continue
        res["paths"].append(conv(nh))
    for nh in d.get("backupNexthops") or []:
        res["backups"].append(dict(conv(nh), backup=True, kind="TI-LFA",
                                   labels=[l for l in conv(nh)["out"]]))
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
            enc = r.get("encap") or {}
            labels, res = [], {"via": None, "vpn_label": None,
                               "connected": r.get("type") == "local" or not r.get("gateway")}
            if enc.get("encap_type") == "mpls":
                labels = [_lab_int(x) for x in str(enc.get("dst") or "").split("/") if x]
                res["connected"] = False
            elif enc.get("encap_type") == "seg6" and enc.get("segs"):
                res["srv6_sids"] = [norm_ip(x) for x in enc["segs"]]
                res["connected"] = False
            res["paths"] = [{"nh": r.get("gateway"), "out_if": r.get("dev"), "labels": [l for l in labels if l is not None]}]
            return res
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
        start = {"node": src, "in_port": None, "vrf": src_vrf, "inner": dst, "enc": None,
                 "fec": None, "vpn_vrf": None}
        queue, done, seen = [(start, [], None)], [], set()
        while queue and len(done) < MAX_BRANCHES and self.lookups < MAX_LOOKUPS:
            st, hops, force = queue.pop(0)
            hops, result = self._walk(_copy_state(st), hops, force, queue if branches else None)
            if result == "down" and hops and hops[-1].get("_far"):
                far, far_port = hops[-1].pop("_far")
                hops.append({"node": far, "in_port": far_port, "action": "not running"})
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

    def _lfib(self, node, label):
        """Label table entry on a platform whose LFIB is read directly, else None."""
        if self.platform(node) not in LABEL_WALK:
            return None
        k = (node, "mpls", label)
        if k not in self.cache:
            self.lookups += 1
            rc, out = devcfg.ExecConn(self.cont[node]["name"], "frr").exec(
                ["vtysh", "-c", "show mpls table %d json" % label], timeout=20)
            self.cmds += 1
            self.cache[k] = parse_frr_lfib(out)
        return self.cache[k]

    # The packet is walked through each router's own dataplane: an MPLS packet
    # by its top label, an SRv6 packet by its destination address (a local SID
    # applies its behaviour - uN/uA shift, uB6 policy insert, uDT decap), an
    # IP packet by its destination in the VRF it arrived in. Each step on a
    # router is a record of its own; the map groups them per router.
    def _walk(self, st, hops, force, forks):
        links = 0
        for _ in range(MAX_STEPS):
            here = _copy_state(st)
            n0 = len(hops)
            node, enc = st["node"], st["enc"]
            hop = {"node": node, "in_port": st["in_port"], "vrf": st["vrf"], "in_labels": _enc_repr(enc)}
            hops.append(hop)

            def pick(paths):
                # this branch was forked onto one of the equal-cost next hops
                nonlocal force
                p = next((x for x in paths if force and _same_path(x, force)), None) or paths[0]
                if force is None and forks is not None:
                    for alt in paths:
                        if alt is not p:
                            forks.append((here, [dict(h) for h in hops[:n0]], alt))
                force = None
                return p, [x for x in paths if x is not p]

            # ---- MPLS: the top label
            if enc and enc["t"] == "mpls":
                stack = enc["stack"]
                top = stack[0]
                hop["lookup"] = "label %s" % top
                if top in (0, 2):
                    hop["action"] = "pop explicit-null %s" % top
                    st["enc"] = {"t": "mpls", "stack": stack[1:]} if stack[1:] else None
                    continue
                lf = self._lfib(node, top)
                if lf is None:
                    r = self._mpls_by_fec(st, hop, pick)
                    if r == "next":
                        continue
                    if r:
                        return hops, r
                    links += 1
                    continue
                if not lf["paths"]:
                    hop["action"] = "no label table entry for %s - dropped" % top
                    return hops, "no-route"
                p, others = pick(lf["paths"])
                if p["vrf"]:
                    hop["action"] = "pop VPN label %s → VRF %s" % (top, p["vrf"])
                    rest = stack[1:]
                    st["enc"] = {"t": "mpls", "stack": rest} if rest else None
                    st["vrf"] = p["vrf"]
                    continue
                new = p["out"] + stack[1:]
                if not p["out"]:
                    hop["action"] = "pop %s (PHP)" % top
                elif len(p["out"]) == 1:
                    hop["action"] = "swap %s → %s" % (top, p["out"][0])
                else:
                    hop["action"] = "swap %s → %s, push %s" % (top, p["out"][-1], " ".join(str(x) for x in p["out"][:-1]))
                hop["out_labels"] = p["out"]
                st["enc"] = {"t": "mpls", "stack": new} if new else None
                r = self._send(st, hop, node, p, others, lf)
                if r:
                    return hops, r
                links += 1
                if links > MAX_HOPS:
                    break
                continue

            # ---- SRv6: the destination address
            if enc and enc["t"] == "srv6":
                da = enc["da"]
                hop["lookup"] = da
                res = self._lookup(node, None, da)
                ep = res.get("endpoint")
                if not ep and self.owns(node, da):
                    hop["action"] = "delivered (SRv6 DA %s is this router's address)" % da
                    return hops, "reached"
                if ep:
                    r = self._srv6_endpoint(st, hop, res, ep, pick)
                    if r == "next":
                        continue
                    if r:
                        return hops, r
                    links += 1
                    continue
                if res.get("updated"):
                    hop["fib_updated"] = res["updated"]
                if not res["paths"]:
                    hop["action"] = "no route to %s - dropped" % da
                    return hops, "no-route"
                p, others = pick(res["paths"])
                hop["action"] = "forward on SRv6 DA %s" % da
                r = self._send(st, hop, node, p, others, res)
                if r:
                    return hops, r
                links += 1
                if links > MAX_HOPS:
                    break
                continue

            # ---- IP: the destination in the VRF it arrived in
            inner, vrf = st["inner"], st["vrf"]
            hop["lookup"] = inner
            if self.owns(node, inner):
                hop["action"] = "delivered"
                return hops, "reached"
            try:
                res = self._lookup(node, vrf, inner)
            except TraceError as exc:
                hop["action"] = str(exc)
                return hops, "unsupported"
            if res.get("endpoint") and not vrf and ":" in inner:
                # an IPv6 packet whose destination is a local SID gets the SID's
                # behaviour - SRv6 is plain IPv6 on the wire (a trace to a SID)
                hops.pop()
                st["enc"], st["sid_target"] = {"t": "srv6", "da": inner, "srh": []}, True
                continue
            if res.get("updated"):
                hop["fib_updated"] = res["updated"]
            if not res["paths"]:
                hop["action"] = "no route to %s%s" % (inner, (" in VRF %s" % vrf) if vrf else "")
                return hops, "no-route"
            p, others = pick(res["paths"])
            # XR calls a route "SRv6 Headend" also when only its TI-LFA backup
            # inserts SIDs: the primary path's Encaps / Insert list is what counts
            if res.get("srv6_sids") or res.get("srv6_insert"):
                # H.Insert alone (IPv6 steered into a policy): the original
                # destination stays as the last segment
                sids = (res.get("srv6_insert") or []) + (res.get("srv6_sids") or [inner])
                da, srh = srv6_compose(sids)
                if not da:
                    hop["action"] = "SRv6 headend without a SID list"
                    return hops, "unsupported"
                hop["srv6"] = res.get("srv6_sids") or []
                hop["action"] = "SRv6 H.Encaps.Red → %s" % da + (
                    " (SR policy, BSID %s: + %s)" % (res["bsid"], " ".join(res.get("srv6_insert") or []))
                    if res.get("bsid") else "")
                st["enc"] = {"t": "srv6", "da": da, "srh": srh}
                st["vpn_vrf"] = vrf
                # the repair path lives on the core route (or the policy's BSID), not the VRF route
                core = None
                try:
                    core = self._lookup(node, None, res.get("bsid") or da)
                except TraceError:
                    pass
                r = self._send(st, hop, node, p, others, core if core and core["paths"] else res)
                if r:
                    return hops, r
                links += 1
                continue
            if p["labels"]:
                st["enc"] = {"t": "mpls", "stack": list(p["labels"])}
                st["fec"] = res["via"] if res["via"] and res["via"] != inner else None
                st["vpn_vrf"] = vrf
                if res["via"] and res["via"] != inner:
                    hop["via"] = res["via"]
                hop["action"] = "push " + " ".join(str(l) for l in p["labels"])
                hop["out_labels"] = p["labels"]
            else:
                hop["action"] = "forward"
            r = self._send(st, hop, node, p, others, res, connected=res["connected"])
            if r:
                return hops, r
            links += 1
            if links > MAX_HOPS:
                break
        return hops, "loop"

    def _srv6_endpoint(self, st, hop, res, ep, pick):
        """Apply a local SID's behaviour. "next" = carry on at this router."""
        node, da, srh = st["node"], st["enc"]["da"], list(st["enc"]["srh"])
        beh = ep["behavior"]
        kind = beh.split()[0].split("(")[0]
        k = kind.lower()
        shift = "shift" in beh.lower()
        block = BLOCK_BITS
        used = max(1, ((ep.get("plen") or block + USID_BITS) - block) // USID_BITS)

        def advance():
            """The DA after this SID: the rest of the container, else the next
            SRH segment; None = no segments left."""
            nxt = srv6_shift(da, used) if shift else None
            if nxt is None and srh:
                nxt = srh.pop(0)
            return nxt
        if st.get("sid_target") and (k.startswith(("udt", "end.dt", "udx", "end.dx"))
                                     or (k in ("un", "end") and not (shift and srv6_shift(da, used)) and not srh)):
            hop["action"] = "SRv6 SID %s reached (%s)" % (da, beh)
            return "reached"
        if k in ("udt4", "udt6", "udt46", "end.dt4", "end.dt6", "end.dt46"):
            vrf = ep.get("vrf") or st.get("vpn_vrf")
            hop["action"] = "SRv6 decap (%s) → VRF %s" % (kind, vrf or "default")
            st["enc"], st["vrf"] = None, vrf
            return "next"
        if k in ("un", "end"):
            nxt = advance()
            if nxt:
                hop["action"] = "SRv6 %s shift → DA %s" % (kind, nxt)
                st["enc"] = {"t": "srv6", "da": nxt, "srh": srh}
            else:
                # last segment with USD: the inner packet goes on as plain IP
                hop["action"] = "SRv6 %s end of segments - decap" % kind
                st["enc"], st["vrf"] = None, None
            return "next"
        if k in ("ub6", "end.b6", "end.b6.encaps", "end.b6.insert"):
            rest = advance()
            policy = res.get("srv6_insert") or res.get("srv6_sids") or []
            if not policy:
                hop["action"] = "SRv6 %s without a policy SID list" % kind
                return "unsupported"
            nda, nsrh = srv6_compose(policy + ([rest] if rest else []))
            hop["action"] = "SRv6 %s policy insert %s → DA %s" % (kind, " ".join(policy), nda)
            st["enc"] = {"t": "srv6", "da": nda, "srh": nsrh + srh}
            return "next"
        if k in ("ua", "end.x", "udx4", "udx6", "end.dx4", "end.dx6"):
            if not res["paths"]:
                hop["action"] = "SRv6 %s with no adjacency" % kind
                return "no-route"
            p, others = pick(res["paths"])
            if k.startswith(("udx", "end.dx")):
                hop["action"] = "SRv6 decap (%s) → out %s" % (kind, p["out_if"])
                st["enc"], st["vrf"] = None, None
            else:
                nxt = advance()
                if nxt:
                    hop["action"] = "SRv6 %s shift → DA %s, out %s" % (kind, nxt, p["out_if"])
                    st["enc"] = {"t": "srv6", "da": nxt, "srh": srh}
                else:
                    hop["action"] = "SRv6 %s end of segments - decap, out %s" % (kind, p["out_if"])
                    st["enc"] = None
            return self._send(st, hop, node, p, others, res)
        hop["action"] = "SRv6 endpoint %s (not modelled)" % beh
        return "unsupported"

    def _mpls_by_fec(self, st, hop, pick):
        """A labelled packet on a router whose label table is not read: follow
        the route to the FEC (the BGP next hop) and rewrite the top label as
        that route says. "next" = carry on at this router."""
        node, stack, fec = st["node"], st["enc"]["stack"], st.get("fec")
        if fec and self.owns(node, fec):
            hop["action"] = "pop VPN label%s → VRF %s" % ((" %s" % stack[-1]) if stack else "", st["vpn_vrf"] or "?")
            st["enc"], st["vrf"] = None, st["vpn_vrf"]
            return "next"
        if not fec:
            hop["action"] = "label %s: this router's label table is not read and the FEC is unknown" % stack[0]
            return "unsupported"
        hop["lookup"] = fec
        try:
            res = self._lookup(node, None, fec)
        except TraceError as exc:
            hop["action"] = str(exc)
            return "unsupported"
        if res.get("updated"):
            hop["fib_updated"] = res["updated"]
        if not res["paths"]:
            hop["action"] = "no route to %s" % fec
            return "no-route"
        p, others = pick(res["paths"])
        out = [l for l in p["labels"] if l != "pop"]
        hop["action"] = _describe(stack, p["labels"])
        hop["out_labels"] = p["labels"]
        # a label-switching router only swaps / pops the top label; what is
        # below it (the VPN label) travels on unchanged
        new = out[:1] + stack[1:]
        st["enc"] = {"t": "mpls", "stack": new} if new else None
        return self._send(st, hop, node, p, others, res)

    def _send(self, st, hop, node, p, others, res, connected=False):
        """Put the packet on the wire out of path p; move the state to the far
        end. A result string when the walk ends here."""
        hop["out_if"], hop["nh"] = p.get("out_if"), p.get("nh")
        hop["ecmp"] = [{"out_if": x.get("out_if"), "nh": x.get("nh"),
                        "labels": x.get("labels") or x.get("out") or []} for x in others]
        hop["stack_out"] = _enc_repr(st["enc"])
        link, far, far_port = self.link_out(node, p.get("out_if"))
        if link is None:
            if connected and not st["enc"]:
                hop["action"] = "delivered on a directly connected network"
                return "reached"
            hop["action"] += " - %s leaves the lab" % p.get("out_if")
            return "left-lab"
        hop["link"] = link["id"]
        hop["ecmp_links"] = [x["id"] for x in (self.link_out(node, e["out_if"])[0] for e in hop["ecmp"]) if x]
        self._protection(hop, node, res)
        if self._check(hop, link):
            return "blackhole"
        st["node"], st["in_port"] = far, far_port
        if far not in self.cont:
            hop["_far"] = (far, far_port)
            return "down"
        # plain IP arriving on a router is looked up in its incoming port's VRF
        st["vrf"] = None if st["enc"] else self.vrf_of(far, self._platform_if(far, far_port))
        return None

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


# platforms whose label table the walk reads; the others follow the FEC's route
LABEL_WALK = {"frr"}


def _copy_state(st):
    st = dict(st)
    if st.get("enc"):
        st["enc"] = dict(st["enc"], **({"stack": list(st["enc"]["stack"])} if "stack" in st["enc"]
                                       else {"srh": list(st["enc"]["srh"])}))
    return st


def _same_path(a, b):
    return a.get("out_if") == b.get("out_if") and a.get("nh") == b.get("nh")


def _enc_repr(enc):
    """The encapsulation as shown on a hop card: labels top first, or the SRv6 DA (+ SRH)."""
    if not enc:
        return []
    if enc["t"] == "mpls":
        return list(enc["stack"])
    return ["DA " + enc["da"]] + ["SRH " + x for x in enc["srh"]]


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
