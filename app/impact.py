#!/usr/bin/env python3
"""
Blast radius: what a failure would do, worked out before it is made.

The model is the IGP as the routers run it now:

  nodes   every router with an IGP (OSPF or IS-IS) adjacency or interface
  edges   every lab link whose adjacency is up, in both directions, each way
          weighted with the cost the router at that end puts on the interface
          (`show ip ospf interface brief`, `show isis interface`, FRR JSON...)

Shortest paths (all equal-cost ones) are computed between every pair of
routers with the links and nodes of the failure taken out, and compared with
the paths now:

  lost       no path any more - traffic between the two is black-holed
  rerouted   a different path (cost before -> after)
  ecmp       one of several equal-cost paths is gone, the others carry on

On top of that: BGP sessions whose two ends can no longer reach each other
(or whose direct link is gone), and VPNs (VRFs, from the PE-CE sessions)
whose PEs are cut off from each other.

This is a prediction from the link-state view - it does not know about LDP-IGP
sync, FRR repair paths, static routes or policy. That is why the page offers to
fail it for real and trace the affected pairs: the difference is the lesson.
"""

import heapq
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import devcfg
import livestate

COST_TTL = 60.0
MAX_PATHS = 4

_cost_cache = {}            # lab id -> (time, {node: {"proto", "costs": {port: cost}}})
_cost_lock = threading.Lock()


class ImpactError(Exception):
    pass


# --------------------------------------------------------------------------
# interface costs
# --------------------------------------------------------------------------

# XE / XR `show (ip) ospf interface brief`:
#   Interface  PID  Area  IP Address/Mask  Cost  State  Nbrs F/C
OSPF_BRIEF_IOS = re.compile(r"^(\S+)\s+\d+\s+\S+\s+[\d.]+/\d+\s+(\d+)\s+\S+", re.M)
# NX-OS: Interface  ID  Area  Cost  State  Neighbors  Status
OSPF_BRIEF_NX = re.compile(r"^\s*(\S+)\s+\d+\s+[\d.]+\s+(\d+)\s+\S+\s+\d+\s+\S+", re.M)


def _isis_blocks(text, head_re, metric_re):
    """{ifname: metric} from per-interface blocks of IS-IS output."""
    out, cur = {}, None
    for line in text.splitlines():
        m = re.match(head_re, line)
        if m:
            cur = m.group(1)
            continue
        if cur and cur not in out:
            mm = re.search(metric_re, line)
            if mm:
                out[cur] = int(mm.group(1))
    return out


def node_costs(platform, run, igp):
    """{port: cost} for one router; run(cmd) -> text."""
    raw = {}
    if platform in ("cisco_xe", "cisco_xr", "cisco_nxos"):
        if igp == "ospf":
            cmd = {"cisco_xe": "show ip ospf interface brief", "cisco_xr": "show ospf interface brief",
                   "cisco_nxos": "show ip ospf interface brief"}[platform]
            rx = OSPF_BRIEF_NX if platform == "cisco_nxos" else OSPF_BRIEF_IOS
            raw = {m.group(1): int(m.group(2)) for m in rx.finditer(run(cmd))}
        else:
            if platform == "cisco_xe":
                # "GigabitEthernet2 is up, line protocol is up" ... "Level-2 Metric: 10, Priority: 64"
                raw = _isis_blocks(run("show clns interface"), r"^(\S+) is (?:up|down|admin)",
                                   r"Level-[12] Metric:\s*(\d+)")
            elif platform == "cisco_xr":
                # "TenGigE0/0/0/0  Enabled" ... "Metric (L1/L2):  10/10"
                raw = _isis_blocks(run("show isis interface"), r"^(\S+)\s+(?:Enabled|Disabled)",
                                   r"Metric \(L1/L2\):\s*(\d+)")
            else:
                raw = _isis_blocks(run("show isis interface"), r"^(\S+), Interface status",
                                   r"Metric:? L\d (\d+)")
        return {livestate.to_port(platform, k): v for k, v in raw.items() if livestate.to_port(platform, k)}
    if platform == "frr":
        if igp == "ospf":
            try:
                d = json.loads(run("show ip ospf interface json") or "{}")
            except ValueError:
                d = {}
            d = d.get("interfaces", d)
            raw = {k: v.get("cost") for k, v in d.items() if isinstance(v, dict) and v.get("cost") is not None}
        else:
            raw = _isis_blocks(run("show isis interface detail"), r"^\s*Interface: (\S+?),", r"Metric:\s*(\d+)")
        return {k: int(v) for k, v in raw.items() if re.match(r"^eth\d+$", k)}
    return {}


def _costs(lab, poller, snap):
    """{node: {"proto": igp, "costs": {port: cost}}}, cached for COST_TTL."""
    with _cost_lock:
        hit = _cost_cache.get(lab["id"])
        if hit and time.time() - hit[0] < COST_TTL:
            return hit[1]
    cont = {c["short"]: c for c in lab.get("containers") or [] if c.get("state") == "running"}
    jobs = {}
    for n in lab.get("nodes") or []:
        name = n["name"]
        plat = devcfg.platform_of(n.get("kind"), n.get("image"))
        igp = [a.get("proto") for a in ((snap.get("nodes") or {}).get(name) or {}).get("igp") or []]
        if not plat or name not in cont or not igp:
            continue
        proto = "ospf" if "ospf" in igp else "isis"
        jobs[name] = (plat, proto)

    def one(name):
        plat, proto = jobs[name]
        try:
            costs = node_costs(plat, lambda cmd: poller.command(lab, name, cont[name], plat, cmd), proto)
        except Exception:                                  # noqa: BLE001
            costs = {}
        return name, {"proto": proto, "costs": costs, "platform": plat}

    out = {}
    if jobs:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for name, v in ex.map(one, list(jobs)):
                out[name] = v
    with _cost_lock:
        _cost_cache[lab["id"]] = (time.time(), out)
    return out


# --------------------------------------------------------------------------
# the graph
# --------------------------------------------------------------------------

def build(lab, poller, snap):
    """The IGP as it is now: {"nodes": [..], "edges": {link id: {a, b, ab, ba}}, "notes": [...]}."""
    costs = _costs(lab, poller, snap)
    live = snap.get("links") or {}
    edges, notes, assumed = {}, [], set()
    for l in lab.get("links") or []:
        if not (l.get("a_known") and l.get("b_known")):
            continue
        a, b = l["a"], l["b"]
        if a not in costs or b not in costs:
            continue
        ent = live.get(l["id"]) or {}
        if ent.get("down") or ent.get("failed"):
            continue
        igp = ent.get("igp") or []
        if igp and not any(livestate._is_up(x.get("state")) for x in igp):
            continue
        ca, cb = costs[a]["costs"].get(l["a_if"]), costs[b]["costs"].get(l["b_if"])
        if not igp and (ca is None or cb is None):
            continue            # no adjacency seen and the IGP does not run here
        if ca is None:
            assumed.add(a)
        if cb is None:
            assumed.add(b)
        edges[l["id"]] = {"a": a, "b": b, "a_if": l["a_if"], "b_if": l["b_if"],
                          "ab": ca if ca is not None else 10, "ba": cb if cb is not None else 10}
    if assumed:
        notes.append("interface cost not readable on %s - assumed 10" % ", ".join(sorted(assumed)))
    used = {e["a"] for e in edges.values()} | {e["b"] for e in edges.values()}
    return {"nodes": sorted(n for n in costs if n in used),
            "proto": {n: costs[n]["proto"] for n in costs},
            "edges": edges, "notes": notes}


def spf(g, src, drop_links=(), drop_nodes=()):
    """(dist, preds) from src; preds[n] = [(prev node, link id), ...] (all equal-cost)."""
    adj = {}
    for lid, e in g["edges"].items():
        if lid in drop_links or e["a"] in drop_nodes or e["b"] in drop_nodes:
            continue
        adj.setdefault(e["a"], []).append((e["b"], e["ab"], lid))
        adj.setdefault(e["b"], []).append((e["a"], e["ba"], lid))
    dist, preds = {src: 0}, {src: []}
    pq = [(0, src)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, 1 << 60):
            continue
        for v, c, lid in adj.get(u, []):
            nd = d + c
            if nd < dist.get(v, 1 << 60):
                dist[v], preds[v] = nd, [(u, lid)]
                heapq.heappush(pq, (nd, v))
            elif nd == dist[v] and (u, lid) not in preds[v]:
                preds[v].append((u, lid))
    return dist, preds


def paths(preds, src, dst, limit=MAX_PATHS):
    """Up to `limit` equal-cost paths, each [{"node", "link"}...] from src (link None at dst)."""
    if dst not in preds:
        return []
    out = []

    def back(n, tail):
        if len(out) >= limit:
            return
        if n == src:
            out.append([{"node": src, "link": tail[0][1] if tail else None}]
                       + [{"node": tail[i][0], "link": tail[i + 1][1] if i + 1 < len(tail) else None}
                          for i in range(len(tail))])
            return
        for p, lid in preds[n]:
            back(p, [(n, lid)] + tail)
    back(dst, [])
    return out


def _sig(p):
    return tuple(h["link"] for h in p)


def repair_path(g, plr, dst, link_id):
    """The post-convergence path from plr to dst without link_id - where TI-LFA
    steers the traffic while the network converges."""
    if plr not in g["nodes"] or dst not in g["nodes"]:
        return None
    _, preds = spf(g, plr, drop_links={link_id})
    ps = paths(preds, plr, dst, 1)
    return ps[0] if ps else None


# --------------------------------------------------------------------------
# the analysis
# --------------------------------------------------------------------------

def analyse(lab, poller, snap, fail_links=(), fail_nodes=()):
    g = build(lab, poller, snap)
    if len(g["nodes"]) < 2:
        raise ImpactError("no IGP to model - the live view sees no OSPF / IS-IS adjacencies in this lab "
                          "(is it still booting?)")
    fl, fn = set(fail_links), set(fail_nodes)
    nodes = g["nodes"]
    before = {n: spf(g, n) for n in nodes}
    after = {n: spf(g, n, fl, fn) for n in nodes if n not in fn}
    pairs = []
    load_b, load_a = {}, {}
    unchanged = 0

    def carry(load, ps):
        for p in ps:
            for h in p:
                if h["link"]:
                    load[h["link"]] = load.get(h["link"], 0) + 1.0 / len(ps)

    for s in nodes:
        for d in nodes:
            if s == d:
                continue
            db, pb_ = before[s]
            pb = paths(pb_, s, d)
            carry(load_b, pb)
            if s in fn or d in fn:
                continue
            da, pa_ = after[s]
            pa = paths(pa_, s, d)
            carry(load_a, pa)
            if not pb:
                continue
            cb, ca = db.get(d), da.get(d)
            if not pa:
                st = "lost"
            elif {_sig(p) for p in pa} == {_sig(p) for p in pb}:
                unchanged += 1
                continue
            elif ca == cb and {_sig(p) for p in pa} < {_sig(p) for p in pb}:
                st = "ecmp"
            else:
                st = "rerouted"
            pairs.append({"src": s, "dst": d, "status": st, "cost_before": cb, "cost_after": ca,
                          "before": pb, "after": pa})
    order = {"lost": 0, "rerouted": 1, "ecmp": 2}
    pairs.sort(key=lambda p: (order[p["status"]], p["src"], p["dst"]))
    shift = []
    for lid in set(load_b) | set(load_a):
        if lid in fl:
            continue
        b, a = load_b.get(lid, 0), load_a.get(lid, 0)
        if a - b > 0.01:
            shift.append({"link": lid, "before": round(b, 2), "after": round(a, 2)})
    shift.sort(key=lambda x: -(x["after"] - x["before"]))

    bgp, vpns = _bgp(lab, g, snap, after, fl, fn)
    return {"failed_links": sorted(fl), "failed_nodes": sorted(fn), "nodes": nodes,
            "pairs": pairs, "unchanged": unchanged, "routers_lost": sorted(fn & set(nodes)),
            "load": shift, "bgp": bgp, "vpns": vpns, "notes": g["notes"],
            "proto": sorted(set(g["proto"].values())), "when": time.time()}


def _links_between(lab, a, b):
    return [l["id"] for l in lab.get("links") or [] if {l["a"], l["b"]} == {a, b}]


def _bgp(lab, g, snap, after, fl, fn):
    """BGP sessions and VPNs the failure takes down."""
    sessions, seen = [], set()
    vrf_pes = {}
    for node, nd in (snap.get("nodes") or {}).items():
        for b in nd.get("bgp") or []:
            peer = b.get("peer_node")
            if b.get("vrf") and node in g["nodes"]:
                vrf_pes.setdefault(b["vrf"], set()).add(node)
            if not peer or peer == node:
                continue
            k = (tuple(sorted((node, peer))), b.get("vrf"))
            if k in seen:
                continue
            seen.add(k)
            if not livestate._is_up(b.get("state")):
                continue
            why = None
            if node in fn or peer in fn:
                why = "%s fails" % (node if node in fn else peer)
            elif node in g["nodes"] and peer in g["nodes"]:
                if peer not in after.get(node, ({}, {}))[0]:
                    why = "no IGP path between them any more"
            else:
                direct = _links_between(lab, node, peer)
                if direct and all(l in fl for l in direct):
                    why = "the link between them fails"
            sessions.append({"a": node, "b": peer, "vrf": b.get("vrf"), "lost": bool(why), "why": why,
                             "ebgp": None})
    lost = [s for s in sessions if s["lost"]]
    vpns = []
    for vrf, pes in sorted(vrf_pes.items()):
        pes = sorted(pes)
        cut = []
        for i, a in enumerate(pes):
            for b in pes[i + 1:]:
                if a in fn or b in fn or b not in after.get(a, ({}, {}))[0]:
                    cut.append([a, b])
        sites = [s for s in lost if s["vrf"] == vrf]
        if cut or sites:
            vpns.append({"vrf": vrf, "pes": pes, "pe_pairs_cut": cut,
                         "sites_lost": [[s["a"], s["b"]] for s in sites]})
    return {"total": len(sessions), "lost": lost}, vpns
