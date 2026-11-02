#!/usr/bin/env python3
"""
Segment-routing facts for the topology map: per router its loopback
addresses, SR-MPLS prefix-SIDs (with the SRGB), SRv6 locators (with the uN
SID) and adjacency SIDs (MPLS labels or SRv6 uA / End.X SIDs), each adjacency
SID tied to the interface it belongs to.

Collected by the lab's live-state poller (livestate.py) over the sessions it
already holds, only while someone has one of these layers switched on, and
then about once a minute - SIDs rarely change.

Where the facts come from:

  IOS-XR   loopbacks: show ipv4 / ipv6 interface brief
           prefix-SIDs: running config of router isis / ospf (prefix-sid under
           the loopback) and segment-routing global-block
           SRv6: show segment-routing srv6 locator / sid (uN, uA per interface)
           MPLS adjacency SIDs: show isis adjacency detail, show ospf neighbor detail
  IOS-XE   loopbacks: show ip / ipv6 interface brief
           prefix-SIDs: segment-routing connected-prefix-sid-map, global-block
           MPLS adjacency SIDs: show isis neighbors detail, show ip ospf neighbor detail
  FRR      loopbacks: ip addr on lo
           prefix-SIDs: segment-routing prefix / global-block in the running config
           adjacency SIDs: show isis neighbor detail json
           SRv6: show segment-routing srv6 locator json
  NX-OS    loopbacks only
  SR Linux loopbacks (system0) only - the free image has no MPLS

Interface names are returned as the platform prints them; livestate maps them
to clab ports.
"""

import ipaddress
import json
import re

IP4 = r"\d+\.\d+\.\d+\.\d+"
IP6 = r"[0-9a-fA-F]*:[0-9a-fA-F:.]+"
DEFAULT_SRGB = (16000, 23999)
LOOPBACK = re.compile(r"^(Loopback\d+|Lo\d+|loopback\d+|lo\d*|system0)$")
NO_DATA = re.compile(r"(% ?Invalid|Invalid input|No such configuration|not enabled|not running|"
                     r"Unknown command|not configured)", re.I)


def _ip(s):
    try:
        return ipaddress.ip_address(s.split("%")[0])
    except ValueError:
        return None


def _keep(a):
    """A loopback address worth showing: not link-local, not the host's own."""
    return a is not None and not a.is_link_local and not a.is_loopback


# --------------------------------------------------------------------------
# loopbacks
# --------------------------------------------------------------------------

def parse_loopbacks_ios(*texts):
    """Loopback addresses from `show ip(v6) interface brief` on IOS-XE, IOS-XR
    and NX-OS. IPv4 sits on the interface's line; IPv6 output lists the
    addresses on the lines below it."""
    los = {}
    for text in texts:
        cur = None
        for line in (text or "").splitlines():
            if not line.strip():
                continue
            if not line[0].isspace():
                name = line.split()[0]
                cur = name if LOOPBACK.match(name) else None
                rest = line[len(name):]
            else:
                rest = line
            if cur is None:
                continue
            ent = los.setdefault(cur, {"name": cur, "v4": [], "v6": []})
            for tok in re.findall(r"(%s|%s)" % (IP4, IP6), rest):
                a = _ip(tok.split("/")[0])
                if _keep(a):
                    key = "v4" if a.version == 4 else "v6"
                    if str(a) not in ent[key]:
                        ent[key].append(str(a))
    return _order([l for l in los.values() if l["v4"] or l["v6"]])


def _order(los):
    def k(l):
        m = re.search(r"(\d+)$", l["name"])
        return (int(m.group(1)) if m else 0, l["name"])
    return sorted(los, key=k)


# --------------------------------------------------------------------------
# prefix-SIDs
# --------------------------------------------------------------------------

def _srgb(text):
    m = re.search(r"global-block\s+(\d+)\s+(\d+)", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else DEFAULT_SRGB


def _psid(prefix, kind, value, algo, srgb):
    value = int(value)
    if kind == "index":
        return {"prefix": prefix, "index": value, "label": srgb[0] + value, "algo": int(algo or 0)}
    return {"prefix": prefix, "index": value - srgb[0] if srgb[0] <= value <= srgb[1] else None,
            "label": value, "algo": int(algo or 0)}


PSID_XR = re.compile(r"prefix-sid\s+(?:algorithm\s+(\d+)\s+)?(index|absolute)\s+(\d+)(?:.*?\balgorithm\s+(\d+))?")


def parse_prefix_sids_xr(config, loopbacks, srgb):
    """prefix-sid lines under an interface of router isis / ospf."""
    addr = {l["name"]: l for l in loopbacks}
    out, iface, af = [], None, "ipv4"
    for line in (config or "").splitlines():
        m = re.match(r"^\s*interface\s+(\S+)", line)
        if m:
            iface, af = m.group(1), "ipv4"
            continue
        m = re.match(r"^\s*address-family\s+(ipv4|ipv6)", line)
        if m:
            af = m.group(1)
            continue
        m = PSID_XR.search(line)
        if m and iface:
            lo = addr.get(iface) or {}
            ips = lo.get("v6" if af == "ipv6" else "v4") or []
            prefix = (ips[0] + ("/128" if af == "ipv6" else "/32")) if ips else iface
            out.append(_psid(prefix, m.group(2), m.group(3), m.group(1) or m.group(4), srgb))
    return _uniq(out)


PSID_XE = re.compile(r"(%s/\d+)\s+(index|absolute)\s+(\d+)(?:\s+range\s+\d+)?(?:.*?\balgorithm\s+(\d+))?" % IP4)


def parse_prefix_sids_xe(section, loopbacks, srgb):
    """connected-prefix-sid-map entries for this router's own loopbacks."""
    mine = {ip for l in loopbacks for ip in l["v4"]}
    out = []
    for m in PSID_XE.finditer(section or ""):
        if m.group(1).split("/")[0] in mine:
            out.append(_psid(m.group(1), m.group(2), m.group(3), m.group(4), srgb))
    return _uniq(out)


PSID_FRR = re.compile(r"segment-routing prefix\s+(\S+)\s+(index|absolute)\s+(\d+)(?:.*?\balgorithm\s+(\d+))?")


def parse_prefix_sids_frr(config, srgb):
    return _uniq([_psid(m.group(1), m.group(2), m.group(3), m.group(4), srgb)
                  for m in PSID_FRR.finditer(config or "")])


def _uniq(items):
    seen, out = set(), []
    for x in items:
        k = json.dumps(x, sort_keys=True)
        if k not in seen:
            seen.add(k)
            out.append(x)
    return sorted(out, key=lambda x: (x.get("algo") or 0, x.get("label") or 0))


# --------------------------------------------------------------------------
# SRv6
# --------------------------------------------------------------------------

def parse_locators_xr(text):
    locs = []
    for m in re.finditer(r"^(\S+)\s+(\d+)\s+(\d+)\s+(%s/\d+)\s+(\S+)" % IP6, text or "", re.M):
        locs.append({"name": m.group(1), "algo": int(m.group(3)), "prefix": str(ipaddress.ip_network(m.group(4), strict=False)),
                     "up": m.group(5).lower() == "up", "usid": None})
    return locs


def parse_sids_xr(text, locs):
    """uN goes on its locator; uA / End.X become SRv6 adjacency SIDs."""
    by_name = {l["name"]: l for l in locs}
    adj, loc = [], None
    for line in (text or "").splitlines():
        m = re.search(r"\*\*\* Locator: '([^']+)'", line)
        if m:
            loc = m.group(1)
            continue
        f = line.split()
        if len(f) < 2 or ":" not in f[0] or _ip(f[0]) is None:
            continue
        sid, beh = str(_ip(f[0])), f[1]
        if beh in ("uN", "End") and loc in by_name:
            by_name[loc]["usid"] = sid
        elif beh in ("uA", "End.X"):
            m = re.search(r"\[([^,\]]+)", line)
            if m:
                adj.append({"iface": m.group(1), "type": "srv6", "sid": sid, "behavior": beh,
                            "algo": (by_name.get(loc) or {}).get("algo", 0), "locator": loc})
    return adj


def parse_locators_frr(js):
    locs = []
    for l in (js or {}).get("locators") or []:
        if not isinstance(l, dict) or not l.get("prefix"):
            continue
        try:
            pfx = str(ipaddress.ip_network(l["prefix"], strict=False))
        except ValueError:
            continue
        locs.append({"name": l.get("name") or "?", "prefix": pfx,
                     "algo": int(l.get("algoNum") or l.get("algo") or 0),
                     "up": bool(l.get("statusUp", True)), "usid": None})
    return locs


# --------------------------------------------------------------------------
# MPLS adjacency SIDs
# --------------------------------------------------------------------------

XR_IF = r"(?:Gi|Te|TF|Fo|FH|Hu|TH|BE|Tw|Fi)[\d/.]+"


def parse_adj_isis_xr(text):
    """`show isis adjacency detail`: an adjacency row, then its SID lines."""
    out, iface = [], None
    for line in (text or "").splitlines():
        m = re.match(r"^\S+\s+(%s)\s+\S+\s+\w+\s+\d+" % XR_IF, line)
        if m:
            iface = m.group(1)
            continue
        m = re.match(r"^\s*(Non-FRR\s+)?Adjacency SID:\s+(\d+)(?:\s*\((\w+)\))?", line)
        if m and iface:
            out.append({"iface": iface, "type": "mpls", "label": int(m.group(2)),
                        "protected": not m.group(1) and (m.group(3) or "").lower() == "protected",
                        "proto": "isis"})
    return out


def parse_adj_ospf_ios(text):
    """`show (ip) ospf neighbor detail`: the interface, then the SID labels."""
    out, iface = [], None
    for line in (text or "").splitlines():
        m = re.search(r"via interface (\S+)", line)
        if m:
            iface = m.group(1).rstrip(",")
            continue
        if iface is None:
            continue
        m = re.search(r"Label:\s*(\d+),\s*\w+,\s*(\w+)", line)          # IOS-XR
        if not m:
            m = re.search(r"(?:Adj(?:acency)?[- ]?SID|SR adj label)[^\d\n]*?(\d{2,7})(.*)", line, re.I)
        if m:
            out.append({"iface": iface, "type": "mpls", "label": int(m.group(1)),
                        "protected": "unprotected" not in m.group(2).lower() and "protected" in m.group(2).lower(),
                        "proto": "ospf"})
    return out


def parse_adj_isis_xe(text):
    out, iface = [], None
    for line in (text or "").splitlines():
        m = re.match(r"^\S+\s+L[12]\S*\s+(\S+)\s+\S+\s+(?:UP|INIT|DOWN)\b", line)
        if m:
            iface = m.group(1)
            continue
        m = re.search(r"Interface name:\s*(\S+)", line)
        if m:
            iface = m.group(1)
            continue
        m = re.search(r"Adj(?:acency)?[- ]?SID[^\d\n]*?(\d{2,7})(.*)", line, re.I)
        if m and iface:
            out.append({"iface": iface, "type": "mpls", "label": int(m.group(1)),
                        "protected": "unprotected" not in m.group(2).lower() and "protected" in m.group(2).lower(),
                        "proto": "isis"})
    return out


def parse_adj_frr(js):
    """`show isis neighbor detail json`: adj-sid (or a list of them) per circuit."""
    out = []
    for area in (js or {}).get("areas") or []:
        for c in area.get("circuits") or []:
            intf = c.get("interface") if isinstance(c.get("interface"), dict) else {}
            name = intf.get("name")
            if not name:
                continue
            for key, val in intf.items():
                if "sid" not in key.lower():
                    continue
                for s in val if isinstance(val, list) else [val]:
                    if not isinstance(s, dict) or s.get("sid") is None:
                        continue
                    sid = s["sid"]
                    if isinstance(sid, str) and ":" in sid:
                        out.append({"iface": name, "type": "srv6", "sid": str(_ip(sid) or sid),
                                    "behavior": s.get("behavior") or "End.X", "algo": int(s.get("algorithm") or 0)})
                    else:
                        try:
                            out.append({"iface": name, "type": "mpls", "label": int(sid),
                                        "protected": False, "backup": bool(s.get("is-backup")),
                                        "proto": "isis"})
                        except (TypeError, ValueError):
                            pass
    return [a for a in out if not a.get("backup")]


# --------------------------------------------------------------------------
# per platform
# --------------------------------------------------------------------------

def _run(conn, cmd):
    try:
        out = conn.send_command(cmd, read_timeout=30)
    except Exception:                                   # noqa: BLE001
        return ""
    return "" if NO_DATA.search(out[:400]) else out


def _json(conn, cmd):
    out = _run(conn, cmd)
    try:
        return json.loads(out[out.index("{"):]) if "{" in out else {}
    except ValueError:
        return {}


def collect_xr(conn):
    los = parse_loopbacks_ios(_run(conn, "show ipv4 interface brief"), _run(conn, "show ipv6 interface brief"))
    srgb = _srgb(_run(conn, "show running-config segment-routing"))
    cfg = _run(conn, "show running-config router isis") + "\n" + _run(conn, "show running-config router ospf")
    psid = parse_prefix_sids_xr(cfg, los, srgb)
    locs = parse_locators_xr(_run(conn, "show segment-routing srv6 locator"))
    adj = parse_sids_xr(_run(conn, "show segment-routing srv6 sid"), locs) if locs else []
    if psid or "segment-routing mpls" in cfg:
        adj += parse_adj_isis_xr(_run(conn, "show isis adjacency detail"))
        adj += parse_adj_ospf_ios(_run(conn, "show ospf neighbor detail"))
    return {"loopbacks": los, "srgb": list(srgb) if psid else None, "prefix_sids": psid,
            "locators": locs, "adj": adj}


def collect_xe(conn):
    los = parse_loopbacks_ios(_run(conn, "show ip interface brief"), _run(conn, "show ipv6 interface brief"))
    sec = _run(conn, "show running-config | section segment-routing")
    srgb = _srgb(sec)
    psid = parse_prefix_sids_xe(sec, los, srgb)
    adj = []
    if psid or "segment-routing mpls" in sec:
        adj += parse_adj_isis_xe(_run(conn, "show isis neighbors detail"))
        adj += parse_adj_ospf_ios(_run(conn, "show ip ospf neighbor detail"))
    return {"loopbacks": los, "srgb": list(srgb) if psid else None, "prefix_sids": psid,
            "locators": [], "adj": adj}


def collect_nxos(conn):
    los = parse_loopbacks_ios(_run(conn, "show ip interface brief vrf default"),
                              _run(conn, "show ipv6 interface brief vrf default"))
    return {"loopbacks": los, "srgb": None, "prefix_sids": [], "locators": [], "adj": []}


def collect_frr(conn):
    v4, v6 = [], []
    rc, out = conn.exec(["ip", "-o", "addr", "show", "dev", "lo"], timeout=10)
    for fam, a in re.findall(r"\b(inet6?)\s+(\S+)", out):
        ip = _ip(a.split("/")[0])
        if _keep(ip):
            (v6 if fam == "inet6" else v4).append(str(ip))
    los = [{"name": "lo", "v4": v4, "v6": v6}] if v4 or v6 else []
    cfg = _run(conn, "show running-config")
    srgb = _srgb(cfg)
    psid = parse_prefix_sids_frr(cfg, srgb)
    locs = parse_locators_frr(_json(conn, "show segment-routing srv6 locator json"))
    adj = parse_adj_frr(_json(conn, "show isis neighbor detail json"))
    return {"loopbacks": los, "srgb": list(srgb) if psid else None, "prefix_sids": psid,
            "locators": locs, "adj": adj}


def collect_srl(conn):
    v4, v6 = [], []
    for fam in ("ipv4", "ipv6"):
        rc, out = conn.exec(["sr_cli", "-d", "info from state /interface system0 subinterface * %s address * | as json" % fam],
                            timeout=25)
        for p in re.findall(r'"ip-prefix"\s*:\s*"([^"]+)"', out):
            ip = _ip(p.split("/")[0])
            if _keep(ip):
                (v6 if ip.version == 6 else v4).append(str(ip))
    los = [{"name": "system0", "v4": v4, "v6": v6}] if v4 or v6 else []
    return {"loopbacks": los, "srgb": None, "prefix_sids": [], "locators": [], "adj": []}


COLLECTORS = {"cisco_xr": collect_xr, "cisco_xe": collect_xe, "cisco_nxos": collect_nxos,
              "frr": collect_frr, "srl": collect_srl}


def collect(conn, platform):
    fn = COLLECTORS.get(platform)
    return fn(conn) if fn else None
