#!/usr/bin/env python3
"""
Live protocol state for the topology map.

While someone has a lab's topology view open, a poller thread for that lab
collects, and the map colours itself from:

  every 3 s    per container interface (one nsenter per container): admin/oper
               state, netem, byte counters -> link up/down and bits per second
               each way. Works the same for VMs (vrnetlab ethN) and native
               containers.
  every ~10 s  per router: IGP adjacencies (IS-IS, OSPF), BGP sessions and the
               router's IPv4 addresses (to tell which node a BGP peer is).
               IOS-XE / IOS-XR / NX-OS over one persistent SSH session per
               router (netmiko); FRR and SR Linux through docker exec, with
               their JSON output.

The poller stops, and closes its SSH sessions, IDLE_STOP seconds after the
last request - an unwatched lab costs nothing.

Adjacencies are tied to links by interface: each platform's interface name is
mapped back to the containerlab port (Gi2 -> eth1 on IOS-XE, Gi0/0/0/0 -> eth1
on IOS-XR, Eth1/1 -> eth1 on NX-OS, ethernet-1/1.0 -> e1-1 on SR Linux).
"""

import collections
import concurrent.futures
import json
import re
import threading
import time

import devcfg
import linkctl

FAST = 3.0
RATE_WINDOW = 12.0
SLOW = 10.0
IDLE_STOP = 45.0
UP_WORDS = ("up", "full", "established")

IP_RE = r"\d+\.\d+\.\d+\.\d+"


# --------------------------------------------------------------------------
# interface name -> clab port
# --------------------------------------------------------------------------

def to_port(platform, ifname):
    s = (ifname or "").strip()
    if platform == "frr":
        m = re.match(r"^(eth\d+)", s)
        return m.group(1) if m else None
    if platform == "srl":
        m = re.match(r"^ethernet-1/(\d+)", s)
        return "e1-%s" % m.group(1) if m else None
    if platform == "cisco_xe":
        m = re.match(r"^(?:Gi|GigabitEthernet)\s*(\d+)$", s)
        return "eth%d" % (int(m.group(1)) - 1) if m else None
    if platform == "cisco_xr":
        m = re.match(r"^(?:Gi|GigabitEthernet)\s*0/0/0/(\d+)$", s)
        return "eth%d" % (int(m.group(1)) + 1) if m else None
    if platform == "cisco_nxos":
        m = re.match(r"^(?:Eth|Ethernet)\s*1/(\d+)$", s)
        return "eth%s" % m.group(1) if m else None
    return None


# --------------------------------------------------------------------------
# text parsers (IOS family)
# --------------------------------------------------------------------------

def _is_up(state):
    return any(w in (state or "").lower() for w in UP_WORDS)


def parse_isis_xe(text):
    """IOS-XE `show isis neighbors`: System Id  Type Interface  IP  State ..."""
    out = []
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 5 and re.match(r"^L[12]|^L1L2", f[1]) and f[2][:2] in ("Gi", "Te", "Et", "Fa"):
            out.append({"proto": "isis", "peer": f[0], "iface": f[2], "state": f[4]})
    return out


def parse_isis_xr(text):
    """IOS-XR `show isis adjacency`: System Id  Interface  SNPA  State ..."""
    out = []
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 4 and re.match(r"^(Gi|Te|Hu|Fo|BE)", f[1]) and f[3] in ("Up", "Init", "Down", "Failed"):
            out.append({"proto": "isis", "peer": f[0], "iface": f[1], "state": f[3]})
    return out


def parse_isis_nxos(text):
    """NX-OS `show isis adjacency`: System ID  SNPA  Level  State  Hold  Interface"""
    out = []
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 6 and re.match(r"^(Eth|Ethernet)", f[-1]) and f[3].upper() in ("UP", "INIT", "DOWN"):
            out.append({"proto": "isis", "peer": f[0], "iface": f[-1], "state": f[3]})
    return out


def parse_ospf_ios(text):
    """`show ip ospf neighbor` (XE), `show ospf neighbor` (XR), NX-OS alike:
    Neighbor ID  Pri  State  [Dead|Up] Time  Address  Interface"""
    out = []
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 5 and re.match("^%s$" % IP_RE, f[0]) and "/" in " ".join(f[2:4]):
            state = f[2] if "/" in f[2] else f[2] + f[3]
            out.append({"proto": "ospf", "peer": f[0], "iface": f[-1], "state": state.split("/")[0]})
    return out


def parse_bgp_ios(text, vrf=None):
    """Summary tables of IOS-XE / IOS-XR / NX-OS: rows start with the neighbour
    address; AS is the third column, Up/Down the second last, the last is the
    prefix count when Established, else the state."""
    out = []
    af = None
    for line in text.splitlines():
        m = re.match(r"^\s*(?:For address family|Address Family):\s*(.+?)\s*$", line)
        if m:
            af = m.group(1)
            continue
        m = re.match(r"^\s*VRF:?\s*(\S+)", line)
        if m and "Neighbor" not in line:
            vrf = m.group(1).strip('",')
        f = line.split()
        if len(f) >= 9 and re.match("^%s$" % IP_RE, f[0]) and f[2].isdigit():
            last = f[-1]
            established = last.isdigit()
            out.append({"peer_ip": f[0], "asn": int(f[2]), "uptime": f[-2],
                        "state": "Established" if established else " ".join(f[9:]) or last,
                        "pfx": int(last) if established else None, "af": af,
                        "vrf": vrf if vrf not in (None, "default") else None})
    # the same session appears once per address family
    seen, res = {}, []
    for s in out:
        k = (s["peer_ip"], s["vrf"])
        if k in seen:
            prev = seen[k]
            if s["pfx"] is not None:
                prev["pfx"] = (prev["pfx"] or 0) + s["pfx"]
            prev["af"] = ", ".join(x for x in (prev["af"], s["af"]) if x)
            continue
        seen[k] = s
        res.append(s)
    return res


def parse_ips_ios(text):
    return sorted(set(re.findall(r"\s(%s)\s" % IP_RE, " " + text + " ")))


# --------------------------------------------------------------------------
# per-platform collectors
# --------------------------------------------------------------------------

IOS_CMDS = {
    "cisco_xe": [("isis", "show isis neighbors", parse_isis_xe),
                 ("ospf", "show ip ospf neighbor", parse_ospf_ios),
                 ("bgp", "show bgp all summary", parse_bgp_ios),
                 ("ips", "show ip interface brief", lambda t: parse_ips_ios(
                     "\n".join(l for l in t.splitlines() if not l.startswith("GigabitEthernet1 "))))],
    "cisco_xr": [("isis", "show isis adjacency", parse_isis_xr),
                 ("ospf", "show ospf neighbor", parse_ospf_ios),
                 ("bgp", "show bgp all all summary", parse_bgp_ios),
                 ("bgpvrf", "show bgp vrf all summary", parse_bgp_ios),
                 ("ips", "show ipv4 vrf all interface brief", parse_ips_ios)],
    "cisco_nxos": [("isis", "show isis adjacency", parse_isis_nxos),
                   ("ospf", "show ip ospf neighbors", parse_ospf_ios),
                   ("bgp", "show bgp all summary vrf all", parse_bgp_ios),
                   ("ips", "show ip interface brief vrf all", parse_ips_ios)],
}
NOT_RUNNING = re.compile(r"(not running|not enabled|Invalid input|% ?Invalid|not configured|"
                         r"No such|does not exist|BGP not active|Instance .* not found)", re.I)


def collect_ios(conn, platform):
    igp, bgp, ips = [], [], []
    for what, cmd, parser in IOS_CMDS[platform]:
        try:
            text = conn.send_command(cmd, read_timeout=30)
        except Exception as exc:                        # noqa: BLE001
            raise RuntimeError("%s: %s" % (cmd, str(exc).splitlines()[0][:120]))
        if NOT_RUNNING.search(text[:300]) and what != "ips":
            continue
        res = parser(text)
        if what in ("isis", "ospf"):
            igp += res
        elif what.startswith("bgp"):
            known = {(b["peer_ip"], b["vrf"]) for b in bgp}
            bgp += [b for b in res if (b["peer_ip"], b["vrf"]) not in known]
        else:
            ips = res
    return igp, bgp, ips


def _vtysh_json(conn, cmd):
    rc, out = conn.exec(["vtysh", "-c", cmd], timeout=20)
    try:
        return json.loads(out[out.index("{"):]) if "{" in out else {}
    except ValueError:
        return {}


def collect_frr(conn, cname):
    igp, bgp = [], []
    d = _vtysh_json(conn, "show isis neighbor json")
    for area in d.get("areas") or []:
        for c in area.get("circuits") or []:
            if c.get("adj"):
                igp.append({"proto": "isis", "peer": c["adj"], "iface": c.get("interface"),
                            "state": c.get("state") or "?"})
    d = _vtysh_json(conn, "show ip ospf neighbor json")
    for rid, lst in (d.get("neighbors") or {}).items():
        for n in lst if isinstance(lst, list) else [lst]:
            iface = str(n.get("ifaceName") or n.get("interfaceName") or "").split(":")[0]
            st = str(n.get("nbrState") or n.get("converged") or "?").split("/")[0]
            igp.append({"proto": "ospf", "peer": rid, "iface": iface, "state": st})
    d = _vtysh_json(conn, "show bgp vrf all summary json")
    for vrf, afs in d.items():
        if not isinstance(afs, dict):
            continue
        for af, body in afs.items():
            if not isinstance(body, dict):
                continue
            for ip, p in (body.get("peers") or {}).items():
                if not re.match("^%s$" % IP_RE, ip):
                    continue
                prev = next((b for b in bgp if b["peer_ip"] == ip and b["vrf"] == (None if vrf == "default" else vrf)), None)
                if prev:
                    prev["af"] += ", " + af
                    if p.get("pfxRcd") is not None:
                        prev["pfx"] = (prev["pfx"] or 0) + int(p.get("pfxRcd") or 0)
                    continue
                bgp.append({"peer_ip": ip, "asn": p.get("remoteAs"), "state": p.get("state"),
                            "pfx": p.get("pfxRcd"), "uptime": p.get("peerUptime"), "af": af,
                            "vrf": None if vrf == "default" else vrf, "desc": p.get("desc")})
    rc, out = conn.exec(["ip", "-4", "-o", "addr"], timeout=10)
    ips = sorted(set(re.findall(r"inet (%s)/" % IP_RE, out)) - {"127.0.0.1"})
    return igp, bgp, ips


def _srl_json(conn, path):
    rc, out = conn.exec(["sr_cli", "-d", "info from state %s | as json" % path], timeout=25)
    try:
        return json.loads(out[out.index("{"):]) if "{" in out else {}
    except ValueError:
        return {}


def _walk(x, fn, ctx=None):
    ctx = dict(ctx or {})
    if isinstance(x, dict):
        for k in ("name", "interface-name", "instance-name"):
            if isinstance(x.get(k), str):
                ctx.setdefault("names", []).append(x[k])
        fn(x, ctx)
        for v in x.values():
            if isinstance(v, (dict, list)):
                _walk(v, fn, ctx)
    elif isinstance(x, list):
        for v in x:
            _walk(v, fn, ctx)


def collect_srl(conn, cname):
    igp, bgp, ips = [], [], []

    def adj(proto):
        # `show ... adjacency | as json` rows: {"Interface Name", "Neighbor System Id", "State", ...}
        def fn(x, ctx):
            keys = {k.lower(): k for k in x if isinstance(k, str)}
            ik = next((keys[k] for k in keys if "interface" in k), None)
            sk = next((keys[k] for k in keys if k in ("state", "adjacency state", "adjacency-state")), None)
            if not ik or not sk or not str(x.get(ik, "")).startswith("ethernet-"):
                return
            pk = next((keys[k] for k in keys if re.search(r"(system id|neighbor|router id|rtr id)", k)), None)
            igp.append({"proto": proto, "peer": str(x.get(pk) or "?"), "iface": str(x[ik]),
                        "state": str(x[sk])})
        return fn

    def show_json(cmd):
        rc, out = conn.exec(["sr_cli", "-d", cmd + " | as json"], timeout=25)
        try:
            return json.loads(out[out.index("{"):]) if "{" in out else {}
        except ValueError:
            return {}
    _walk(show_json("show network-instance * protocols isis adjacency"), adj("isis"))
    _walk(show_json("show network-instance * protocols ospf neighbor"), adj("ospf"))

    def nb(x, ctx):
        if "peer-address" not in x or "session-state" not in x:
            return
        vrf = (ctx.get("names") or ["default"])[0]
        pfx = 0
        for a in x.get("afi-safi") or []:
            if isinstance(a, dict):
                pfx += int(a.get("received-routes") or 0)
        bgp.append({"peer_ip": x["peer-address"], "asn": x.get("peer-as"),
                    "state": str(x["session-state"]).capitalize(), "pfx": pfx,
                    "uptime": str(x.get("last-established") or "").split(" ")[0],
                    "af": None, "vrf": None if vrf == "default" else vrf,
                    "desc": x.get("description")})
    _walk(_srl_json(conn, "/network-instance * protocols bgp neighbor *"), nb)

    def ad(x, ctx):
        p = x.get("ip-prefix")
        if isinstance(p, str) and "." in p:
            ips.append(p.split("/")[0])
    _walk(_srl_json(conn, "/interface * subinterface * ipv4 address *"), ad)
    return igp, bgp, sorted(set(ips))


# --------------------------------------------------------------------------
# per-lab poller
# --------------------------------------------------------------------------

class LabPoller(threading.Thread):
    def __init__(self, lab_id, lab_index_fn, links):
        super().__init__(daemon=True)
        self.lab_id = lab_id
        self.index = lab_index_fn
        self.links = links
        self.last_access = time.time()
        self.lock = threading.Lock()
        self.snap = {"updated": 0, "proto_updated": 0, "links": {}, "nodes": {}, "bgp_edges": []}
        self.conns = {}               # node -> (conn, lock)
        self.counters = {}            # (node, if) -> deque of (t, rx, tx)
        self.rates = {}
        self.ifstate = {}
        self.proto = {}               # node -> {igp, bgp, ips, error, platform, t}
        self.seen_up = set()          # (link id) that had an IGP adjacency up
        self.stop = False
        self._last_slow = 0.0
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)

    def touch(self):
        self.last_access = time.time()

    def lab(self):
        return self.index().get(self.lab_id)

    def run(self):
        try:
            while not self.stop and time.time() - self.last_access < IDLE_STOP:
                lab = self.lab()
                if lab is None:
                    break
                t0 = time.time()
                try:
                    self._fast(lab)
                    if time.time() - self._last_slow >= SLOW:
                        self._last_slow = time.time()
                        self._slow(lab)
                    self._assemble(lab)
                except Exception as exc:              # noqa: BLE001
                    with self.lock:
                        self.snap["error"] = str(exc)[:300]
                time.sleep(max(0.5, FAST - (time.time() - t0)))
        finally:
            self.stop = True
            for node, (conn, _) in list(self.conns.items()):
                try:
                    conn.disconnect()
                except Exception:                     # noqa: BLE001
                    pass
            self.conns.clear()
            self.pool.shutdown(wait=False)

    # kernel side: states, netem, counters
    def _fast(self, lab):
        now = time.time()
        states = {}
        for c in lab.get("containers") or []:
            if c.get("state") != "running":
                continue
            st = linkctl.Links.iface_state(c["name"])
            ctr = _counters(c["name"])
            for ifn, v in st.items():
                k = (c["short"], ifn)
                v = dict(v)
                if ifn in ctr:
                    rx, tx = ctr[ifn]
                    # rate over a sliding window: routing hellos come every
                    # 3-10 s, a single poll interval would read 0 half the time
                    hist = self.counters.setdefault(k, collections.deque())
                    hist.append((now, rx, tx))
                    while len(hist) > 2 and now - hist[1][0] >= RATE_WINDOW:
                        hist.popleft()
                    t0, rx0, tx0 = hist[0]
                    if now > t0 and rx >= rx0 and tx >= tx0:
                        v["rx_bps"] = int((rx - rx0) * 8 / (now - t0))
                        v["tx_bps"] = int((tx - tx0) * 8 / (now - t0))
                    elif rx < rx0 or tx < tx0:     # counters reset (node restarted)
                        hist.clear()
                        hist.append((now, rx, tx))
                states[k] = v
        self.ifstate = states

    # routers: protocols
    def _slow(self, lab):
        jobs = {}
        for n in lab.get("nodes") or []:
            plat = devcfg.platform_of(n.get("kind"), n.get("image"))
            c = next((c for c in lab.get("containers") or [] if c.get("short") == n["name"]), None)
            if not plat or not c or c.get("state") != "running":
                self.proto.pop(n["name"], None)
                continue
            if plat in devcfg.SUPPORTED.values() and plat not in devcfg.EXEC_PLATFORMS \
                    and c.get("status") not in ("healthy", None, ""):
                self.proto[n["name"]] = {"platform": plat, "igp": [], "bgp": [], "ips": [],
                                         "error": "still booting", "t": time.time()}
                continue
            jobs[n["name"]] = self.pool.submit(self._node, lab, n["name"], c, plat)
        for name, fut in jobs.items():
            try:
                fut.result(timeout=60)
            except Exception as exc:                  # noqa: BLE001
                self.proto[name] = {"platform": None, "igp": [], "bgp": [], "ips": [],
                                    "error": str(exc)[:200], "t": time.time()}
        with self.lock:
            self.snap["proto_updated"] = time.time()

    def _node(self, lab, name, c, plat):
        try:
            if plat in devcfg.EXEC_PLATFORMS:
                conn = devcfg.ExecConn(c["name"], plat)
                igp, bgp, ips = (collect_frr if plat == "frr" else collect_srl)(conn, c["name"])
            else:
                conn, lk = self.conns.get(name) or (None, threading.Lock())
                with lk:
                    if conn is None:
                        conn = devcfg._connect(lab, name, c, plat, timeout=300)
                        self.conns[name] = (conn, lk)
                    try:
                        igp, bgp, ips = collect_ios(conn, plat)
                    except Exception:
                        self.conns.pop(name, None)
                        try:
                            conn.disconnect()
                        except Exception:             # noqa: BLE001
                            pass
                        raise
            for a in igp:
                a["port"] = to_port(plat, a.get("iface"))
            self.proto[name] = {"platform": plat, "igp": igp, "bgp": bgp, "ips": ips,
                                "error": None, "t": time.time()}
        except Exception as exc:                      # noqa: BLE001
            old = self.proto.get(name) or {}
            self.proto[name] = {"platform": plat, "igp": old.get("igp", []), "bgp": old.get("bgp", []),
                                "ips": old.get("ips", []), "error": str(exc)[:200], "t": time.time()}

    def command(self, lab, name, c, plat, cmd, timeout=40):
        """One show command on a node, over the poller's persistent session."""
        if plat in devcfg.EXEC_PLATFORMS:
            return devcfg.ExecConn(c["name"], plat).send_command(cmd, read_timeout=timeout)
        self.touch()
        conn, lk = self.conns.get(name) or (None, threading.Lock())
        with lk:
            for attempt in (1, 2):
                if conn is None:
                    conn = devcfg._connect(lab, name, c, plat, timeout=300)
                    self.conns[name] = (conn, lk)
                try:
                    return conn.send_command(cmd, read_timeout=timeout)
                except Exception:
                    # a session the router (or an idle stop) closed: log in again once
                    self.conns.pop(name, None)
                    try:
                        conn.disconnect()
                    except Exception:                 # noqa: BLE001
                        pass
                    conn = None
                    if attempt == 2:
                        raise

    def _assemble(self, lab):
        owner = {}
        for node, p in self.proto.items():
            for ip in p.get("ips") or []:
                owner.setdefault(ip, node)
        failures = self.links.status_records(lab)
        links = {}
        for l in lab.get("links") or []:
            if not (l.get("a_known") and l.get("b_known")):
                continue
            ent = {"a": self.ifstate.get((l["a"], l["a_if"])),
                   "b": self.ifstate.get((l["b"], l["b_if"])), "igp": []}
            for side in ("a", "b"):
                p = self.proto.get(l[side]) or {}
                for a in p.get("igp") or []:
                    if a.get("port") == l[side + "_if"]:
                        ent["igp"].append(dict(a, node=l[side]))
            ent["down"] = any(ent[s] and (not ent[s].get("up") or ent[s].get("oper") == "DOWN")
                              for s in ("a", "b"))
            # traffic A->B is what B receives: a vrnetlab VM's transmit is
            # redirected onto ethN by tc and never shows in ethN's tx counter
            for x, y, key in (("a", "b", "ab_bps"), ("b", "a", "ba_bps")):
                rx = (ent[y] or {}).get("rx_bps")
                ent[key] = rx if rx is not None else (ent[x] or {}).get("tx_bps")
            ent["impaired"] = any(ent[s] and ent[s].get("netem") for s in ("a", "b"))
            ent["failed"] = failures.get(linkctl.link_key(l))
            if ent["down"]:
                ent["igp_state"] = "down"
            elif any(_is_up(a["state"]) for a in ent["igp"]):
                # both ends that run an IGP on this port should see it up
                ent["igp_state"] = "up" if all(_is_up(a["state"]) for a in ent["igp"]) else "partial"
                self.seen_up.add(l["id"])
            elif ent["igp"]:
                ent["igp_state"] = "partial"
            elif l["id"] in self.seen_up:
                ent["igp_state"] = "lost"
            else:
                ent["igp_state"] = None
            links[l["id"]] = ent

        nodes, edges = {}, {}
        for node, p in self.proto.items():
            bgp = []
            for b in p.get("bgp") or []:
                b = dict(b, peer_node=owner.get(b["peer_ip"]))
                bgp.append(b)
                if b["peer_node"] and b["peer_node"] != node:
                    k = tuple(sorted((node, b["peer_node"])))
                    e = edges.setdefault(k, {"a": k[0], "b": k[1], "sessions": 0, "up": 0,
                                             "vrfs": set(), "asns": set()})
                    e["sessions"] += 1
                    e["asns"].add(b.get("asn"))
                    e["up"] += 1 if _is_up(b.get("state")) else 0
                    if b.get("vrf"):
                        e["vrfs"].add(b["vrf"])
            nodes[node] = {"platform": devcfg.PLATFORM_NAMES.get(p.get("platform"), p.get("platform")),
                           "igp": p.get("igp") or [], "bgp": bgp, "error": p.get("error"),
                           "age": round(time.time() - p.get("t", 0), 1)}
        edge_list = []
        for e in edges.values():
            # each session is seen from both ends
            e["state"] = "up" if e["up"] == e["sessions"] else ("down" if e["up"] == 0 else "partial")
            e["vrfs"] = sorted(e["vrfs"])
            # each end reports the other's AS: two different numbers = eBGP
            e["ebgp"] = len(e.pop("asns") - {None}) > 1
            edge_list.append(e)
        with self.lock:
            self.snap.update({"updated": time.time(), "links": links, "nodes": nodes,
                              "bgp_edges": edge_list, "error": None})

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.snap, default=list))


def _counters(cname):
    """{ifname: (rx_bytes, tx_bytes)} from the container's /proc/net/dev."""
    pid = linkctl._pid(cname)
    if not pid:
        return {}
    try:
        with open("/proc/%d/net/dev" % pid) as fh:
            lines = fh.read().splitlines()[2:]
    except OSError:
        return {}
    res = {}
    for line in lines:
        name, _, rest = line.partition(":")
        f = rest.split()
        if len(f) >= 9:
            res[name.strip()] = (int(f[0]), int(f[8]))
    return res


class Live:
    def poller(self, lab_id):
        """The lab's poller (started if needed) - path traces borrow its sessions."""
        self.get(lab_id)
        return self.pollers[lab_id]

    def __init__(self, lab_index_fn, links):
        self.index = lab_index_fn
        self.links = links
        self.lock = threading.Lock()
        self.pollers = {}

    def get(self, lab_id):
        with self.lock:
            p = self.pollers.get(lab_id)
            if p is None or p.stop or not p.is_alive():
                p = LabPoller(lab_id, self.index, self.links)
                self.pollers[lab_id] = p
                p.start()
            p.touch()
        return p.snapshot()
