#!/usr/bin/env python3
"""
Convergence tests: how long does traffic stop when a link fails, and where
does it go instead?

A test (a "scenario") names a source node, a destination, a probe interval and
a failure: fail link L (shutdown or cut) at T seconds, restore it D seconds
later, keep probing P seconds after that. A run:

  1. traces the path (pathtrace.py) before anything happens,
  2. starts prober.py in the source node's network namespace - one ICMP echo
     every few milliseconds, each reply's RTT recorded,
  3. fails and restores the link on schedule (linkctl.py), recording the exact
     times,
  4. traces the path again while the link is down and after it is back,
  5. finds every run of lost probes and attributes each to the event that
     preceded it: outage = lost probes x interval.

The source has to be a native container (FRR or Linux): the prober runs with
the host's python inside its network namespace, and a VM router's traffic does
not originate there. The destination can be any address.

Stored per lab under <lab dir>/.clabd-scenarios/<lab>/: scenarios.json and
runs/<id>.json (the per-probe RTTs included, for the chart).
"""

import json
import os
import re
import statistics
import subprocess
import threading
import time

import linkctl
import pathtrace
import sysbin

PROBER = os.path.join(sysbin.APP_DIR, "prober.py")
PYTHON = sysbin.find("python3")
DIR = ".clabd-scenarios"
ID_RE = re.compile(r"^[a-z0-9]{6,20}$")
MAX_TOTAL = 600
KEEP_RUNS = 40

CURRENT = {"run": None}          # the run in progress, for the page to watch
LOCK = threading.Lock()


class ConvError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


def _root(lab):
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", lab.get("name") or "lab")
    return os.path.join(os.path.dirname(lab["path"]), DIR, name)


def _own(path, lab):
    st = os.stat(os.path.dirname(lab["path"]))
    for dirpath, dirs, files in os.walk(path):
        os.chown(dirpath, st.st_uid, st.st_gid)
        for f in files:
            os.chown(os.path.join(dirpath, f), st.st_uid, st.st_gid)


def _write(lab, rel, obj):
    root = _root(lab)
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    tmp = full + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh)
    os.replace(tmp, full)
    _own(os.path.dirname(root), lab)


def probe_nodes(lab):
    """Nodes a probe can start from: native linux containers (FRR included)."""
    return sorted(n["name"] for n in lab.get("nodes") or [] if n.get("kind") == "linux")


# --------------------------------------------------------------------------
# scenarios
# --------------------------------------------------------------------------

def load(lab):
    try:
        with open(os.path.join(_root(lab), "scenarios.json")) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return []


def validate(lab, sc):
    names = {n["name"] for n in lab.get("nodes") or []}
    out = {"id": sc.get("id") or os.urandom(5).hex(), "name": str(sc.get("name") or "").strip()[:60]}
    if not out["name"]:
        raise ConvError("give the test a name")
    if not ID_RE.match(out["id"]):
        raise ConvError("bad test id")
    src = sc.get("src")
    if src not in probe_nodes(lab):
        raise ConvError("the source must be an FRR or Linux node (the probe runs in its network namespace)")
    out["src"] = src
    out["src_ip"] = (sc.get("src_ip") or "").strip() or None
    dst = str(sc.get("dst") or "").strip()
    if not dst:
        raise ConvError("pick a destination")
    if dst not in names and not re.match(r"^\d+\.\d+\.\d+\.\d+$", dst):
        raise ConvError("the destination is a node name or an IPv4 address")
    out["dst"] = dst
    try:
        out["interval_ms"] = max(1, min(100, int(sc.get("interval_ms") or 5)))
        out["pre_s"] = max(2, min(60, int(sc.get("pre_s") or 5)))
        out["down_s"] = max(0, min(300, int(sc.get("down_s") if sc.get("down_s") is not None else 20)))
        out["post_s"] = max(3, min(120, int(sc.get("post_s") or 10)))
    except (TypeError, ValueError):
        raise ConvError("times must be whole numbers")
    if out["pre_s"] + out["down_s"] + out["post_s"] > MAX_TOTAL:
        raise ConvError("a test can run %d s at most" % MAX_TOTAL)
    link = sc.get("link")
    out["link"] = {k: link[k] for k in ("a", "a_if", "b", "b_if")} if isinstance(link, dict) else None
    if out["link"] is None:
        raise ConvError("pick the link to fail")
    linkctl.Links.find_link(lab, out["link"])
    out["mode"] = sc.get("mode") if sc.get("mode") in ("shutdown", "cut") else "shutdown"
    return out


def save(lab, sc):
    sc = validate(lab, sc)
    lst = [x for x in load(lab) if x.get("id") != sc["id"]] + [sc]
    _write(lab, "scenarios.json", lst)
    return sc


def delete(lab, sid):
    lst = load(lab)
    if not any(x.get("id") == sid for x in lst):
        raise ConvError("no such test", 404)
    _write(lab, "scenarios.json", [x for x in lst if x.get("id") != sid])
    return {"ok": True}


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------

def runs(lab, full_id=None):
    d = os.path.join(_root(lab), "runs")
    if full_id:
        if not ID_RE.match(full_id or ""):
            raise ConvError("bad run id")
        try:
            with open(os.path.join(d, full_id + ".json")) as fh:
                return json.load(fh)
        except OSError:
            raise ConvError("no such run", 404)
    out = []
    try:
        names = sorted(os.listdir(d), reverse=True)
    except OSError:
        return out
    for fn in names:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fn)) as fh:
                r = json.load(fh)
        except (OSError, ValueError):
            continue
        r.pop("rtt_us", None)
        out.append(r)
    return out


def delete_run(lab, rid):
    if not ID_RE.match(rid or ""):
        raise ConvError("bad run id")
    try:
        os.remove(os.path.join(_root(lab), "runs", rid + ".json"))
    except OSError:
        raise ConvError("no such run", 404)
    return {"ok": True}


def analyse(res, events):
    """Outages (runs of lost probes) and what each event cost."""
    iv = res["interval_ms"]
    rtt = res["rtt_us"]
    start = res["start"]
    outages, i, n = [], 0, len(rtt)
    while i < n:
        if rtt[i] < 0:
            j = i
            while j < n and rtt[j] < 0:
                j += 1
            outages.append({"from_s": round(i * iv / 1000, 3), "to_s": round(j * iv / 1000, 3),
                            "lost": j - i, "ms": round((j - i) * iv)})
            i = j
        else:
            i += 1
    for ev in events:
        ev["t_s"] = round(ev["at"] - start, 3)
        # the first outage that starts around or after the event, before the next one
        nxt = min([e["at"] - start for e in events if e["at"] > ev["at"]] + [1e9])
        cand = [o for o in outages if ev["t_s"] - 2 * iv / 1000 <= o["from_s"] < nxt]
        ev["outage_ms"] = sum(o["ms"] for o in cand) if cand else 0
        ev["outage_parts"] = len(cand)

    def med(a, b):
        xs = [rtt[k] / 1000 for k in range(int(a * 1000 / iv), min(n, int(b * 1000 / iv))) if rtt[k] >= 0]
        return round(statistics.median(xs), 3) if xs else None
    t_fail = events[0]["t_s"] if events else n * iv / 1000
    t_back = events[1]["t_s"] if len(events) > 1 else n * iv / 1000
    return {
        "outages": outages,
        "sent": n, "lost": sum(1 for x in rtt if x < 0),
        "rtt_before_ms": med(0, t_fail),
        "rtt_during_ms": med(t_fail, t_back),
        "rtt_after_ms": med(t_back, n * iv / 1000),
    }


def _path_summary(tr):
    if not tr or tr.get("error"):
        return tr
    return {"result": tr["result"],
            "nodes": [h["node"] for h in tr["hops"] if not h.get("action", "").startswith(("pop VPN", "SRv6 decap"))],
            "links": [h["link"] for h in tr["hops"] if h.get("link")],
            "hops": tr["hops"]}


def run(lab_index_fn, lab_id, sc, links, live, audit, log):
    """The whole test, as a job step. Raises on failure (the link is restored)."""
    lab = lab_index_fn().get(lab_id)
    if lab is None or not lab.get("running"):
        raise ConvError("the lab is not running")
    sc = validate(lab, sc)
    poller = live.poller(lab_id)
    cont = next((c for c in lab.get("containers") or [] if c.get("short") == sc["src"]), None)
    if not cont or cont.get("state") != "running":
        raise ConvError("%s is not running" % sc["src"])
    src_ip = sc["src_ip"] or pathtrace.node_address(lab, poller, sc["src"])
    dst_ip = sc["dst"] if re.match(r"^\d+\.\d+\.\d+\.\d+$", sc["dst"]) \
        else pathtrace.node_address(lab, poller, sc["dst"])
    if not src_ip or not dst_ip:
        raise ConvError("could not work out the source / destination address - give IPs")
    rid = time.strftime("%Y%m%d%H%M%S") + os.urandom(2).hex()
    total = sc["pre_s"] + sc["down_s"] + sc["post_s"]
    log("%s: probing %s -> %s every %d ms for %d s" % (sc["name"], src_ip, dst_ip, sc["interval_ms"], total))

    def trace(label):
        try:
            # the poller stops when nobody watches the map: ask for it each time
            t = pathtrace.trace(lab_index_fn().get(lab_id) or lab, live.poller(lab_id), sc["src"], dst_ip)
            s = _path_summary(t)
            log("path %s: %s (%s)" % (label, " > ".join(s["nodes"]), t["result"]))
            return s
        except Exception as exc:                        # noqa: BLE001
            log("path %s: could not trace - %s" % (label, exc))
            return {"error": str(exc)}

    paths = {"before": trace("before")}
    pid = linkctl._pid(cont["name"])
    # the prober is given plenty of time and stopped once the test is over:
    # failing a link on a VM router can take a while (an XR commit on a fresh
    # node first clears its configuration inconsistency), and the schedule
    # below is relative to when each step really happened
    proc = subprocess.Popen([linkctl.NSENTER, "-t", str(pid), "-n", PYTHON, PROBER, src_ip, dst_ip,
                             str(sc["interval_ms"]), str(total + 300)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    state = {"id": rid, "scenario": sc, "lab_id": lab_id, "started": time.time(), "total_s": total,
             "progress": None, "events": [], "src_ip": src_ip, "dst_ip": dst_ip}
    with LOCK:
        CURRENT["run"] = state
    result = {}

    def reader():
        for line in proc.stdout:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("p"):
                state["progress"] = d
            elif d.get("done"):
                result.update(d)
    rth = threading.Thread(target=reader, daemon=True)
    rth.start()
    t0 = time.time()
    failed = False
    events = []
    link = sc["link"]
    try:
        time.sleep(0.3)
        if proc.poll() is not None:
            raise ConvError("the prober did not start: %s" % (proc.stderr.read().strip()[-300:] or "?"))

        def wait_until(t):
            d = t - time.time()
            if d > 0:
                time.sleep(d)
        wait_until(t0 + sc["pre_s"])
        cur = lab_index_fn().get(lab_id) or lab
        te = time.time()
        links.fail(lab_index_fn, cur, link, sc["mode"], 0, "convergence test")
        failed = True
        done = time.time()
        events.append({"op": "fail", "mode": sc["mode"], "at": te, "took_s": round(done - te, 2)})
        log("t=%.1fs link %s:%s - %s:%s failed (%s%s)" % (te - t0, link["a"], link["a_if"], link["b"],
            link["b_if"], sc["mode"], ", took %.1f s" % (done - te) if done - te > 2 else ""))
        state["events"] = events
        # the link stays down for down_s from the moment the failure was in place
        if sc["down_s"] >= 6:
            wait_until(done + sc["down_s"] / 2.0)
            paths["during"] = trace("during")
        wait_until(done + sc["down_s"])
        cur = lab_index_fn().get(lab_id) or lab
        te = time.time()
        links.restore(lab_index_fn, cur, link, "convergence test")
        failed = False
        done = time.time()
        events.append({"op": "restore", "at": te, "took_s": round(done - te, 2)})
        log("t=%.1fs link restored%s" % (te - t0, " (took %.1f s)" % (done - te) if done - te > 2 else ""))
        wait_until(done + sc["post_s"])
        proc.send_signal(15)
        proc.wait(timeout=60)
        rth.join(timeout=10)
    finally:
        if failed:
            try:
                links.restore(lab_index_fn, lab_index_fn().get(lab_id) or lab, link, "convergence test (cleanup)")
                log("link restored after an error")
            except Exception as exc:                    # noqa: BLE001
                log("could not restore the link: %s - restore it from the topology view" % exc)
        if proc.poll() is None:
            proc.kill()
        with LOCK:
            CURRENT["run"] = None
    if not result.get("rtt_us"):
        raise ConvError("the prober returned no results: %s" % (proc.stderr.read().strip()[-300:] or "?"))
    time.sleep(2)
    paths["after"] = trace("after")
    stats = analyse(result, events)
    rec = {"id": rid, "scenario": sc, "when": t0, "src_ip": src_ip, "dst_ip": dst_ip,
           "interval_ms": result["interval_ms"], "start": result["start"], "events": events,
           "paths": paths, "rtt_us": result["rtt_us"], **stats}
    for ev in events:
        log("%s: %d ms of loss%s" % (ev["op"], ev["outage_ms"],
                                    (" (%d separate gaps)" % ev["outage_parts"]) if ev["outage_parts"] > 1 else ""))
    log("lost %d of %d probes; rtt %s / %s / %s ms before / during / after"
        % (stats["lost"], stats["sent"], stats["rtt_before_ms"], stats["rtt_during_ms"], stats["rtt_after_ms"]))
    _write(lab, os.path.join("runs", rid + ".json"), rec)
    audit("CONVERGENCE lab=%s test=%r fail=%sms restore=%sms"
          % (lab.get("name"), sc["name"], events[0]["outage_ms"] if events else "-",
             events[1]["outage_ms"] if len(events) > 1 else "-"))
    # prune old runs
    d = os.path.join(_root(lab), "runs")
    old = sorted(f for f in os.listdir(d) if f.endswith(".json"))[:-KEEP_RUNS]
    for f in old:
        try:
            os.remove(os.path.join(d, f))
        except OSError:
            pass
    return rec
