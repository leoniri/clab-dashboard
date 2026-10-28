#!/usr/bin/env python3
"""
Failure playbooks and the flight recorder.

A playbook is a script of failures run against a live lab, the same way every
time:

  fail          one link, or several together (a shared-risk group: one
                fibre duct, cut at once) - shutdown or cut
  restore       some links, or everything this run broke
  flap          a link down / up N times
  node_fail     a router fails: freeze (docker pause - it goes silent, its
                neighbours notice only when hellos time out), or all its
                links cut / shut
  node_restore  bring it back
  impair        netem on a link (delay, loss, ...), or clear it
  wait          N seconds

Optionally a probe runs from a Linux / FRR node (prober.py, as for convergence
tests) and some source -> destination pairs are traced over and over.

While it runs the recorder keeps a timeline:

  step     what the playbook did, when (host clock)
  kernel   container interfaces going down / up (polled every 0.5 s)
  live     IGP adjacencies and BGP sessions as the live view sees them
           (polled - a few seconds behind)
  path     every change of a watched path (and the result: reached / dropped)
  fib      a router's FIB entry for a watched destination updated (IOS-XR
           `show cef` keeps the time, to the millisecond)
  log      after the run, each router's own syslog for the run's window:
           adjacency, interface, BGP, LDP, BFD messages, timestamps corrected
           by the router's clock offset (measured with `show clock`)
  spf      SPF runs (OSPF statistics / IS-IS spf-log), when the router says
  probe    traffic loss, from the probe

Everything a run broke is put back at the end, whatever happens. Stored per lab
under <lab dir>/.clabd-experiments/<lab>/: playbooks.json, runs/<id>.json.
"""

import calendar
import json
import os
import re
import statistics
import subprocess
import threading
import time

import convergence
import devcfg
import linkctl
import livestate
import pathtrace

DIR = ".clabd-experiments"
ID_RE = re.compile(r"^[a-z0-9]{6,20}$")
MAX_STEPS = 40
MAX_TOTAL = 900
MAX_WATCH = 4
KEEP_RUNS = 40
OPS = ("fail", "restore", "flap", "node_fail", "node_restore", "impair", "wait")

CURRENT = {"run": None}
LOCK = threading.Lock()


class ExpError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


class Stopped(Exception):
    pass


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

def _root(lab):
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", lab.get("name") or "lab")
    return os.path.join(os.path.dirname(lab["path"]), DIR, name)


def _write(lab, rel, obj):
    root = _root(lab)
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    tmp = full + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh)
    os.replace(tmp, full)
    convergence._own(os.path.dirname(root), lab)


def load(lab):
    try:
        with open(os.path.join(_root(lab), "playbooks.json")) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return []


def _link(lab, x):
    if not isinstance(x, dict):
        raise ExpError("bad link")
    try:
        l = linkctl.Links.find_link(lab, x)
    except linkctl.LinkError as exc:
        raise ExpError(str(exc))
    return {k: l[k] for k in ("a", "a_if", "b", "b_if")}


def _int(v, lo, hi, what):
    try:
        v = int(float(v))
    except (TypeError, ValueError):
        raise ExpError("%s must be a number" % what)
    if v < lo or v > hi:
        raise ExpError("%s must be %d to %d" % (what, lo, hi))
    return v


def validate(lab, pb):
    names = {n["name"] for n in lab.get("nodes") or []}
    out = {"id": pb.get("id") or os.urandom(5).hex(), "name": str(pb.get("name") or "").strip()[:60],
           "notes": str(pb.get("notes") or "")[:500]}
    if not out["name"]:
        raise ExpError("give the playbook a name")
    if not ID_RE.match(out["id"]):
        raise ExpError("bad playbook id")
    steps = pb.get("steps") or []
    if not isinstance(steps, list) or not steps:
        raise ExpError("add at least one step")
    if len(steps) > MAX_STEPS:
        raise ExpError("%d steps at most" % MAX_STEPS)
    clean = []
    for i, s in enumerate(steps):
        if not isinstance(s, dict) or s.get("op") not in OPS:
            raise ExpError("step %d: unknown action" % (i + 1))
        op = s["op"]
        c = {"op": op}
        try:
            if op in ("fail", "restore", "flap", "impair"):
                ls = s.get("links") or []
                if op == "restore" and not ls:
                    c["links"] = []             # everything this run broke
                else:
                    if not ls:
                        raise ExpError("pick a link")
                    if op in ("flap", "impair") and len(ls) != 1:
                        raise ExpError("one link")
                    c["links"] = [_link(lab, x) for x in ls[:16]]
            if op in ("fail", "flap"):
                c["mode"] = s.get("mode") if s.get("mode") in ("shutdown", "cut") else "shutdown"
            if op == "fail" and len(c["links"]) > 1:
                c["label"] = str(s.get("label") or "").strip()[:40]
            if op == "flap":
                c["count"] = _int(s.get("count", 3), 1, 20, "flaps")
                c["down_s"] = _int(s.get("down_s", 5), 1, 120, "down time")
                c["up_s"] = _int(s.get("up_s", 10), 1, 120, "up time")
            if op in ("node_fail", "node_restore"):
                if s.get("node") not in names:
                    raise ExpError("pick a node")
                c["node"] = s["node"]
            if op == "node_fail":
                c["mode"] = s.get("mode") if s.get("mode") in ("freeze", "cut", "shutdown") else "freeze"
            if op == "impair":
                c["direction"] = s.get("direction") if s.get("direction") in ("both", "a", "b") else "both"
                p = s.get("params") or {}
                c["params"] = {}
                for k, (_, _, mx) in linkctl.IMPAIR.items():
                    v = p.get(k)
                    if v in (None, "", 0, "0"):
                        continue
                    try:
                        v = float(v)
                    except (TypeError, ValueError):
                        raise ExpError("%s must be a number" % k)
                    if v < 0 or v > mx:
                        raise ExpError("%s out of range" % k)
                    c["params"][k] = v
            if op == "wait":
                c["s"] = _int(s.get("s", 10), 1, 600, "wait")
        except ExpError as exc:
            raise ExpError("step %d (%s): %s" % (i + 1, op.replace("_", " "), exc))
        clean.append(c)
    out["steps"] = clean
    out["settle_s"] = _int(pb.get("settle_s", 10), 0, 120, "settle time")
    probe = pb.get("probe")
    if isinstance(probe, dict) and probe.get("src"):
        if probe["src"] not in convergence.probe_nodes(lab):
            raise ExpError("the probe must start from an FRR or Linux node")
        dst = str(probe.get("dst") or "").strip()
        if dst not in names and not convergence.is_ip(dst):
            raise ExpError("the probe destination is a node name or an IP address")
        out["probe"] = {"src": probe["src"], "dst": dst,
                        "interval_ms": _int(probe.get("interval_ms", 10), 1, 100, "probe interval")}
    else:
        out["probe"] = None
    watch = []
    for w in (pb.get("watch") or [])[:MAX_WATCH]:
        if isinstance(w, dict) and w.get("src") in names and w.get("dst") in names and w["src"] != w["dst"]:
            watch.append({"src": w["src"], "dst": w["dst"]})
    out["watch"] = watch
    if duration(out) > MAX_TOTAL:
        raise ExpError("a playbook can run %d s at most (this one: %d s)" % (MAX_TOTAL, duration(out)))
    return out


def duration(pb):
    t = 0
    for s in pb["steps"]:
        if s["op"] == "wait":
            t += s["s"]
        elif s["op"] == "flap":
            t += s["count"] * (s["down_s"] + s["up_s"])
    return t + pb.get("settle_s", 0)


def save(lab, pb):
    pb = validate(lab, pb)
    lst = [x for x in load(lab) if x.get("id") != pb["id"]] + [pb]
    _write(lab, "playbooks.json", lst)
    return pb


def delete(lab, pid):
    lst = load(lab)
    if not any(x.get("id") == pid for x in lst):
        raise ExpError("no such playbook", 404)
    _write(lab, "playbooks.json", [x for x in lst if x.get("id") != pid])
    return {"ok": True}


def runs(lab, full_id=None):
    d = os.path.join(_root(lab), "runs")
    if full_id:
        if not ID_RE.match(full_id or ""):
            raise ExpError("bad run id")
        try:
            with open(os.path.join(d, full_id + ".json")) as fh:
                return json.load(fh)
        except OSError:
            raise ExpError("no such run", 404)
    out = []
    try:
        names = sorted((f for f in os.listdir(d) if f.endswith(".json")), reverse=True)
    except OSError:
        return out
    for fn in names:
        try:
            with open(os.path.join(d, fn)) as fh:
                r = json.load(fh)
        except (OSError, ValueError):
            continue
        out.append({k: r.get(k) for k in ("id", "playbook", "when", "duration", "summary", "ok", "error")})
    return out


def delete_run(lab, rid):
    if not ID_RE.match(rid or ""):
        raise ExpError("bad run id")
    try:
        os.remove(os.path.join(_root(lab), "runs", rid + ".json"))
    except OSError:
        raise ExpError("no such run", 404)
    return {"ok": True}


# --------------------------------------------------------------------------
# router clocks and logs
# --------------------------------------------------------------------------

MONTHS = {m: i + 1 for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split())}
CLOCK_RE = re.compile(r"(\d+):(\d+):(\d+)(?:\.(\d+))?\s+\S+\s+\w{3}\s+(\w{3})\s+(\d+)\s+(\d{4})")
STAMP_RE = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d+)\s+(?:(\d{4})\s+)?"
                      r"(\d+):(\d+):(\d+)(?:\.(\d+))?")
MSG_RE = re.compile(r"%([A-Z][A-Z0-9_]*(?:-[A-Z][A-Z0-9_]*)*)-(\d)-([A-Z0-9_]+)\s*:?\s*(.*)")
IF_RE = re.compile(r"\b((?:TenGigE|TenGigabitEthernet|GigabitEthernet|HundredGigE|FortyGigE|TwentyFiveGigE|"
                   r"Ethernet|Gi|Te|Hu|Eth)\s?[\d/]+\d)\b")
LOG_FILTER = "ADJCHG|ADJCHANGE|UPDOWN|CHANGED|LDP|BFD|BGP|NBRCHG|ISIS|OSPF|CLNS|SPF"
SKIP_MSG = re.compile(r"CONFIG|SSH|SEC_LOGIN|LOGIN|SYS-5|AAA|SECURITY", re.I)


def _naive(mon, day, year, hh, mm, ss, frac):
    t = calendar.timegm((year, MONTHS[mon], int(day), int(hh), int(mm), int(ss)))
    return t + (int(frac) / (10 ** len(frac)) if frac else 0.0)


def clock_offset(conn, platform):
    """host time - router time (seconds), from `show clock` timed on the host."""
    t0 = time.time()
    out = conn.send_command("show clock", read_timeout=20)
    t1 = time.time()
    m = CLOCK_RE.search(out)
    if not m:
        return None, None
    hh, mm, ss, frac, mon, day, year = m.groups()
    if mon not in MONTHS:
        return None, None
    r = _naive(mon, day, int(year), hh, mm, ss, frac)
    return (t0 + t1) / 2 - r, int(year)


def router_time(stamp_m, offset, year):
    mon, day, y, hh, mm, ss, frac = stamp_m.groups()
    yr = int(y) if y else year
    return _naive(mon, day, yr, hh, mm, ss, frac) + offset


def classify(fac, mnem):
    f = fac.upper()
    if "LINK" in f or "LINEPROTO" in f:
        return "link"
    if "OSPF" in f or "ISIS" in f or "CLNS" in f:
        return "igp"
    if "BGP" in f:
        return "bgp"
    if "LDP" in f:
        return "ldp"
    if "BFD" in f:
        return "bfd"
    return "other"


def parse_logs(text, offset, year, node, platform, lab, t_from, t_to):
    out = []
    for line in text.splitlines():
        sm, mm = STAMP_RE.search(line), MSG_RE.search(line)
        if not sm or not mm or SKIP_MSG.search(mm.group(1) + "-" + mm.group(3)):
            continue
        t = router_time(sm, offset, year)
        if not (t_from <= t <= t_to):
            continue
        fac, sev, mnem, msg = mm.groups()
        ev = {"at": t, "src": "log", "kind": classify(fac, mnem), "node": node,
              "code": "%s-%s" % (fac, mnem), "text": re.sub(r",?\s*vrf default vrfid 0x[0-9a-f]+", "",
                                                           msg.strip()).replace("VRF 'default' (0x60000000), ", "")[:240]}
        im = IF_RE.search(msg)
        if im:
            port = livestate.to_port(platform, im.group(1).replace(" ", ""))
            for l in lab.get("links") or []:
                if (l["a"] == node and l["a_if"] == port) or (l["b"] == node and l["b_if"] == port):
                    ev["link"] = l["id"]
        out.append(ev)
    return out


def parse_spf(text, platform, node, t_cmd, t_from, t_to):
    """SPF runs. OSPF statistics give 'how long ago' (to the second);
    IOS-XR IS-IS spf-log gives the time of day."""
    out = []
    day = None
    for line in text.splitlines():
        dm = re.match(r"^\s*---\s+\w{3}\s+(\w{3})\s+(\d+)\s+(\d{4})\s+---", line)
        if dm:
            day = dm.groups()
            continue
        m = re.match(r"^\s*(\d+):(\d+):(\d+)\s+([\d.]+)\s+(\d+)\s+(.*)$", line)       # XR OSPF
        if m and platform == "cisco_xr":
            ago = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            out.append({"at": t_cmd - ago, "approx": True, "ms": int(m.group(5)),
                        "text": "OSPF SPF (area %s, %s ms, %s)" % (m.group(4), m.group(5),
                                                                   m.group(6).strip().rstrip(", ") or "-")})
            continue
        m = re.match(r"^\s*(\d+):(\d+):(\d+)\s+((?:\d+\s+){7})(\S.*)?$", line)          # XE OSPF
        if m and platform == "cisco_xe":
            ago = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            total = m.group(4).split()[-1]
            out.append({"at": t_cmd - ago, "approx": True, "ms": int(total),
                        "text": "OSPF SPF (%s ms, %s)" % (total, (m.group(5) or "-").strip())})
            continue
        m = re.match(r"^\s*(\d+:\d+:\d+\.\d+)\s+(\w+)\s+(\d+)\s+\d+\s+\d+\s*(\S*)\s*(.*)$", line)  # XR IS-IS
        if m and day and day[0] in MONTHS:
            hh, mi, ss = m.group(1).split(":")
            s, _, frac = ss.partition(".")
            out.append({"at": _naive(day[0], day[1], int(day[2]), hh, mi, s, frac), "router_clock": True,
                        "ms": int(m.group(3)), "text": "IS-IS %s (%s ms) %s" % (m.group(2), m.group(3),
                                                                                m.group(5).strip())})
    for e in out:
        e.update(src="spf", kind="spf", node=node)
    return [e for e in out if e.get("router_clock") or t_from <= e["at"] <= t_to]


def collect_router_side(lab, t_from, t_to, log):
    """Syslog + SPF + clock offset from every VM router, after the run."""
    events, offsets = [], {}
    cont = {c["short"]: c for c in lab.get("containers") or [] if c.get("state") == "running"}
    jobs = []
    for n in lab.get("nodes") or []:
        plat = devcfg.platform_of(n.get("kind"), n.get("image"))
        if plat in ("cisco_xe", "cisco_xr", "cisco_nxos") and n["name"] in cont:
            jobs.append((n["name"], plat))
    lock = threading.Lock()

    def one(name, plat):
        try:
            _, _, c, _ = devcfg.resolve({lab["id"]: lab}, lab["id"], name)
            conn = devcfg._connect(lab, name, c, plat)
            try:
                off, year = clock_offset(conn, plat)
                if off is None:
                    log("%s: could not read the clock - its log is left out" % name)
                    return
                # IOS-XR reads a `|` in an include pattern as the next pipe
                cmd = ('show logging | utility egrep "%s"' if plat == "cisco_xr"
                       else "show logging | include %s") % LOG_FILTER
                logs = conn.send_command(cmd, read_timeout=60)
                evs = parse_logs(logs, off, year, name, plat, lab, t_from, t_to)
                t_cmd = time.time()
                spf_cmd = {"cisco_xr": "show ospf statistics spf", "cisco_xe": "show ip ospf statistics"}.get(plat)
                spf = []
                if spf_cmd:
                    spf = parse_spf(conn.send_command(spf_cmd, read_timeout=30), plat, name, t_cmd, t_from, t_to)
                if plat == "cisco_xr":
                    isis = parse_spf(conn.send_command("show isis spf-log last 30", read_timeout=30),
                                     plat, name, t_cmd, t_from, t_to)
                    for e in isis:
                        if e.pop("router_clock", None):
                            e["at"] += off
                    spf += [e for e in isis if t_from <= e["at"] <= t_to]
            finally:
                conn.disconnect()
            with lock:
                offsets[name] = round(off * 1000)
                events.extend(evs + spf)
            log("%s: %d log message(s), %d SPF run(s); clock %+d ms from the host"
                % (name, len(evs), len(spf), round(off * 1000)))
        except Exception as exc:                        # noqa: BLE001
            log("%s: could not read its log - %s" % (name, str(exc)[:160]))

    ts = [threading.Thread(target=one, args=j, daemon=True) for j in jobs]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=150)
    return events, offsets


# --------------------------------------------------------------------------
# the recorder
# --------------------------------------------------------------------------

def _linkstate(lab, snap, links):
    ls = {lid: {"down": e.get("down"), "igp_state": e.get("igp_state"), "failed": e.get("failed")}
          for lid, e in (snap.get("links") or {}).items()}
    for l in lab.get("links") or []:
        rec = links.state.get("%s|%s" % (lab.get("name"), linkctl.link_key(l)))
        if rec:
            ls.setdefault(l["id"], {})["failed"] = rec
    return ls


def _path_sig(tr):
    return (tr.get("result"), tuple((h.get("node"), h.get("link")) for h in tr.get("hops") or []))


class Recorder:
    def __init__(self, index_fn, lab_id, live, links, watch, frozen=None):
        self.index, self.lab_id, self.live, self.links = index_fn, lab_id, live, links
        self.frozen = frozen if frozen is not None else {}      # node -> container (the runner's)
        self.watch = watch
        self.events = []
        self.changes = []           # [t(abs), link id, key, value]
        self.paths = {}             # "src>dst" -> [{at, result, hops}]
        self.fib_seen = {}
        self.slow = []              # traces thrown away: [start, seconds, pair]
        self.lock = threading.Lock()
        self.stop_ev = threading.Event()
        self.kern = {}              # (node, port) -> up
        self.linkst = {}            # link id -> {"down", "igp"}
        self.bgp = {}
        self.initial = None
        self.threads = []

    def lab(self):
        return self.index().get(self.lab_id)

    def add(self, ev):
        with self.lock:
            self.events.append(ev)

    def start(self):
        self._kernel(first=True)
        self._live(first=True)
        for fn in (self._kernel_loop, self._live_loop, self._path_loop):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self.threads.append(t)

    def stop(self):
        self.stop_ev.set()
        for t in self.threads:
            t.join(timeout=90)

    # interfaces, from the kernel
    def _kernel(self, first=False):
        lab = self.lab()
        if not lab:
            return
        now = time.time()
        ports = {}
        for l in lab.get("links") or []:
            if l.get("a_known") and l.get("b_known"):
                ports.setdefault(l["a"], []).append((l["a_if"], l))
                ports.setdefault(l["b"], []).append((l["b_if"], l))
        down = {}
        for c in lab.get("containers") or []:
            if c.get("short") not in ports:
                continue
            st = linkctl.Links.iface_state(c["name"]) if c.get("state") in ("running", "paused") else {}
            for port, l in ports[c["short"]]:
                s = st.get(port)
                up = bool(s and s.get("up") and s.get("oper") != "DOWN")
                k = (c["short"], port)
                if not first and self.kern.get(k) is not None and self.kern[k] != up:
                    self.add({"at": now, "src": "kernel", "kind": "link", "node": c["short"], "link": l["id"],
                              "text": "%s %s %s" % (c["short"], port, "up" if up else "down")})
                self.kern[k] = up
                if not up:
                    down[l["id"]] = True
        for l in lab.get("links") or []:
            if not (l.get("a_known") and l.get("b_known")):
                continue
            d = bool(down.get(l["id"]))
            prev = self.linkst.setdefault(l["id"], {}).get("down")
            if prev is None or prev != d:
                self.linkst[l["id"]]["down"] = d
                with self.lock:
                    self.changes.append([now, l["id"], "down", d])

    def _kernel_loop(self):
        while not self.stop_ev.wait(0.5):
            try:
                self._kernel()
            except Exception:                           # noqa: BLE001
                pass

    # adjacencies and sessions, from the live view
    def _live(self, first=False):
        snap = self.live.get(self.lab_id) or {}
        now = time.time()
        for lid, e in (snap.get("links") or {}).items():
            igp = e.get("igp_state")
            prev = self.linkst.setdefault(lid, {}).get("igp", "?")
            if prev != igp:
                self.linkst[lid]["igp"] = igp
                with self.lock:
                    self.changes.append([now, lid, "igp", igp])
                if not first and prev != "?":
                    l = next((x for x in (self.lab() or {}).get("links") or [] if x["id"] == lid), None)
                    self.add({"at": now, "src": "live", "kind": "igp", "link": lid, "approx": True,
                              "node": l["a"] if l else None,
                              "text": "IGP adjacency %s - %s: %s" % (
                                  l["a"] if l else "?", l["b"] if l else "?", igp or "none")})
        for e in snap.get("bgp_edges") or []:
            k = (e["a"], e["b"])
            if self.bgp.get(k) != e.get("state"):
                if not first and k in self.bgp:
                    self.add({"at": now, "src": "live", "kind": "bgp", "node": e["a"], "approx": True,
                              "text": "BGP %s - %s: %s" % (e["a"], e["b"], e.get("state"))})
                self.bgp[k] = e.get("state")
        return snap

    def _live_loop(self):
        while not self.stop_ev.wait(1.0):
            try:
                self._live()
            except Exception:                           # noqa: BLE001
                pass

    # the watched paths
    def trace_once(self, w):
        lab = self.lab()
        if not lab:
            return
        if self.frozen:
            # a frozen router is down for the trace at once - asking it would
            # hang until the session times out
            lab = dict(lab, containers=[dict(c, state="paused") if c.get("short") in self.frozen else c
                                        for c in lab.get("containers") or []])
        poller = self.live.poller(self.lab_id)
        snap = self.live.get(self.lab_id) or {}
        key = "%s>%s" % (w["src"], w["dst"])
        t = time.time()
        try:
            dst = w.get("ip") or pathtrace.node_address(lab, poller, w["dst"], w["src"])
            w["ip"] = dst
            tr = pathtrace.trace(lab, poller, w["src"], dst, None, _linkstate(lab, snap, self.links),
                                 branches=False)
        except Exception as exc:                        # noqa: BLE001
            tr = {"result": "error", "hops": [], "error": str(exc)[:200]}
        t_end = time.time()
        if self.stop_ev.is_set() and t_end - t > 5:
            return              # finished after the recording closed
        if t_end - t > 6:
            # the network changed while this trace was walking it (a router
            # froze mid-command): what it read does not describe one moment
            self.slow.append([round(t, 3), round(t_end - t, 1), key])
            return
        t = (t + t_end) / 2
        if tr.get("result") == "error":
            return              # a session dropped (a router just thawed): no reading
        lst = self.paths.setdefault(key, [])
        if not lst or _path_sig(lst[-1]) != _path_sig(tr):
            rec = {"at": t, "result": tr.get("result"), "hops": tr.get("hops") or [], "dst_ip": w.get("ip"),
                   "error": tr.get("error")}
            with self.lock:
                lst.append(rec)
            if len(lst) > 1:
                route = " > ".join(h["node"] for h in rec["hops"] if h.get("link") or h is rec["hops"][-1])
                self.add({"at": t, "src": "path", "kind": "path", "node": w["src"], "pair": key,
                          "text": "path %s → %s: %s (%s)" % (w["src"], w["dst"], route or "-", rec["result"])})
        for h in tr.get("hops") or []:
            if h.get("fib_updated"):
                k = (h["node"], h.get("lookup"))
                seen = self.fib_seen.setdefault(k, set())
                if seen and h["fib_updated"] not in seen:
                    self.add({"at": None, "router_stamp": h["fib_updated"], "src": "fib", "kind": "fib",
                              "node": h["node"], "link": h.get("link"),
                              "text": "%s FIB entry for %s updated (now out %s)" % (h["node"], h.get("lookup"),
                                                                                    h.get("out_if") or "-")})
                seen.add(h["fib_updated"])

    def _path_loop(self):
        """Trace the watched pairs in turn. Each trace gets 5 s: one that hangs
        (on a router that froze under it) is left to finish on its own, and
        the others carry on meanwhile."""
        if not self.watch:
            return
        busy, i = {}, 0
        while not self.stop_ev.is_set():
            w = self.watch[i % len(self.watch)]
            i += 1
            k = (w["src"], w["dst"])
            busy[k] = [t for t in busy.get(k, []) if t.is_alive()]
            if len(busy[k]) >= 2:           # one stuck, and its stand-in still running
                self.stop_ev.wait(0.2)
                continue
            th = threading.Thread(target=self.trace_once, args=(w,), daemon=True)
            th.start()
            busy[k].append(th)
            th.join(timeout=5)
            self.stop_ev.wait(0.2)


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------

class Probe:
    def __init__(self, lab, poller, pb):
        self.cfg = pb
        cont = next((c for c in lab.get("containers") or [] if c.get("short") == pb["src"]), None)
        if not cont or cont.get("state") != "running":
            raise ExpError("probe source %s is not running" % pb["src"])
        try:
            self.src_ip, self.dst_ip = convergence.probe_addresses(lab, poller, pb["src"], pb["dst"])
        except ValueError as exc:
            raise ExpError("probe: %s" % exc)
        pid = linkctl._pid(cont["name"])
        self.proc = subprocess.Popen([linkctl.NSENTER, "-t", str(pid), "-n", convergence.PYTHON, convergence.PROBER,
                                      self.src_ip, self.dst_ip, str(pb["interval_ms"]), str(MAX_TOTAL + 300)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.result, self.progress = {}, None
        self.th = threading.Thread(target=self._read, daemon=True)
        self.th.start()
        time.sleep(0.3)
        if self.proc.poll() is not None:
            raise ExpError("the probe did not start: %s" % (self.proc.stderr.read().strip()[-300:] or "?"))

    def _read(self):
        for line in self.proc.stdout:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("p"):
                self.progress = d
            elif d.get("done"):
                self.result.update(d)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.send_signal(15)
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.th.join(timeout=10)
        return self.result


LATE_US = 1000000            # a reply after 1 s is as good as lost


def _probe_summary(res, t0):
    """Loss and RTT in 100 ms buckets, and every outage. A reply that comes
    back after more than a second counts as lost: a frozen VM router queues
    packets and lets them all out when it wakes, many seconds late."""
    iv, start = res["interval_ms"], res["start"]
    late = sum(1 for x in res["rtt_us"] if x > LATE_US)
    rtt = [x if x <= LATE_US else -1 for x in res["rtt_us"]]
    per = max(1, int(100 / iv))
    series = []
    for i in range(0, len(rtt), per):
        chunk = rtt[i:i + per]
        ok = [x for x in chunk if x >= 0]
        series.append([round(start - t0 + i * iv / 1000.0, 2), round(1 - len(ok) / len(chunk), 2),
                       round(statistics.median(ok) / 1000, 2) if ok else None])
    outages, i, n = [], 0, len(rtt)
    while i < n:
        if rtt[i] < 0:
            j = i
            while j < n and rtt[j] < 0:
                j += 1
            outages.append({"from": round(start - t0 + i * iv / 1000, 3), "to": round(start - t0 + j * iv / 1000, 3),
                            "ms": round((j - i) * iv)})
            i = j
        else:
            i += 1
    return {"interval_ms": iv, "sent": n, "lost": sum(1 for x in rtt if x < 0), "late": late, "series": series,
            "outages": outages}


class Runner:
    def __init__(self, index_fn, lab_id, pb, links, live, audit, log):
        self.index, self.lab_id, self.pb = index_fn, lab_id, pb
        self.links, self.live, self.audit, self.log = links, live, audit, log
        self.broken = {}            # link key -> link
        self.frozen = {}            # node -> container name
        self.node_links = {}        # node -> [links]
        self.impaired = {}          # link key -> (link, direction)
        self.steps = []
        self.stop_flag = False
        self.t0 = None

    def lab(self):
        lab = self.index().get(self.lab_id)
        if lab is None:
            raise ExpError("the lab is gone")
        return lab

    def sleep(self, s):
        end = time.time() + s
        while True:
            if self.stop_flag:
                raise Stopped()
            left = end - time.time()
            if left <= 0:
                return
            time.sleep(min(0.2, left))

    def step_ev(self, at, text, kind, **kw):
        ev = {"at": at, "src": "step", "kind": kind, "text": text}
        ev.update(kw)
        self.rec.add(ev)
        self.log("t=%5.1fs  %s" % (at - self.t0, text))
        return ev

    def _mark(self, ls, failed):
        now = time.time()
        lab = self.lab()
        for x in ls:
            k = linkctl.link_key(x)
            for l in lab.get("links") or []:
                if linkctl.link_key(l) == k:
                    with self.rec.lock:
                        self.rec.changes.append([now, l["id"], "failed", failed])

    def _lid(self, x):
        k = linkctl.link_key(x)
        return next((l["id"] for l in self.lab().get("links") or [] if linkctl.link_key(l) == k), None)

    def fail(self, ls, mode, label=None):
        t = time.time()
        r = self.links.fail_many(self.index, self.lab(), ls, mode, "playbook")
        for x in ls:
            if linkctl.link_key(x) in r["failed"]:
                self.broken[linkctl.link_key(x)] = x
        self._mark([x for x in ls if linkctl.link_key(x) in r["failed"]], mode)
        what = (label + ": " if label else "") + ", ".join("%s–%s" % (x["a"], x["b"]) for x in ls)
        self.step_ev(t, "%s %s%s" % ("cut" if mode == "cut" else "shut", what,
                                     " (already down: %d)" % len(r["skipped"]) if r["skipped"] else ""),
                     "fail", links=[self._lid(x) for x in ls], took=round(time.time() - t, 2))

    def restore(self, ls):
        if not ls:
            ls = list(self.broken.values())
        ls = [x for x in ls if linkctl.link_key(x) in self.broken]
        if not ls:
            self.step_ev(time.time(), "restore: nothing is down", "restore")
            return
        t = time.time()
        self.links.restore_many(self.index, self.lab(), ls, "playbook")
        for x in ls:
            self.broken.pop(linkctl.link_key(x), None)
        self._mark(ls, None)
        self.step_ev(t, "restore " + ", ".join("%s–%s" % (x["a"], x["b"]) for x in ls), "restore",
                     links=[self._lid(x) for x in ls], took=round(time.time() - t, 2))

    def node_fail(self, node, mode):
        lab = self.lab()
        c = next((c for c in lab.get("containers") or [] if c.get("short") == node), None)
        if not c or c.get("state") != "running":
            raise ExpError("%s is not running" % node)
        t = time.time()
        if mode == "freeze":
            rc, _, err = linkctl._run([linkctl.DOCKER, "pause", c["name"]], timeout=30)
            if rc != 0:
                raise ExpError("could not freeze %s: %s" % (node, err.strip()[:200]))
            self.frozen[node] = c["name"]
            with self.rec.lock:
                self.rec.changes.append([time.time(), node, "frozen", True])
            self.step_ev(t, "%s fails silently (frozen - no carrier loss, neighbours wait for hellos)" % node,
                         "node_fail", node=node, took=round(time.time() - t, 2))
            return
        ls = [{k: l[k] for k in ("a", "a_if", "b", "b_if")} for l in lab.get("links") or []
              if (l["a"] == node or l["b"] == node) and l.get("a_known") and l.get("b_known")]
        r = self.links.fail_many(self.index, lab, ls, mode, "playbook")
        mine = [x for x in ls if linkctl.link_key(x) in r["failed"]]
        for x in mine:
            self.broken[linkctl.link_key(x)] = x
        self.node_links[node] = mine
        self._mark(mine, mode)
        with self.rec.lock:
            self.rec.changes.append([time.time(), node, "isolated", mode])
        self.step_ev(t, "%s fails - all %d links %s" % (node, len(mine), "cut" if mode == "cut" else "shut"),
                     "node_fail", node=node, links=[self._lid(x) for x in mine], took=round(time.time() - t, 2))

    def node_restore(self, node):
        t = time.time()
        if node in self.frozen:
            rc, _, err = linkctl._run([linkctl.DOCKER, "unpause", self.frozen[node]], timeout=30)
            if rc != 0:
                raise ExpError("could not unfreeze %s: %s" % (node, err.strip()[:200]))
            self.frozen.pop(node)
            with self.rec.lock:
                self.rec.changes.append([time.time(), node, "frozen", False])
            self.step_ev(t, "%s back (unfrozen)" % node, "node_restore", node=node, took=round(time.time() - t, 2))
            return
        ls = [x for x in self.node_links.pop(node, []) if linkctl.link_key(x) in self.broken]
        if not ls:
            self.step_ev(t, "%s: nothing to restore" % node, "node_restore", node=node)
            return
        self.links.restore_many(self.index, self.lab(), ls, "playbook")
        for x in ls:
            self.broken.pop(linkctl.link_key(x), None)
        self._mark(ls, None)
        with self.rec.lock:
            self.rec.changes.append([time.time(), node, "isolated", None])
        self.step_ev(t, "%s back - %d links restored" % (node, len(ls)), "node_restore", node=node,
                     links=[self._lid(x) for x in ls], took=round(time.time() - t, 2))

    def impair(self, x, direction, params):
        t = time.time()
        self.links.impair(self.lab(), x, direction, params, "playbook")
        k = linkctl.link_key(x)
        if params:
            self.impaired[k] = (x, direction)
        else:
            self.impaired.pop(k, None)
        txt = ", ".join("%s %g" % (k2.replace("_", " "), v) for k2, v in sorted(params.items())) or "cleared"
        with self.rec.lock:
            self.rec.changes.append([time.time(), self._lid(x), "impaired", txt if params else None])
        self.step_ev(t, "impair %s–%s (%s): %s" % (x["a"], x["b"], direction, txt), "impair",
                     links=[self._lid(x)], took=round(time.time() - t, 2))

    def do(self, s):
        op = s["op"]
        if op == "fail":
            self.fail(s["links"], s["mode"], s.get("label"))
        elif op == "restore":
            self.restore(s["links"])
        elif op == "flap":
            x = s["links"][0]
            for i in range(s["count"]):
                self.fail([x], s["mode"], "flap %d/%d" % (i + 1, s["count"]))
                self.sleep(s["down_s"])
                self.restore([x])
                self.sleep(s["up_s"])
        elif op == "node_fail":
            self.node_fail(s["node"], s["mode"])
        elif op == "node_restore":
            self.node_restore(s["node"])
        elif op == "impair":
            self.impair(s["links"][0], s["direction"], s["params"])
        elif op == "wait":
            self.sleep(s["s"])

    def cleanup(self):
        """Put back everything the run broke."""
        msgs = []
        for node, cname in list(self.frozen.items()):
            rc, _, err = linkctl._run([linkctl.DOCKER, "unpause", cname], timeout=30)
            msgs.append("%s unfrozen" % node if rc == 0 else "could not unfreeze %s: %s" % (node, err.strip()[:120]))
            self.frozen.pop(node, None)
        if self.broken:
            try:
                self.links.restore_many(self.index, self.lab(), list(self.broken.values()), "playbook (cleanup)")
                msgs.append("%d link(s) restored" % len(self.broken))
                self.broken.clear()
            except Exception as exc:                    # noqa: BLE001
                msgs.append("could not restore links: %s - restore them from the topology view" % exc)
        for k, (x, d) in list(self.impaired.items()):
            try:
                self.links.impair(self.lab(), x, d, {}, "playbook (cleanup)")
                msgs.append("impairment on %s–%s cleared" % (x["a"], x["b"]))
            except Exception as exc:                    # noqa: BLE001
                msgs.append("could not clear netem on %s–%s: %s" % (x["a"], x["b"], exc))
        self.impaired.clear()
        return msgs

    def run(self):
        lab = self.lab()
        if not lab.get("running"):
            raise ExpError("the lab is not running")
        pb = self.pb
        rid = time.strftime("%Y%m%d%H%M%S") + os.urandom(2).hex()
        poller = self.live.poller(self.lab_id)
        # the live view needs a first look at the lab before the clock starts
        for _ in range(15):
            snap = self.live.get(self.lab_id) or {}
            if snap.get("links") and snap.get("proto_updated"):
                break
            time.sleep(1)
        self.rec = Recorder(self.index, self.lab_id, self.live, self.links, [dict(w) for w in pb["watch"]], self.frozen)
        state = {"id": rid, "lab_id": self.lab_id, "playbook": pb, "started": time.time(),
                 "total_s": duration(pb), "step": 0, "events": self.rec.events, "runner": self}
        with LOCK:
            CURRENT["run"] = state
        self.log("%s: %d step(s), about %d s" % (pb["name"], len(pb["steps"]), duration(pb)))
        for w in self.rec.watch:
            self.rec.trace_once(w)
        probe = None
        error = None
        cleanup_msgs = []
        try:
            if pb.get("probe"):
                probe = Probe(lab, poller, pb["probe"])
                self.log("probe %s -> %s every %d ms" % (probe.src_ip, probe.dst_ip, pb["probe"]["interval_ms"]))
            self.t0 = time.time()
            self.rec.start()
            self.sleep(2)               # a quiet baseline
            for i, s in enumerate(pb["steps"]):
                state["step"] = i + 1
                self.do(s)
            if pb["settle_s"]:
                self.log("settling for %d s" % pb["settle_s"])
                self.sleep(pb["settle_s"])
        except Stopped:
            error = "stopped by the user"
            self.log("stopped - putting everything back")
        except Exception as exc:                        # noqa: BLE001
            error = str(exc)
            self.log("error: %s - putting everything back" % exc)
        finally:
            t_end = time.time()
            if self.rec.threads:
                self.rec.stop()
            probe_res = probe.stop() if probe else None
            cleanup_msgs = self.cleanup()
            for m in cleanup_msgs:
                self.log("cleanup: " + m)
        if self.t0 is None:
            with LOCK:
                CURRENT["run"] = None
            raise ExpError(error or "the run did not start")
        self.log("reading the routers' logs")
        rlog, offsets = collect_router_side(self.lab(), self.t0 - 3, t_end + 3, self.log)
        events = self.rec.events + rlog
        for e in events:
            if e.get("router_stamp"):
                # a FIB time, on the router's clock
                m = STAMP_RE.search(e.pop("router_stamp"))
                off = offsets.get(e["node"])
                if m and off is not None:
                    e["at"] = router_time(m, off / 1000.0, time.gmtime().tm_year)
        events = [e for e in events if e.get("at")]
        for e in events:
            e["t"] = round(e.pop("at") - self.t0, 3)
        # a FIB stamp from before the run says nothing about it; and IOS-XR
        # bumps an entry twice within a millisecond (route, then load info)
        last_fib, kept = {}, []
        for e in sorted(events, key=lambda e: e["t"]):
            if e["src"] == "fib":
                k = (e["node"], e["text"])
                if e["t"] < -1 or (k in last_fib and e["t"] - last_fib[k] < 0.05):
                    continue
                last_fib[k] = e["t"]
            kept.append(e)
        events = kept
        events.sort(key=lambda e: (e["t"], 0 if e["src"] == "step" else 1))
        changes = [[round(c[0] - self.t0, 3)] + c[1:] for c in self.rec.changes]
        paths = {k: [dict(p, t=round(p.pop("at") - self.t0, 3)) for p in v] for k, v in self.rec.paths.items()}
        rec = {"id": rid, "playbook": pb, "lab": lab.get("name"), "when": self.t0,
               "duration": round(t_end - self.t0, 1), "events": events, "changes": changes, "paths": paths,
               "clock_ms": offsets, "error": error, "ok": error is None, "cleanup": cleanup_msgs,
               "slow_traces": [[round(x[0] - self.t0, 2), x[1], x[2]] for x in self.rec.slow]}
        if probe_res and probe_res.get("rtt_us"):
            rec["probe"] = dict(_probe_summary(probe_res, self.t0), src=pb["probe"]["src"], dst=pb["probe"]["dst"],
                                src_ip=probe.src_ip, dst_ip=probe.dst_ip)
            for o in rec["probe"]["outages"]:
                events.append({"t": o["from"], "src": "probe", "kind": "loss",
                               "text": "traffic lost for %d ms" % o["ms"], "ms": o["ms"]})
            events.sort(key=lambda e: (e["t"], 0 if e["src"] == "step" else 1))
        rec["summary"] = self.summary(rec)
        _write(self.lab(), os.path.join("runs", rid + ".json"), rec)
        d = os.path.join(_root(self.lab()), "runs")
        for f in sorted(f for f in os.listdir(d) if f.endswith(".json"))[:-KEEP_RUNS]:
            try:
                os.remove(os.path.join(d, f))
            except OSError:
                pass
        self.audit("PLAYBOOK lab=%s playbook=%r steps=%d result=%s" % (lab.get("name"), pb["name"],
                                                                       len(pb["steps"]), error or "ok"))
        with LOCK:
            CURRENT["run"] = None
        self.log("saved run %s: %s" % (rid, rec["summary"]))
        if error:
            raise ExpError(error)
        return rec

    @staticmethod
    def summary(rec):
        ev = rec["events"]
        parts = ["%d events" % len(ev)]
        n = sum(1 for e in ev if e["kind"] == "path")
        if n:
            parts.append("%d path change%s" % (n, "s" if n > 1 else ""))
        if rec.get("probe"):
            lost = sum(o["ms"] for o in rec["probe"]["outages"])
            parts.append("%d ms of traffic lost" % lost)
        return ", ".join(parts)


def current(lab_id):
    st = CURRENT.get("run")
    if not st or st.get("lab_id") != lab_id:
        return None
    t0 = st["runner"].t0
    evs = list(st["events"])[-60:]
    return {"id": st["id"], "playbook": st["playbook"], "started": st["started"], "total_s": st["total_s"],
            "step": st["step"], "elapsed": round(time.time() - (t0 or st["started"]), 1),
            "events": [dict({k: v for k, v in e.items() if k != "at"},
                            t=round(e["at"] - t0, 2) if (t0 and e.get("at")) else None) for e in evs]}


def stop(lab_id):
    st = CURRENT.get("run")
    if not st or st.get("lab_id") != lab_id:
        raise ExpError("no playbook is running on this lab", 409)
    st["runner"].stop_flag = True
    return {"ok": True}
