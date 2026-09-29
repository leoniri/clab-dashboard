#!/usr/bin/env python3
"""
Containerlab dashboard - backend.

Serves a small JSON API plus the static UI. A background collector keeps a
cache of lab state, host stats and per-container resource usage so that HTTP
requests never block on docker.

Deliberately dependency-light: stdlib plus PyYAML.

Security notes
--------------
* Binds to 127.0.0.1 only; nginx is the public listener.
* The client never sends a filesystem path. It sends a lab id, which must be a
  key in the server's own discovered index; the path comes from there. This is
  what stops the action endpoint from becoming "run clab against any file".
* The clab subcommands invoked are deploy, deploy --reconfigure, destroy and
  `tools netem` / `tools veth` (link impairments, live topology edits) - the
  last two only against a link the lab's own topology names.
* "delete" never removes anything: it destroys the lab if it has containers,
  then moves its files into TRASH_DIR with a RESTORE.txt beside them. What is
  moved is decided here (see delete_plan), never by the client, and the client
  must echo the lab name back as confirmation.
* One action at a time, globally - these labs are RAM-bound and two concurrent
  deploys would thrash the host.

Modules: builder (topology generator), editor (file editor + history),
topoedit (live topology edits), devcfg (router config: IOS-XE/XR, NX-OS, FRR,
SR Linux), linkctl (link failures and netem), livestate (adjacencies, BGP,
traffic for the map), capture (tcpdump/tshark in the browser), snapshots
(config snapshots and restore), labio (export, import, catalogue), images /
vendorimg (image management), lanexpose (LAN access).
"""

import collections
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlparse

import yaml

import auth
import builder
import capture
import convergence
import devcfg
import editor
import guide
import images
import labio
import lanexpose
import linkctl
import livestate
import pathtrace
import snapshots
import topoedit
import vendorimg
import sysbin

try:
    with open(os.path.join(sysbin.APP_DIR, "VERSION")) as _fh:
        APP_VERSION = _fh.read().strip()
except OSError:
    APP_VERSION = "dev"

BIND_HOST = "127.0.0.1"
BIND_PORT = 8090
STATIC_DIR = os.path.join(sysbin.APP_DIR, "static")
# Everything the builder writes lands under here and nowhere else.
TOPO_BASE = "/opt/clab-topologies"
CLAB = sysbin.find("clab", "containerlab")
DOCKER = sysbin.find("docker")

# Deleted labs are moved here, never removed. Deliberately outside SCAN_ROOTS
# so trashed topologies do not reappear in the lab list.
TRASH_DIR = "/var/lib/clab-dashboard/trash"
UPLOAD_DIR = "/var/lib/clab-dashboard/uploads"
TRASH_ITEM_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[A-Za-z0-9_.-]{1,40}$")

# Where to look for topology files.
# Where topology files are looked for: the usual places people keep labs on
# any host (every user's home included), plus CLABD_SCAN_ROOTS (":"-separated).
# Labs that are running are found through containerlab wherever they live.
SCAN_ROOTS = ["/opt", "/root", "/srv"] + sorted(
    os.path.join("/home", d) for d in (os.listdir("/home") if os.path.isdir("/home") else [])
    if os.path.isdir(os.path.join("/home", d))) + [
    p for p in os.environ.get("CLABD_SCAN_ROOTS", "").split(":") if p]
SCAN_DEPTH = 5

FAST_INTERVAL = 3.0     # lab state + host stats
SLOW_INTERVAL = 15.0    # docker stats (takes ~2s, so keep it off the fast path)
LOG_LINES = 4000

# Rough per-node memory, MB. Used only to show a "this lab will cost you N GB"
# estimate before you deploy, on a host where RAM is the binding constraint.
KIND_RAM_MB = {
    "cisco_c8000v": 4096,
    "cisco_csr1000v": 4096,
    "cisco_cat9kv": 18432,
    "cisco_n9kv": 10240,
    "cisco_xrd_vrouter": 8192,
    "cisco_xrd_control_plane": 2048,
    "cisco_xrv9k": 16384,
    "cisco_xrv": 4096,
    "cisco_csr": 4096,
    "vr-csr": 4096,
    "vr-xrv9k": 16384,
    "vr-veos": 2048,
    "arista_ceos": 2048,
    "nokia_srlinux": 2048,
    "juniper_vjunosswitch": 5120,
    "juniper_vjunosrouter": 5120,
    "juniper_vmx": 8192,
    "linux": 256,
    "bridge": 0,
    "ovs-bridge": 0,
    "host": 0,
}
DEFAULT_RAM_MB = 512
# The XRd vRouter launcher clamps RAM up to this, whatever the topology says.
HARD_FLOOR_MB = {"cisco_xrd_vrouter": 8192}

RAM_ENV_KEYS = ("QEMU_MEMORY", "RAM", "MEMORY")


# --------------------------------------------------------------------------
# topology discovery
# --------------------------------------------------------------------------

TOPO_RE = re.compile(r"\.clab\.ya?ml$")
# Directories that hold copies, not deployable topologies.
SKIP_DIRS = {"artifacts", "backup", "backups", "node_modules", "venv",
             "__pycache__", "site-packages", "vrnetlab"}
# clab creates a per-lab directory holding .state.clab.yaml and generated
# artefacts. Those are not topologies you can deploy.
def is_generated_dir(path):
    """True for a directory containerlab generated for a deployed lab.

    Those are named clab-<labname> and always hold .state.clab.yaml.
    Matching on the name alone would also hide a user directory that
    merely starts with "clab-", so require the state file as proof.
    """
    return (os.path.basename(path).startswith("clab-")
            and os.path.isfile(os.path.join(path, ".state.clab.yaml")))


def find_topologies():
    found = []
    for root in SCAN_ROOTS:
        if not os.path.isdir(root):
            continue
        root_depth = root.rstrip("/").count("/")
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            if dirpath.count("/") - root_depth >= SCAN_DEPTH:
                dirnames[:] = []
                continue
            dirnames[:] = [
                d for d in dirnames
                if d not in SKIP_DIRS and not d.startswith(".")
                and not is_generated_dir(os.path.join(dirpath, d))
            ]
            for fn in filenames:
                if not TOPO_RE.search(fn):
                    continue
                full = os.path.join(dirpath, fn)
                if fn.startswith(".state."):
                    continue
                found.append(os.path.abspath(full))
    return sorted(set(found))


def lab_id(path):
    return hashlib.sha1(path.encode()).hexdigest()[:12]


def config_is_stale(topo_path, lab_name):
    """True when the topology or its configs changed since the last deploy.

    containerlab generates clab-<lab>/ on deploy and a plain `deploy` reuses
    it, so an edited startup-config is silently ignored until --reconfigure.
    """
    d = os.path.dirname(topo_path)
    state = os.path.join(d, "clab-%s" % lab_name, ".state.clab.yaml")
    if not os.path.isfile(state):
        return False
    try:
        deployed_at = os.path.getmtime(state)
    except OSError:
        return False
    newest = 0.0
    try:
        newest = os.path.getmtime(topo_path)
    except OSError:
        pass
    cfgdir = os.path.join(d, "configs")
    if os.path.isdir(cfgdir):
        for fn in os.listdir(cfgdir):
            try:
                newest = max(newest, os.path.getmtime(os.path.join(cfgdir, fn)))
            except OSError:
                pass
    # a second of slack: clab touches the state file as it writes the configs
    return newest > deployed_at + 1.0


def node_ram_mb(kind, node_env, kind_env):
    env = {}
    env.update(kind_env or {})
    env.update(node_env or {})
    ram = None
    for key in RAM_ENV_KEYS:
        if key in env:
            try:
                ram = int(str(env[key]).strip())
                break
            except (TypeError, ValueError):
                pass
    if ram is None:
        ram = KIND_RAM_MB.get(kind, DEFAULT_RAM_MB)
    floor = HARD_FLOOR_MB.get(kind)
    if floor:
        ram = max(ram, floor)
    return ram


def _endpoint(ep):
    """Return (node, interface) for a clab link endpoint.

    Endpoints are usually "node:iface" strings, but newer topologies may use
    a mapping, and special kinds (host, mgmt-net, macvlan) put a non-node name
    on one side.
    """
    if isinstance(ep, dict):
        node = ep.get("node") or ep.get("name") or ""
        return str(node), str(ep.get("interface") or ep.get("iface") or "")
    if isinstance(ep, str):
        if ":" in ep:
            a, _, b = ep.partition(":")
            return a.strip(), b.strip()
        return ep.strip(), ""
    return "", ""


def parse_topology(path):
    """Return a dict describing a topology file, or an error marker."""
    entry = {
        "id": lab_id(path),
        "path": path,
        "dir": os.path.dirname(path),
        "file": os.path.basename(path),
        "name": None,
        "nodes": [],
        "node_count": 0,
        "link_count": 0,
        "kinds": {},
        "links": [],
        "est_ram_mb": 0,
        "mgmt_subnet": None,
        "mgmt_network": None,
        "parse_error": None,
        "mtime": None,
    }
    try:
        entry["mtime"] = os.path.getmtime(path)
        with open(path) as fh:
            doc = yaml.safe_load(fh) or {}
    except Exception as exc:                      # noqa: BLE001
        entry["parse_error"] = str(exc)[:300]
        return entry

    if not isinstance(doc, dict):
        entry["parse_error"] = "not a mapping"
        return entry

    entry["name"] = doc.get("name") or os.path.basename(path).split(".")[0]
    mgmt = doc.get("mgmt") or {}
    if isinstance(mgmt, dict):
        entry["mgmt_subnet"] = mgmt.get("ipv4-subnet") or mgmt.get("ipv4_subnet")
        entry["mgmt_network"] = mgmt.get("network")

    topo = doc.get("topology") or {}
    kinds_cfg = topo.get("kinds") or {}
    defaults = topo.get("defaults") or {}
    nodes_cfg = topo.get("nodes") or {}
    links = topo.get("links") or []
    entry["link_count"] = len(links) if isinstance(links, list) else 0

    if not isinstance(nodes_cfg, dict):
        entry["parse_error"] = "topology.nodes is not a mapping"
        return entry

    total = 0
    for nname, ncfg in nodes_cfg.items():
        ncfg = ncfg if isinstance(ncfg, dict) else {}
        groups = topo.get("groups") if isinstance(topo.get("groups"), dict) else {}
        gcfg = groups.get(ncfg.get("group")) if ncfg.get("group") else None
        gcfg = gcfg if isinstance(gcfg, dict) else {}
        kind = ncfg.get("kind") or gcfg.get("kind") or defaults.get("kind") or "linux"
        kcfg = kinds_cfg.get(kind) if isinstance(kinds_cfg, dict) else None
        # clab's short aliases (srl, ceos, ...) mean the same kinds
        kind = labio.KIND_ALIASES.get(kind, kind)
        kcfg = kcfg if isinstance(kcfg, dict) else {}
        image = (ncfg.get("image") or gcfg.get("image") or kcfg.get("image")
                 or defaults.get("image") or "-")
        ram = node_ram_mb(kind, ncfg.get("env"), kcfg.get("env"))
        total += ram
        pos = None
        env = {}
        for src in (defaults.get("env"), kcfg.get("env"), ncfg.get("env")):
            if isinstance(src, dict):
                env.update(src)
        labels = ncfg.get("labels")
        if isinstance(labels, dict) and labels.get("builder-pos"):
            try:
                px, py = str(labels["builder-pos"]).split(",")
                pos = [float(px), float(py)]
            except (TypeError, ValueError):
                pos = None
        entry["nodes"].append({
            "name": nname,
            "kind": kind,
            "image": image,
            "est_ram_mb": ram,
            "mgmt_ipv4": ncfg.get("mgmt-ipv4") or ncfg.get("mgmt_ipv4"),
            "pos": pos,
            # containerlab's own convention for the node's diagram icon
            "icon": (labels.get("graph-icon") if isinstance(labels, dict) else None),
            "user": str(env.get("USERNAME") or "clab"),
        })
        # FRR is kind linux to containerlab; call it what it is in the summary
        shown = "frr" if kind == "linux" and devcfg.FRR_IMAGE_RE.search(str(image)) else kind
        entry["kinds"][shown] = entry["kinds"].get(shown, 0) + 1

    known = {n["name"] for n in entry["nodes"]}
    if isinstance(links, list):
        for li, link in enumerate(links):
            eps = link.get("endpoints") if isinstance(link, dict) else None
            if not isinstance(eps, list) or len(eps) < 2:
                continue
            an, ai = _endpoint(eps[0])
            bn, bi = _endpoint(eps[1])
            if not an or not bn:
                continue
            entry["links"].append({
                "id": "l%d" % li,
                "a": an, "a_if": ai, "a_known": an in known,
                "b": bn, "b_if": bi, "b_known": bn in known,
                "type": (link.get("type") if isinstance(link, dict) else None) or "veth",
            })

    entry["nodes"].sort(key=lambda n: n["name"])
    entry["node_count"] = len(entry["nodes"])
    entry["est_ram_mb"] = total
    return entry


# --------------------------------------------------------------------------
# delete
# --------------------------------------------------------------------------

def delete_plan(lab, all_paths):
    """Decide what deleting a lab moves to the trash. Pure: touches nothing.

    The lab's whole directory goes only when it is dedicated to this lab: no
    other discovered topology lives in it or below it, and it is not a scan
    root or one of the dashboard's own directories. Otherwise only the
    topology file and its generated clab-<name>/ directory go, so labs that
    share a directory (several topology files side by side) are left intact.
    """
    path = lab["path"]
    d = os.path.dirname(path)
    name = lab.get("name") or ""
    protected = {os.path.normpath(p) for p in
                 SCAN_ROOTS + [TOPO_BASE, "/", sysbin.APP_DIR,
                               os.path.dirname(TRASH_DIR)]}
    others = [p for p in all_paths
              if p != path and (os.path.dirname(p) == d or p.startswith(d + os.sep))]
    if os.path.normpath(d) not in protected and not others:
        return {"mode": "dir", "moves": [d], "shared_with": []}
    moves = [path]
    gen = os.path.join(d, "clab-%s" % name)
    if name and is_generated_dir(gen):
        moves.append(gen)
    return {"mode": "files", "moves": moves,
            "shared_with": sorted(os.path.basename(p) if os.path.dirname(p) == d
                                  else os.path.relpath(p, d) for p in others)}


def move_to_trash(lab, plan, log):
    """Move the planned paths into a fresh trash folder. Returns True on success."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", lab.get("name") or "lab")[:40]
    dest_root = os.path.join(TRASH_DIR, "%s-%s" % (stamp, safe))
    os.makedirs(dest_root, exist_ok=False)
    restore = ["# deleted from the dashboard %s" % time.strftime("%Y-%m-%d %H:%M:%S %z"),
               "# lab: %s   topology: %s" % (lab.get("name"), lab["path"]),
               "# to restore, run as root:"]
    for src in plan["moves"]:
        if not os.path.lexists(src):
            log("skip (already gone): %s" % src)
            continue
        dst = os.path.join(dest_root, os.path.basename(src))
        shutil.move(src, dst)
        log("moved %s -> %s" % (src, dst))
        restore.append("mv %s %s" % (_shq(dst), _shq(src)))
    with open(os.path.join(dest_root, "RESTORE.txt"), "w") as fh:
        fh.write("\n".join(restore) + "\n")
    log("restore instructions: %s" % os.path.join(dest_root, "RESTORE.txt"))
    return dest_root


def _shq(p):
    return "'" + p.replace("'", "'\\''") + "'"


# --------------------------------------------------------------------------
# runtime state
# --------------------------------------------------------------------------

def run(cmd, timeout=120):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as exc:                      # noqa: BLE001
        return 1, "", str(exc)


def clab_inspect():
    """{lab_name: [container, ...]} from clab, or {} if nothing is deployed."""
    rc, out, _ = run([CLAB, "inspect", "--all", "--format", "json"], timeout=60)
    if rc != 0 or not out.strip():
        return {}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {}
    # clab has used both {lab: [...]} and a flat [...] over its life.
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        grouped = {}
        for c in data:
            grouped.setdefault(c.get("lab_name") or "?", []).append(c)
        return grouped
    return {}


def docker_stats():
    fmt = "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"
    rc, out, _ = run([DOCKER, "stats", "--no-stream", "--format", fmt], timeout=60)
    stats = {}
    if rc != 0:
        return stats
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        name, cpu, mem = parts
        used = mem.split("/")[0].strip()
        stats[name.strip()] = {"cpu": cpu.strip(), "mem": used}
    return stats


def read_host_stats(prev_cpu):
    st = {}
    try:
        meminfo = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, _, v = line.partition(":")
                meminfo[k.strip()] = int(v.split()[0])
        total = meminfo.get("MemTotal", 0)
        avail = meminfo.get("MemAvailable", 0)
        st["mem_total_mb"] = total // 1024
        st["mem_used_mb"] = (total - avail) // 1024
        st["mem_pct"] = round((total - avail) / total * 100, 1) if total else 0
    except Exception:                             # noqa: BLE001
        pass

    cpu_now = None
    try:
        with open("/proc/stat") as fh:
            fields = fh.readline().split()[1:]
        vals = [int(x) for x in fields]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        cpu_now = (sum(vals), idle)
        if prev_cpu:
            dt = cpu_now[0] - prev_cpu[0]
            di = cpu_now[1] - prev_cpu[1]
            st["cpu_pct"] = round((1 - di / dt) * 100, 1) if dt > 0 else 0.0
    except Exception:                             # noqa: BLE001
        pass

    try:
        with open("/proc/loadavg") as fh:
            st["load"] = fh.read().split()[:3]
        st["cpu_count"] = os.cpu_count()
    except Exception:                             # noqa: BLE001
        pass

    try:
        vfs = os.statvfs("/")
        total_b = vfs.f_blocks * vfs.f_frsize
        free_b = vfs.f_bavail * vfs.f_frsize
        st["disk_total_gb"] = round(total_b / 1024**3, 1)
        st["disk_free_gb"] = round(free_b / 1024**3, 1)
        st["disk_pct"] = round((total_b - free_b) / total_b * 100, 1) if total_b else 0
    except Exception:                             # noqa: BLE001
        pass

    try:
        with open("/proc/uptime") as fh:
            st["uptime_s"] = int(float(fh.read().split()[0]))
    except Exception:                             # noqa: BLE001
        pass

    st["hostname"] = os.uname().nodename
    return st, cpu_now


# --------------------------------------------------------------------------
# job runner
# --------------------------------------------------------------------------

class Job:
    """One dashboard action. `steps` run in order and stop at the first failure.

    A step is either a command list, run as a subprocess, or a
    (label, fn) tuple where fn(log) runs in-process and raises on failure.
    """
    def __init__(self, job_id, lab, action, steps):
        self.id = job_id
        self.lab_id = lab["id"]
        self.lab_name = lab.get("name") or lab["file"]
        self.action = action
        self.steps = steps
        self.cmd = " && ".join(s[0] if isinstance(s, tuple) else " ".join(s)
                               for s in steps)
        self.lines = collections.deque(maxlen=LOG_LINES)
        self.running = True
        self.rc = None
        self.started = time.time()
        self.finished = None
        self.lock = threading.Lock()

    def append(self, text):
        with self.lock:
            self.lines.append(text)

    def snapshot(self, since=0):
        with self.lock:
            lines = list(self.lines)
        return {
            "id": self.id,
            "lab_id": self.lab_id,
            "lab_name": self.lab_name,
            "action": self.action,
            "cmd": self.cmd,
            "running": self.running,
            "rc": self.rc,
            "started": self.started,
            "finished": self.finished,
            "total_lines": len(lines),
            "lines": lines[since:],
        }


class JobManager:
    def __init__(self, prefix="j"):
        self.prefix = prefix
        self.lock = threading.Lock()
        self.current = None
        self.history = collections.deque(maxlen=20)
        self.seq = 0

    def busy(self):
        with self.lock:
            return self.current is not None and self.current.running

    def start(self, lab, action, steps):
        with self.lock:
            if self.current is not None and self.current.running:
                return None, "an action is already running: %s on %s" % (
                    self.current.action, self.current.lab_name)
            self.seq += 1
            job = Job("%s%d" % (self.prefix, self.seq), lab, action, steps)
            self.current = job
            self.history.appendleft(job)
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job, None

    def _run(self, job):
        audit("START %s lab=%s cmd=%s" % (job.action, job.lab_name, job.cmd))
        env = dict(os.environ)
        env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        env["TERM"] = "dumb"
        job.rc = 0
        for step in job.steps:
            if isinstance(step, tuple):
                label, fn = step
                job.append("$ " + label)
                try:
                    fn(job.append)
                except Exception as exc:          # noqa: BLE001
                    job.append("dashboard error: %s" % exc)
                    job.rc = 1
            else:
                job.append("$ " + " ".join(step))
                try:
                    proc = subprocess.Popen(
                        step, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, bufsize=1, env=env,
                        cwd=_step_cwd(step))
                    for line in proc.stdout:
                        job.append(line.rstrip("\n"))
                    proc.wait()
                    job.rc = proc.returncode
                except Exception as exc:          # noqa: BLE001
                    job.append("dashboard error: %s" % exc)
                    job.rc = 1
            if job.rc != 0:
                break
        job.running = False
        job.finished = time.time()
        audit("END   %s lab=%s rc=%s" % (job.action, job.lab_name, job.rc))
        job.append("--- exited with code %s ---" % job.rc)

    def get(self, job_id):
        with self.lock:
            for j in self.history:
                if j.id == job_id:
                    return j
        return None

    def latest(self):
        with self.lock:
            return self.history[0] if self.history else None


def _step_cwd(step):
    """clab wants to run beside its topology file; anything else runs in /.
    (The last argument is a path only for clab steps - for docker it can be an
    image name like local/foo:1, which must not be taken for a directory.)"""
    d = os.path.dirname(step[-1])
    return d if os.path.isabs(d) and os.path.isdir(d) else "/"


JOBS = JobManager()
# Image pulls/loads can take minutes; they get their own queue so they never
# block a lab deploy, and still run one at a time among themselves.
IMG_JOBS = JobManager(prefix="i")
LAN = lanexpose.Lan()

AUDIT_LOG = "/var/log/clab-dashboard-actions.log"


def audit(msg):
    """Append to the audit log and to the journal. Never raises."""
    line = "%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%S%z"), msg)
    print("AUDIT " + msg, flush=True)
    try:
        with open(AUDIT_LOG, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# collector
# --------------------------------------------------------------------------

class Collector(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.state = {"labs": [], "host": {}, "updated": 0, "scan_error": None}
        self.stats = {}
        self._prev_cpu = None
        self._last_slow = 0.0
        self._topo_cache = {}

    def index(self):
        """lab_id -> topology entry, for action validation."""
        with self.lock:
            return {lab["id"]: lab for lab in self.state["labs"]}

    def all_paths(self):
        with self.lock:
            return [lab["path"] for lab in self.state["labs"]]

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))

    def run(self):
        while True:
            try:
                self._tick()
            except Exception as exc:              # noqa: BLE001
                with self.lock:
                    self.state["scan_error"] = str(exc)[:300]
            time.sleep(FAST_INTERVAL)

    def _tick(self):
        now = time.time()
        if now - self._last_slow > SLOW_INTERVAL:
            self.stats = docker_stats()
            self._last_slow = now

        host, self._prev_cpu = read_host_stats(self._prev_cpu)
        running = clab_inspect()

        # topology files, re-parsed only when mtime changes
        topos = {}
        for path in find_topologies():
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            cached = self._topo_cache.get(path)
            if cached and cached.get("mtime") == mtime:
                topos[path] = dict(cached)
            else:
                parsed = parse_topology(path)
                self._topo_cache[path] = parsed
                topos[path] = dict(parsed)

        # a running lab whose file is outside the scan roots still deserves a row
        # (except the dashboard's own throw-away boot-test labs)
        running = {k: v for k, v in running.items() if not k.startswith("clabd-boottest-")}
        for lab_name, containers in running.items():
            if not containers:
                continue
            path = containers[0].get("absLabPath") or containers[0].get("labPath")
            if path and path not in topos:
                if os.path.isfile(path):
                    topos[path] = parse_topology(path)
                else:
                    topos[path] = {
                        "id": lab_id(path), "path": path,
                        "dir": os.path.dirname(path), "file": os.path.basename(path),
                        "name": lab_name, "nodes": [], "node_count": 0,
                        "link_count": 0, "kinds": {}, "est_ram_mb": 0,
                        "mgmt_subnet": None, "mtime": None,
                        "parse_error": "topology file not found on disk",
                    }

        by_path_running = {}
        for lab_name, containers in running.items():
            for c in containers:
                p = c.get("absLabPath") or c.get("labPath")
                by_path_running.setdefault(p, {"name": lab_name, "containers": []})
                by_path_running[p]["containers"].append(c)

        labs = []
        for path, topo in topos.items():
            lab = dict(topo)
            run_info = by_path_running.get(path)
            containers = []
            if run_info:
                lab["name"] = run_info["name"] or lab.get("name")
                for c in sorted(run_info["containers"], key=lambda x: x.get("name", "")):
                    st = self.stats.get(c.get("name", ""), {})
                    containers.append({
                        "name": c.get("name"),
                        "short": (c.get("name") or "").replace(
                            "clab-%s-" % (run_info["name"] or ""), ""),
                        "kind": c.get("kind"),
                        "image": c.get("image"),
                        "state": c.get("state"),
                        "status": c.get("status"),
                        # clab reports "N/A" for a stopped container
                        "ipv4": _addr(c.get("ipv4_address")),
                        "id": (c.get("container_id") or "")[:12],
                        "cpu": st.get("cpu"),
                        "mem": st.get("mem"),
                    })
            lab["containers"] = containers
            lab["running_count"] = sum(1 for c in containers if c["state"] == "running")
            # Containers that exist but are all stopped are leftovers from a
            # previous boot: the lab is not running, and only --reconfigure
            # will bring it back.
            lab["stale_containers"] = bool(containers) and lab["running_count"] == 0
            lab["running"] = lab["running_count"] > 0
            lab["stale_config"] = config_is_stale(path, lab.get("name") or "")
            lab["healthy_count"] = sum(1 for c in containers if c["status"] == "healthy")
            labs.append(lab)

        labs.sort(key=lambda l: (not l["running"], (l.get("name") or "").lower()))

        try:
            LAN.reconcile(labs)
        except Exception as exc:                  # noqa: BLE001
            LAN.last_error = "reconcile: %s" % exc
        exp = LAN.settings.get("exposure", {})
        active = {(a["lab"], a["node"]) for a in LAN.active}
        for lab in labs:
            e = exp.get(lab.get("name")) or {}
            lab["lan"] = {
                "enabled": bool(e.get("enabled")),
                "nodes": e.get("nodes") or {},
                "active": sorted(n for (ln, n) in active if ln == lab.get("name")),
                "gateway": sorted(a["node"] for a in LAN.active
                                  if a["lab"] == lab.get("name") and a.get("gateway")),
                "gateway_login": "%s / %s" % LAN._gw_credentials(),
                # per live node: node (its own sshd) | gateway | none
                "ssh": {a["node"]: a.get("ssh") for a in LAN.active if a["lab"] == lab.get("name")},
                "unreachable": sorted(a["node"] for a in LAN.active
                                      if a["lab"] == lab.get("name") and a.get("reachable") is False),
            }

        host["labs_total"] = len(labs)
        host["labs_running"] = sum(1 for l in labs if l["running"])
        host["containers_running"] = sum(l["running_count"] for l in labs)
        host["clab_version"] = CLAB_VERSION

        # A lab that stops running without an action having been issued for it
        # is worth a line in the log - that is exactly the case we could not
        # explain once.
        try:
            prev = {l["id"]: l for l in self.state.get("labs", [])}
            for lab in labs:
                was = prev.get(lab["id"])
                if was and was.get("running") and not lab["running"]:
                    cur = JOBS.latest()
                    acting = (cur and cur.lab_id == lab["id"]
                              and (time.time() - cur.started) < 600)
                    if not acting:
                        audit("LAB DOWN name=%s path=%s - no dashboard action "
                              "was running for it" % (lab.get("name"), lab.get("path")))
        except Exception:                         # noqa: BLE001
            pass

        job = JOBS.latest()
        with self.lock:
            self.state = {
                "labs": labs,
                "host": host,
                "updated": time.time(),
                "scan_error": None,
                "busy": JOBS.busy(),
                "active_job": job.id if (job and job.running) else None,
            }


def _addr(v):
    v = (v or "").split("/")[0].strip()
    return "" if v in ("", "N", "N/A") else v


def clab_version():
    rc, out, _ = run([CLAB, "version"], timeout=20)
    if rc != 0:
        return "unknown"
    for line in out.splitlines():
        line = line.strip()
        if line.lower().startswith("version:"):
            return line.split(":", 1)[1].strip()
    return "unknown"


CLAB_VERSION = "unknown"
COLLECTOR = Collector()
LINKS = linkctl.Links(audit)
LIVE = livestate.Live(COLLECTOR.index, LINKS)
CAPTURES = capture.Captures(audit)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

ACTIONS = {
    "deploy":    lambda p: [CLAB, "deploy", "-t", p],
    "redeploy":  lambda p: [CLAB, "deploy", "--reconfigure", "-t", p],
    "destroy":   lambda p: [CLAB, "destroy", "-t", p],
}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "clabd"

    def log_message(self, fmt, *args):            # quieter journal
        pass

    # -- helpers ----------------------------------------------------------
    def _json(self, obj, code=200, cookie=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path):
        rel = path.lstrip("/") or "index.html"
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            self.send_error(404)
            return
        ctype = CONTENT_TYPES.get(os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- login ------------------------------------------------------------
    def _client_ip(self):
        # nginx is the only thing that reaches the loopback listener, and it
        # passes the real peer in X-Real-IP
        ip = self.client_address[0]
        if ip in ("127.0.0.1", "::1") and self.headers.get("X-Real-IP"):
            return self.headers["X-Real-IP"].strip()[:64]
        return ip

    def _who(self):
        """For the audit log: user@address (just the address with login off)."""
        user = getattr(self, "user", None)
        return "%s@%s" % (user, self._client_ip()) if user else self._client_ip()

    def _secure(self):
        return self.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def _gate(self, u, method):
        """True when the request may go on; otherwise answers it (redirect to
        the login page for a page load, 401 for anything else)."""
        AUTH.fresh()
        if not AUTH.enabled:
            self.user = None
            return True
        if method == "POST":
            # SameSite=Lax already keeps the cookie off cross-site POSTs;
            # refuse a foreign Origin as well, for older browsers
            origin = self.headers.get("Origin")
            host = self.headers.get("Host", "")
            if origin and origin != "null" and urlparse(origin).netloc != host:
                self._json({"error": "cross-origin request refused"}, 403)
                return False
        self.user = AUTH.session_user(self.headers.get("Cookie"))
        if self.user or u.path in auth.PUBLIC_PATHS:
            return True
        page = u.path == "/" or u.path.endswith(".html")
        if method == "GET" and page:
            nxt = u.path + ("?" + u.query if u.query else "")
            self.send_response(302)
            self.send_header("Location", "/login.html?next=" + quote(nxt, safe=""))
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._json({"error": "login required"}, 401)
        return False

    def _auth_get(self, u):
        if u.path == "/api/auth/check":           # nginx auth_request for /term/
            ok = not AUTH.enabled or self.user
            self.send_response(204 if ok else 401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        if u.path == "/api/auth/me":
            self._json({"enabled": AUTH.enabled, "user": self.user,
                        "initial_password": bool(self.user == "admin"
                                                 and os.path.exists(auth.INITIAL_PW_FILE))})
            return True
        return False

    def _auth_post(self, u):
        try:
            p = _read_json_body(self)
        except Exception:                         # noqa: BLE001
            self._json({"error": "bad request body"}, 400)
            return
        ip = self._client_ip()
        try:
            if u.path == "/api/auth/login":
                if not AUTH.enabled:
                    self._json({"ok": True, "user": None})
                    return
                token, exp = AUTH.login(str(p.get("user") or ""), str(p.get("password") or ""), ip)
                self._json({"ok": True, "user": p.get("user")},
                           cookie=auth.cookie_header(token, exp - int(time.time()), self._secure()))
            elif u.path == "/api/auth/logout":
                if self.user:
                    audit("AUTH logout user=%s from=%s" % (self.user, ip))
                self._json({"ok": True}, cookie=auth.cookie_header("", 0, self._secure()))
            elif u.path == "/api/auth/password":
                if not self.user:
                    raise auth.AuthError("login is switched off", 400)
                token, exp = AUTH.change_password(self.user, p.get("current"), p.get("new"), ip)
                self._json({"ok": True},
                           cookie=auth.cookie_header(token, exp - int(time.time()), self._secure()))
            else:
                self._json({"error": "not found"}, 404)
        except auth.AuthError as exc:
            self._json({"error": str(exc)}, exc.status)

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        if not self._gate(u, "GET"):
            return
        if u.path.startswith("/api/auth/") and self._auth_get(u):
            return
        if u.path == "/api/state":
            self._json(COLLECTOR.snapshot())
            return
        if u.path.startswith("/api/job/"):
            job_id = u.path.rsplit("/", 1)[-1]
            since = 0
            for kv in u.query.split("&"):
                if kv.startswith("since="):
                    try:
                        since = int(kv[6:])
                    except ValueError:
                        since = 0
            job = (JOBS.latest() if job_id == "latest"
                   else IMG_JOBS.latest() if job_id == "latest-image"
                   else JOBS.get(job_id) or IMG_JOBS.get(job_id))
            if job is None:
                self._json({"error": "no such job"}, 404)
                return
            self._json(job.snapshot(since))
            return
        if u.path == "/api/images":
            self._json({"images": builder.list_images(),
                        "kinds": sorted(builder.KINDS.keys())})
            return
        if u.path == "/api/delete-plan":
            lid = ""
            for kv in u.query.split("&"):
                if kv.startswith("lab="):
                    lid = kv[4:]
            lab = COLLECTOR.index().get(lid)
            if lab is None:
                self._json({"error": "unknown lab id"}, 404)
                return
            plan = delete_plan(lab, COLLECTOR.all_paths())
            plan.update({"lab_id": lid, "name": lab.get("name"),
                         "will_destroy": bool(lab.get("containers")),
                         "trash_dir": TRASH_DIR})
            self._json(plan)
            return
        if u.path.startswith("/api/") and self._manage_get(u):
            return
        if u.path == "/api/health":
            self._json({"ok": True, "clab": CLAB_VERSION, "version": APP_VERSION})
            return
        self._static(u.path)

    def do_POST(self):
        u = urlparse(self.path)
        if not self._gate(u, "POST"):
            return
        if u.path.startswith("/api/auth/"):
            self._auth_post(u)
            return
        if u.path == "/api/topology":
            self._topology()
            return
        if u.path == "/api/images/upload":
            self._upload(u)
            return
        if u.path == "/api/images/vendor-upload":
            self._vendor_upload(u)
            return
        if u.path == "/api/lab/import/upload":
            self._lab_upload(u)
            return
        if u.path in MANAGE_POST:
            try:
                payload = _read_json_body(self)
            except Exception:                     # noqa: BLE001
                self._json({"error": "bad request body"}, 400)
                return
            try:
                status, body = MANAGE_POST[u.path](self, payload)
            except (ValueError, lanexpose.LanError, topoedit.TopoError, linkctl.LinkError) as exc:
                status, body = 400, {"error": str(exc)}
            except (capture.CaptureError, snapshots.SnapError, labio.ImportError_,
                    convergence.ConvError) as exc:
                status, body = exc.status, {"error": str(exc)}
            except devcfg.DevError as exc:
                status, body = getattr(exc, "status", 400), {"error": str(exc)}
            except editor.EditError as exc:
                status, body = exc.status, {"error": str(exc)}
            self._json(body, status)
            return
        if u.path != "/api/action":
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:                         # noqa: BLE001
            self._json({"error": "bad request body"}, 400)
            return

        action = payload.get("action")
        lid = payload.get("lab_id")
        if action not in ACTIONS and action != "delete":
            self._json({"error": "unknown action"}, 400)
            return

        # The path is looked up from our own index - never taken from the client.
        lab = COLLECTOR.index().get(lid)
        if lab is None:
            self._json({"error": "unknown lab id"}, 404)
            return
        path = lab["path"]
        if not os.path.isfile(path):
            self._json({"error": "topology file no longer exists: %s" % path}, 409)
            return

        if action == "delete":
            if payload.get("confirm_name") != lab.get("name"):
                self._json({"error": "confirmation does not match the lab name"}, 400)
                return
            steps = self._delete_steps(lab)
        else:
            steps = [ACTIONS[action](path)]

        audit("REQUEST %s lab=%s path=%s from=%s"
              % (action, lab.get("name"), path, self._who()))
        job, err = JOBS.start(lab, action, steps)
        if job is None:
            self._json({"error": err}, 409)
            return
        self._json({"job_id": job.id})

    @staticmethod
    def _delete_steps(lab):
        """destroy (only if containers exist), then move the files to trash.

        The plan is recomputed inside the job, after destroy, so it reflects
        the disk as it is at the moment of the move.
        """
        steps = []
        if lab.get("containers"):
            steps.append(ACTIONS["destroy"](lab["path"]))

        def trash(log):
            left = [c for c in clab_inspect().get(lab.get("name") or "", [])
                    if (c.get("absLabPath") or c.get("labPath")) == lab["path"]]
            if left:
                raise RuntimeError("%d container(s) still exist - not moving files"
                                   % len(left))
            plan = delete_plan(lab, COLLECTOR.all_paths())
            if plan["shared_with"]:
                log("directory is shared with %s - moving only this lab's files"
                    % ", ".join(plan["shared_with"]))
            dest = move_to_trash(lab, plan, log)
            audit("TRASHED lab=%s moves=%s to=%s" % (lab.get("name"), plan["moves"], dest))
            twins = [l for l in COLLECTOR.index().values()
                     if l.get("name") == lab.get("name") and l["path"] != lab["path"]]
            if not twins and lab.get("name") in LAN.settings.get("exposure", {}):
                LAN.forget_lab(lab.get("name"))
                log("released the LAN addresses reserved for %s" % lab.get("name"))

        steps.append(("move lab files to %s" % TRASH_DIR, trash))
        return steps

    # -- management API (images, networks, LAN, trash, audit) ---------------
    def _manage_get(self, u):
        q = dict(kv.split("=", 1) for kv in u.query.split("&") if "=" in kv)
        from urllib.parse import unquote
        q = {k: unquote(v) for k, v in q.items()}
        labs = COLLECTOR.snapshot().get("labs", [])
        if u.path == "/api/images/list":      # GET /api/images is the builder's palette
            hints = {i["image"]: i for i in builder.list_images()}
            body = images.list_images(labs, hints)
            tests = vendorimg.load_tests()
            for im in body["images"]:
                im["boot_test"] = tests.get(im.get("ref"))
                im["boot_testable"] = (im.get("kind") in vendorimg.BOOT)
            body["disk"] = images.disk_usage()
            self._json(body)
        elif u.path == "/api/images/vendor-check":
            try:
                self._json(vendorimg.check(q.get("name", ""), q.get("tag", "")))
            except vendorimg.VendorError as exc:
                self._json({"error": str(exc), "platforms": vendorimg.platforms()}, 400)
        elif u.path == "/api/images/search":
            try:
                self._json({"results": images.search_hub(q.get("q", ""))})
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
        elif u.path == "/api/networks":
            self._json(images.list_networks(labs))
        elif u.path == "/api/lan":
            body = LAN.public()
            body["interfaces"] = host_interfaces()
            self._json(body)
        elif u.path == "/api/lan/ssh-config":
            lab = COLLECTOR.index().get(q.get("lab", ""))
            if lab is None:
                self._json({"error": "unknown lab id"}, 404)
            else:
                users = {k: (v[0] if v else None) for k, v in _lan_logins(lab).items()}
                self._json({"text": LAN.ssh_config(lab.get("name"), users)})
        elif u.path == "/api/trash":
            self._json({"items": list_trash(), "dir": TRASH_DIR})
        elif u.path.startswith("/api/device/"):
            r = devcfg.handle_get(u.path, q, COLLECTOR.index())
            if r:
                self._json(r[1], r[0])
            else:
                self._json({"error": "not found"}, 404)
        elif u.path == "/api/live":
            lab = COLLECTOR.index().get(q.get("lab", ""))
            if lab is None:
                self._json({"error": "unknown lab id"}, 404)
            elif not lab.get("running"):
                LINKS.status(lab)             # forgets failures of a stopped lab
                self._json({"running": False, "links": {}, "nodes": {}, "bgp_edges": []})
            else:
                body = LIVE.get(lab["id"])
                body["running"] = True
                self._json(body)
        elif u.path == "/api/snapshots" or u.path.startswith("/api/snapshots/"):
            self._snap_get(u.path, q)
        elif u.path == "/api/guide":
            lab = COLLECTOR.index().get(q.get("lab", ""))
            if lab is None:
                self._json({"error": "unknown lab id"}, 404)
            else:
                self._json(guide.load(lab))
        elif u.path == "/api/catalog":
            self._json({"templates": labio.templates(builder), "scenarios": labio.scenario_listing(builder),
                        "examples": labio.examples(),
                        "community": labio.COMMUNITY})
        elif u.path == "/api/lab/export":
            lab = COLLECTOR.index().get(q.get("lab", ""))
            if lab is None:
                self._json({"error": "unknown lab id"}, 404)
                return True
            dedicated = delete_plan(lab, COLLECTOR.all_paths())["mode"] == "dir"
            data, n, skipped = labio.export_zip(lab, dedicated, q.get("snapshots") == "1")
            audit("EXPORT lab=%s files=%d skipped=%d from=%s"
                  % (lab.get("name"), n, len(skipped), self._who()))
            name = "%s-%s.zip" % (re.sub(r"[^A-Za-z0-9_.-]", "_", lab.get("name") or "lab"),
                                  time.strftime("%Y%m%d"))
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif u.path in ("/api/convergence", "/api/convergence/run"):
            lab = COLLECTOR.index().get(q.get("lab", ""))
            if lab is None:
                self._json({"error": "unknown lab id"}, 404)
            elif u.path == "/api/convergence/run":
                try:
                    self._json(convergence.runs(lab, q.get("id")))
                except convergence.ConvError as exc:
                    self._json({"error": str(exc)}, exc.status)
            else:
                cur = convergence.CURRENT.get("run")
                if cur and cur.get("lab_id") != lab["id"]:
                    cur = None
                if cur:
                    cur = {k: cur[k] for k in ("id", "scenario", "started", "total_s", "progress", "events",
                                               "src_ip", "dst_ip")}
                self._json({"lab_name": lab.get("name"),
                            "scenarios": convergence.load(lab), "runs": convergence.runs(lab),
                            "probe_nodes": convergence.probe_nodes(lab), "running": bool(lab.get("running")),
                            "current": cur, "busy": JOBS.busy(),
                            "links": [l for l in lab.get("links") or [] if l.get("a_known") and l.get("b_known")],
                            "nodes": [{"name": n["name"], "kind": n["kind"], "image": n.get("image")}
                                      for n in lab.get("nodes") or []]})
        elif u.path.startswith("/api/capture/"):
            self._capture_get(u.path, q)
        elif u.path == "/api/topo/ports":
            lab = COLLECTOR.index().get(q.get("lab", ""))
            if lab is None:
                self._json({"error": "unknown lab id"}, 404)
            else:
                try:
                    self._json({"ports": topoedit.ports(lab),
                                "subnets": topoedit.suggest_subnets(lab),
                                "running": bool(lab.get("running"))})
                except Exception as exc:              # noqa: BLE001
                    self._json({"error": "cannot read the topology: %s" % exc}, 400)
        elif u.path.startswith("/api/edit/") or u.path == "/api/builder-spec":
            self._edit_get(u.path, q)
        elif u.path == "/api/audit":
            try:
                n = max(10, min(2000, int(q.get("lines", "300"))))
            except ValueError:
                n = 300
            self._json({"lines": tail(AUDIT_LOG, n)})
        else:
            return False
        return True

    def _snap_get(self, path, q):
        lab = COLLECTOR.index().get(q.get("lab", ""))
        if lab is None:
            self._json({"error": "unknown lab id"}, 404)
            return
        try:
            if path == "/api/snapshots":
                self._json({"snapshots": snapshots.list_(lab), "running": bool(lab.get("running")),
                            "nodes": [{"name": n, "platform": devcfg.PLATFORM_NAMES.get(p, p),
                                       "running": bool(c)}
                                      for n, p, c in snapshots.configurable_nodes(lab)]})
            elif path == "/api/snapshots/config":
                self._json({"text": snapshots.read_node(lab, q.get("id"), q.get("node"))})
            elif path == "/api/snapshots/diff":
                self._json(snapshots.diff(COLLECTOR.index(), lab, q.get("id"), q.get("node")))
            else:
                self._json({"error": "not found"}, 404)
        except snapshots.SnapError as exc:
            self._json({"error": str(exc)}, exc.status)
        except devcfg.DevError as exc:
            self._json({"error": str(exc)}, exc.status)

    def _capture_get(self, path, q):
        try:
            if path == "/api/capture/list":
                self._json({"captures": CAPTURES.list(q.get("lab") or None),
                            "tools": capture.have_tools()})
            elif path == "/api/capture/packets":
                self._json(CAPTURES.packets(q.get("id"), q.get("since", 0), q.get("limit", 500),
                                            q.get("filter")))
            elif path == "/api/capture/detail":
                self._json(CAPTURES.detail(q.get("id"), q.get("n")))
            elif path == "/api/capture/pcap":
                full, name = CAPTURES.pcap_path(q.get("id"))
                self._file(full, name, "application/vnd.tcpdump.pcap")
            else:
                self._json({"error": "not found"}, 404)
        except capture.CaptureError as exc:
            self._json({"error": str(exc)}, exc.status)

    def _file(self, full, name, ctype):
        """Send a file as a download, streamed."""
        size = os.path.getsize(full)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(full, "rb") as fh:
            shutil.copyfileobj(fh, self.wfile, 1024 * 1024)

    def _edit_get(self, path, q):
        lab = COLLECTOR.index().get(q.get("lab", ""))
        if lab is None:
            self._json({"error": "unknown lab id"}, 404)
            return
        try:
            if path == "/api/edit/files":
                self._json({"lab": {k: lab.get(k) for k in
                                    ("id", "name", "path", "dir", "running", "stale_config",
                                     "node_count", "containers")},
                            "files": editor.editable_files(lab),
                            "builder": builder_mode(lab)})
            elif path == "/api/edit/file":
                self._json(editor.read_file(lab, q.get("path", "")))
            elif path == "/api/edit/history":
                self._json({"versions": editor.history(lab, q.get("path", ""))})
            elif path == "/api/edit/version":
                self._json({"content": editor.read_version(lab, q.get("path", ""),
                                                           q.get("version", ""))})
            elif path == "/api/builder-spec":
                spec, how, warnings = editor.builder_spec(lab, builder.KINDS)
                mode = builder_mode(lab)
                self._json({"spec": spec, "how": how, "warnings": warnings, **mode})
            else:
                self._json({"error": "not found"}, 404)
        except editor.EditError as exc:
            self._json({"error": str(exc)}, exc.status)

    def _upload(self, u):
        """Stream a docker image tarball to disk, then `docker load` it as a job."""
        from urllib.parse import parse_qs
        name = (parse_qs(u.query).get("name") or [""])[0]
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,120}\.(tar|tar\.gz|tgz|tar\.xz)$", name):
            self._json({"error": "file must be a .tar, .tar.gz or .tar.xz docker image archive"}, 400)
            return
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            size = 0
        if size <= 0:
            self._json({"error": "empty upload"}, 400)
            return
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        st = os.statvfs(UPLOAD_DIR)
        free = st.f_bavail * st.f_frsize
        # the archive plus the unpacked image, plus headroom for running labs
        if size * 2.2 + 5 * 1024**3 > free:
            self._json({"error": "not enough disk: %.1f GB upload, %.1f GB free"
                        % (size / 1024**3, free / 1024**3)}, 507)
            return
        dest = os.path.join(UPLOAD_DIR, "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), name))
        left = size
        try:
            with open(dest, "wb") as fh:
                while left > 0:
                    chunk = self.rfile.read(min(left, 4 * 1024 * 1024))
                    if not chunk:
                        raise OSError("upload interrupted with %d bytes missing" % left)
                    fh.write(chunk)
                    left -= len(chunk)
        except OSError as exc:
            try:
                os.remove(dest)                   # our own partial temp file
            except OSError:
                pass
            self._json({"error": str(exc)}, 400)
            return
        audit("UPLOAD image archive %s (%d bytes) from=%s" % (name, size, self._who()))

        def cleanup(log):
            os.remove(dest)
            log("removed the uploaded archive %s" % dest)

        job, err = IMG_JOBS.start({"id": "images", "name": name}, "load",
                                  [[DOCKER, "load", "-i", dest], ("remove upload", cleanup)])
        if job is None:
            self._json({"error": err}, 409)
            return
        self._json({"job_id": job.id})

    def _lab_upload(self, u):
        """A lab archive (.zip / .tar.gz / .clab.yml): unpack it into a new lab directory."""
        from urllib.parse import parse_qs
        qs = parse_qs(u.query)
        name = (qs.get("name") or [""])[0]
        want = (qs.get("as") or [""])[0].strip().lower() or None
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,120}\.(zip|tar|tar\.gz|tgz|tar\.xz|clab\.ya?ml)$", name):
            self._json({"error": "upload a .zip, .tar.gz, .tar.xz or a .clab.yml file"}, 400)
            return
        if want and not labio.NAME_RE.match(want):
            self._json({"error": "the lab directory name must match [a-z][a-z0-9-]{0,30}"}, 400)
            return
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            size = 0
        if size <= 0 or size > 300 * 1024 * 1024:
            self._json({"error": "the upload must be between 1 byte and 300 MB"}, 400)
            return
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        dest = os.path.join(UPLOAD_DIR, "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), name))
        err = self._receive(dest, size)
        if err:
            self._json({"error": err}, 400)
            return
        lines = []
        try:
            res = labio.import_archive(dest, want, _lab_names(), lines.append)
        except labio.ImportError_ as exc:
            self._json({"error": str(exc)}, exc.status)
            return
        finally:
            try:
                os.remove(dest)
            except OSError:
                pass
        audit("IMPORT archive %s -> %s from=%s" % (name, res["dir"], self._who()))
        res["log"] = lines
        self._json(res)

    def _receive(self, dest, size):
        """Stream the request body to dest. Returns an error string or None."""
        left = size
        try:
            with open(dest, "wb") as fh:
                while left > 0:
                    chunk = self.rfile.read(min(left, 4 * 1024 * 1024))
                    if not chunk:
                        raise OSError("upload interrupted with %d bytes missing" % left)
                    fh.write(chunk)
                    left -= len(chunk)
        except OSError as exc:
            try:
                os.remove(dest)                   # our own partial temp file
            except OSError:
                pass
            return str(exc)
        return None

    def _vendor_upload(self, u):
        """A vendor ISO / qcow2: store it, identify it, build a vrnetlab image as a job."""
        from urllib.parse import parse_qs
        qs = parse_qs(u.query)
        name = (qs.get("name") or [""])[0]
        tag = (qs.get("tag") or [""])[0]
        replace = (qs.get("replace") or ["0"])[0] == "1"
        chosen = (qs.get("platform") or [""])[0] or None
        boottest = (qs.get("boottest") or ["1"])[0] != "0"
        if not vendorimg.NAME_RE.match(name):
            self._json({"error": "the file must be a .iso or .qcow2"}, 400)
            return
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            size = 0
        if size <= 0:
            self._json({"error": "empty upload"}, 400)
            return
        os.makedirs(vendorimg.BUILD_DIR, exist_ok=True)
        st = os.statvfs(vendorimg.BUILD_DIR)
        free = st.f_bavail * st.f_frsize
        # upload + a 16 GB sparse install disk that fills to a few GB + the
        # compacted copy + the docker image layers, and headroom for labs
        need = size * 3 + 12 * 1024**3
        if need > free:
            self._json({"error": "not enough disk: this needs about %.0f GB free, %.0f GB is"
                        % (need / 1024**3, free / 1024**3)}, 507)
            return
        work = os.path.join(vendorimg.BUILD_DIR, time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(work, exist_ok=True)
        dest = os.path.join(work, name)
        err = self._receive(dest, size)
        if err:
            shutil.rmtree(work, ignore_errors=True)
            self._json({"error": err}, 400)
            return
        try:
            platform, version, how = vendorimg.detect(dest, chosen, tag or None)
        except vendorimg.VendorError as exc:
            shutil.rmtree(work, ignore_errors=True)
            self._json({"error": str(exc)}, 400)
            return
        p = vendorimg.PLATFORMS[platform]
        if name.rsplit(".", 1)[-1].lower() not in p["inputs"]:
            shutil.rmtree(work, ignore_errors=True)
            self._json({"error": "%s is only supported as %s" % (p["label"], " or ".join(p["inputs"]))}, 400)
            return
        tag = tag or version
        ref = "%s:%s" % (p["repo"], tag)
        if not vendorimg.TAG_RE.match(tag or ""):
            shutil.rmtree(work, ignore_errors=True)
            self._json({"error": "bad image tag %r" % tag}, 400)
            return
        if vendorimg.image_exists(ref) and not replace:
            shutil.rmtree(work, ignore_errors=True)
            self._json({"error": "%s already exists - pick another tag or allow replacing it" % ref,
                        "exists": True, "ref": ref}, 409)
            return
        audit("UPLOAD vendor image %s (%d bytes) -> %s %s (%s) tag=%s from=%s"
              % (name, size, platform, version, how, tag, self._who()))

        def run_build(log):
            log("%s %s identified %s" % (p["label"], version, how))
            vendorimg.build(dest, platform, version, tag, log, replace=replace,
                            boottest=boottest, avoid_subnets=_lab_subnets())

        job, jerr = IMG_JOBS.start({"id": "images", "name": ref}, "build image", [("build " + ref, run_build)])
        if job is None:
            shutil.rmtree(work, ignore_errors=True)
            self._json({"error": jerr}, 409)
            return
        self._json({"job_id": job.id, "platform": platform, "version": version, "ref": ref,
                    "how": how})

    def _topology(self):
        try:
            payload = _read_json_body(self)
        except Exception:                         # noqa: BLE001
            self._json({"error": "bad request body"}, 400)
            return
        status, body = _topology_impl(payload)
        self._json(body, status)


def _read_json_body(handler):
    n = int(handler.headers.get("Content-Length") or 0)
    if n > 1_000_000:
        raise ValueError("body too large")
    return json.loads(handler.rfile.read(n) or b"{}")


def _topology_impl(payload):
    """Generate, and optionally write, a topology. Returns (status, body)."""
    spec = payload.get("spec") or {}
    preview = bool(payload.get("preview"))
    overwrite = bool(payload.get("overwrite"))

    try:
        result = builder.generate(spec)
    except builder.SpecError as exc:
        return 400, {"error": str(exc)}
    except Exception as exc:                      # noqa: BLE001
        return 500, {"error": "generator failed: %s" % exc}

    name = result["summary"]["name"]
    target = os.path.join(TOPO_BASE, name)
    topo_path = os.path.join(target, "%s.clab.yml" % name)

    if preview:
        return 200, {"preview": True, "target_dir": target,
                     "topology_path": topo_path, **result}

    # Refuse to clobber a lab that is currently deployed.
    for lab in COLLECTOR.snapshot().get("labs", []):
        if lab.get("path") == topo_path and lab.get("running"):
            return 409, {"error": "%s is running - stop it before overwriting" % name}

    if os.path.exists(target) and not overwrite:
        return 409, {"error": "%s already exists" % target, "exists": True}

    snapshot = None
    if os.path.isdir(target):
        # Keep every current file before the builder overwrites the lab.
        snapshot, kept = editor.snapshot_dir(target, "before the builder overwrote the lab")
        audit("BUILDER overwrite %s - previous files kept in %s/%s (%d files)"
              % (name, editor.HISTORY, snapshot, len(kept)))
    try:
        os.makedirs(os.path.join(target, "configs"), exist_ok=True)
        written = []
        for rel, text in result["files"].items():
            full = os.path.normpath(os.path.join(target, rel))
            if not full.startswith(target + os.sep):
                return 400, {"error": "refusing to write outside %s" % target}
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as fh:
                fh.write(text)
            written.append(full)
        # configs of nodes that no longer exist would only confuse; the
        # snapshot above still has them
        cfgdir = os.path.join(target, "configs")
        for fn in os.listdir(cfgdir):
            p = os.path.join(cfgdir, fn)
            if os.path.isfile(p) and p not in written and snapshot:
                os.remove(p)
        # remember the drawing, so the lab reopens in the builder as drawn
        with open(os.path.join(target, editor.SPEC_FILE), "w") as fh:
            json.dump(spec, fh, indent=1, sort_keys=True)
    except OSError as exc:
        return 500, {"error": "write failed: %s" % exc}

    return 200, {"preview": False, "target_dir": target, "history": snapshot,
                 "topology_path": topo_path, "written": written,
                 "lab_id": lab_id(topo_path),
                 "summary": result["summary"], "warnings": result["warnings"]}


# --------------------------------------------------------------------------
# management: images, networks, LAN exposure, trash, audit
# --------------------------------------------------------------------------

def host_interfaces():
    rc, out, _ = run([sysbin.find("ip"), "-o", "-4", "addr", "show"], timeout=10)
    res = []
    for line in out.splitlines():
        f = line.split()
        if len(f) < 4 or f[1] == "lo":
            continue
        name = f[1]
        if name.startswith(("br-", "docker", "veth")) or name in [r["name"] for r in res]:
            continue
        res.append({"name": name, "addr": f[3]})
    return res


def tail(path, n):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - n * 400))
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    return data.splitlines()[-n:]


def _restore_pairs(item_dir):
    """(trash path, original path) pairs from RESTORE.txt."""
    pairs = []
    try:
        with open(os.path.join(item_dir, "RESTORE.txt")) as fh:
            for line in fh:
                m = re.match(r"^mv '(.+)' '(.+)'$", line.strip())
                if m:
                    pairs.append((m.group(1).replace("'\\''", "'"),
                                  m.group(2).replace("'\\''", "'")))
    except OSError:
        pass
    return pairs


def _trash_item_dir(item):
    if not TRASH_ITEM_RE.match(item or ""):
        raise ValueError("bad trash item")
    d = os.path.join(TRASH_DIR, item)
    if os.path.realpath(d) != d or not os.path.isdir(d):
        raise ValueError("no such trash item")
    return d


def _du(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def list_trash():
    items = []
    try:
        names = sorted(os.listdir(TRASH_DIR), reverse=True)
    except OSError:
        return items
    for name in names:
        d = os.path.join(TRASH_DIR, name)
        if not (TRASH_ITEM_RE.match(name) and os.path.isdir(d)):
            continue
        pairs = _restore_pairs(d)
        items.append({
            "item": name,
            "deleted": "%s-%s-%s %s:%s" % (name[0:4], name[4:6], name[6:8], name[9:11], name[11:13]),
            "lab": name[16:],
            "originals": [p[1] for p in pairs],
            "restorable": bool(pairs) and not any(os.path.lexists(p[1]) for p in pairs),
            "blocked_by": [p[1] for p in pairs if os.path.lexists(p[1])],
            "size_mb": round(_du(d) / 1024**2, 1),
        })
    return items


def _images_post(handler, p):
    op = p.get("op")
    who = handler._who()
    if op == "pull":
        ref = images.check_ref(p.get("ref"))
        steps, label = [[DOCKER, "pull", ref]], ref
    elif op == "remove":
        ref = images.check_image_id(p.get("ref"))
        steps, label = [[DOCKER, "image", "rm", ref]], ref
    elif op == "tag":
        src = images.check_image_id(p.get("ref"))
        dst = images.check_ref(p.get("new_ref"))
        steps, label = [[DOCKER, "tag", src, dst]], "%s -> %s" % (src, dst)
    elif op == "boottest":
        ref = images.check_ref(p.get("ref"))
        hint = {i["image"]: i for i in builder.list_images()}.get(ref) or {}
        kind = hint.get("kind")
        if kind not in vendorimg.BOOT:
            return 400, {"error": "no boot test for %s (kind %s)" % (ref, kind or "unknown")}
        steps = [("boot test " + ref, lambda log: vendorimg.boot_test(ref, kind, log, _lab_subnets()))]
        label = ref
    elif op == "prune":
        steps, label = [[DOCKER, "image", "prune", "-f"]], "dangling images"
    else:
        return 400, {"error": "unknown op"}
    audit("REQUEST image-%s %s from=%s" % (op, label, who))
    job, err = IMG_JOBS.start({"id": "images", "name": label}, "image " + op, steps)
    if job is None:
        return 409, {"error": err}
    return 200, {"job_id": job.id}


def _lab_subnets():
    return [l.get("mgmt_subnet") for l in COLLECTOR.snapshot().get("labs", [])]


def _networks_post(handler, p):
    if p.get("op") != "remove":
        return 400, {"error": "unknown op"}
    name = images.check_net(p.get("name"))
    for lab in COLLECTOR.snapshot().get("labs", []):
        if lab.get("running") and (lab.get("mgmt_network") or "clab") == name:
            return 409, {"error": "%s is the management network of running lab %s"
                         % (name, lab.get("name"))}
    audit("REQUEST network-remove %s from=%s" % (name, handler._who()))
    rc, out, err = run([DOCKER, "network", "rm", name], timeout=30)
    if rc != 0:
        return 409, {"error": err.strip() or "docker network rm failed"}
    return 200, {"ok": True}


def _lan_lab(p):
    lab = COLLECTOR.index().get(p.get("lab_id"))
    if lab is None:
        raise ValueError("unknown lab id")
    return lab


def _lan_settings_post(handler, p):
    if "pool" in p or "iface" in p:
        pool = p.get("pool") or []
        if isinstance(pool, str):
            pool = [x for x in re.split(r"[\s,]+", pool) if x]
        iface = (p.get("iface") or "").strip() or LAN.settings["lan"]["iface"]
        LAN.set_lan(iface, pool)
        audit("LAN settings iface=%s pool=%s from=%s"
              % (iface, ",".join(pool), handler._who()))
    g = p.get("gateway")
    if isinstance(g, dict):
        LAN.set_gateway(port=g.get("port"), user=g.get("user"), password=g.get("password"))
        audit("LAN ssh gateway settings port=%s user=%s%s from=%s"
              % (g.get("port"), g.get("user"), " (password changed)" if g.get("password") else "",
                 handler._who()))
        LAN.reconcile(COLLECTOR.snapshot().get("labs", []))
    return 200, LAN.public()


def _lan_suggest_post(handler, p):
    lab = _lan_lab(p)
    res = LAN.suggest(lab.get("name"), [n["name"] for n in lab.get("nodes", [])])
    res["pool"] = list(LAN.settings["lan"].get("pool") or [])
    res["pool_hint"] = LAN.pool_hint()
    res["iface"] = LAN.settings["lan"]["iface"]
    return 200, res


def _lan_logins(lab):
    """{node: [user, password]} for SSH to a lab's exposed nodes: the gateway's
    login where the gateway answers, else what the node was started with."""
    gw = {a["node"] for a in LAN.active if a["lab"] == lab.get("name") and a.get("gateway")}
    out = {}
    for n in lab.get("nodes") or []:
        name = n["name"]
        if name in gw:
            out[name] = list(LAN._gw_credentials())
        elif n.get("kind") == "linux":
            out[name] = None                    # a plain container: no login of its own
        else:
            out[name] = list(devcfg.credentials(lab, name))
    return out


def _lan_lab_post(handler, p):
    lab = _lan_lab(p)
    names = {n["name"] for n in lab.get("nodes", [])}
    nodes = {k: v for k, v in (p.get("nodes") or {}).items() if k in names}
    LAN.set_lab(lab.get("name"), bool(p.get("enabled")), nodes)
    audit("LAN %s lab=%s nodes=%s from=%s"
          % ("expose" if p.get("enabled") else "unexpose", lab.get("name"),
             ",".join("%s=%s" % kv for kv in sorted(nodes.items())),
             handler._who()))
    LAN.reconcile(COLLECTOR.snapshot().get("labs", []))
    res = LAN.public()
    res["logins"] = _lan_logins(COLLECTOR.index().get(p.get("lab_id")) or lab)
    return 200, res


def _trash_post(handler, p):
    op, item = p.get("op"), p.get("item")
    d = _trash_item_dir(item)
    who = handler._who()
    if op == "restore":
        pairs = _restore_pairs(d)
        if not pairs:
            return 409, {"error": "no RESTORE.txt entries in %s" % item}
        for src, dst in pairs:
            if os.path.lexists(dst):
                return 409, {"error": "%s exists again - move it away first" % dst}
            if not os.path.isdir(os.path.dirname(dst)):
                return 409, {"error": "parent directory %s is gone" % os.path.dirname(dst)}
        for src, dst in pairs:
            shutil.move(src, dst)
        os.remove(os.path.join(d, "RESTORE.txt"))
        os.rmdir(d)
        audit("RESTORED %s -> %s from=%s" % (item, [x[1] for x in pairs], who))
        return 200, {"restored": [x[1] for x in pairs]}
    if op == "purge":
        if p.get("confirm") != item:
            return 400, {"error": "confirmation does not match"}
        audit("PURGED trash item %s from=%s" % (item, who))
        shutil.rmtree(d)
        return 200, {"ok": True}
    return 400, {"error": "unknown op"}


def builder_mode(lab):
    """Can this lab be edited in place in the builder, or only copied into it?"""
    name = lab.get("name") or ""
    in_place = (lab["path"] == os.path.join(TOPO_BASE, name, "%s.clab.yml" % name))
    return {"in_place": in_place,
            "why": None if in_place else
            "not made by the builder - its configs are hand-written, and the builder "
            "regenerates every config it saves. Open it as a copy instead; the "
            "original is not touched."}


def _edit_post(handler, p):
    lab = COLLECTOR.index().get(p.get("lab_id"))
    if lab is None:
        return 404, {"error": "unknown lab id"}
    content = p.get("content")
    if not isinstance(content, str):
        return 400, {"error": "content missing"}
    try:
        res = editor.write_file(lab, p.get("path", ""), content, p.get("sha"),
                                create=bool(p.get("create")))
    except editor.EditError as exc:
        return exc.status, {"error": str(exc), "errors": getattr(exc, "errors", None)}
    if not res.get("unchanged"):
        audit("EDIT lab=%s file=%s version-kept=%s from=%s"
              % (lab.get("name"), res["path"], res.get("saved_version"),
                 handler._who()))
    res["running"] = bool(lab.get("running"))
    return 200, res


def _topo_lab(p):
    lab = COLLECTOR.index().get(p.get("lab_id"))
    if lab is None:
        raise ValueError("unknown lab id")
    if lab.get("parse_error"):
        raise ValueError("the topology file has errors - fix it in the editor first")
    return lab


def _topo_plan_post(handler, p):
    lab = _topo_lab(p)
    return 200, topoedit.plan(lab, p.get("changes") or {}, builder.KINDS)


def _topo_apply_post(handler, p):
    """Apply a change set: file always, the running lab as the plan says."""
    lab = _topo_lab(p)
    pl = topoedit.plan(lab, p.get("changes") or {}, builder.KINDS)
    if pl["errors"]:
        return 400, {"error": "\n".join(pl["errors"]), "plan": pl}
    ch, lid, path = pl["changes"], lab["id"], lab["path"]
    fresh = lambda: COLLECTOR.index().get(lid) or lab       # noqa: E731
    steps = []
    routers = [n["name"] for n in lab.get("nodes", []) if n.get("kind") in topoedit.ROUTERS]
    running_routers = [c["short"] for c in lab.get("containers") or []
                       if c.get("state") == "running" and c.get("short") in routers]

    if pl["running"] and pl["redeploy"]:
        def save_all(log):
            for n in running_routers:
                if n in ch["remove_nodes"]:
                    continue
                r = devcfg.save_startup(fresh(), n)
                log("%s: running config saved to %s" % (n, r["path"]))
        steps.append(("save the running config of every router", save_all))

    def write(log):
        topoedit.write_topology(fresh(), ch, builder.KINDS, log)
    steps.append(("update the topology file", write))

    if pl["running"] and pl["redeploy"]:
        steps.append([CLAB, "deploy", "--reconfigure", "-t", path])

        def wait_all(log):
            names = ["clab-%s-%s" % (lab["name"], n["name"]) for n in fresh().get("nodes", [])
                     if n.get("kind") in topoedit.ROUTERS]
            topoedit.wait_healthy(names, log, timeout=1200)
        steps.append(("wait for the routers to boot", wait_all))
    elif pl["running"]:
        live_links = [l for l in ch["add_links"]
                      if not ({l["a"], l["b"]} & set(pl["restart"]))]

        def live(log):
            cur = fresh()
            for n in ch["remove_nodes"]:
                rc, _, err = run([DOCKER, "rm", "-f", topoedit._cname(cur, n)], timeout=120)
                log("removed node %s%s" % (n, "" if rc == 0 else " (docker: %s)" % err.strip()))
            for l in ch["remove_links"]:
                if l["a"] in ch["remove_nodes"] or l["b"] in ch["remove_nodes"]:
                    continue
                for side in ("a", "b"):
                    rc, _, _ = topoedit._ns(topoedit._cname(cur, l[side]), topoedit.IP,
                                            "link", "del", l[side + "_if"])
                    if rc == 0:
                        break
                log("removed link %s:%s - %s:%s" % (l["a"], l["a_if"], l["b"], l["b_if"]))
            _, _, doc = topoedit._load(cur)
            for l in live_links:
                topoedit.veth(topoedit._cname(cur, l["a"]), l["a_if"],
                              topoedit._cname(cur, l["b"]), l["b_if"], log)
                for side in ("a", "b"):
                    if topoedit._node_kind(doc, l[side]) in topoedit.ROUTERS:
                        topoedit.rewire(topoedit._cname(cur, l[side]), l[side + "_if"], log)
            if pl["restart"]:
                log("restarting %s" % ", ".join(pl["restart"]))
                topoedit.restart_routers(cur, doc, pl["restart"], log)
        steps.append(("apply the changes to the running lab", live))

    if any(l.get("a_ip") or l.get("b_ip") for l in ch["add_links"]) and pl["running"]:
        def conf(log):
            topoedit.configure_links(COLLECTOR.index, lid, ch, log)
        steps.append(("configure the new links", conf))

    audit("REQUEST topology-edit lab=%s changes=%s plan=%s from=%s"
          % (lab.get("name"), json.dumps(ch, sort_keys=True),
             "redeploy" if pl["redeploy"] else ("restart " + ",".join(pl["restart"])
                                                if pl["restart"] else "live"),
             handler._who()))
    job, err = JOBS.start(lab, "edit", steps)
    if job is None:
        return 409, {"error": err}
    return 200, {"job_id": job.id, "plan": pl}


def _link_post(handler, p):
    """Fail / restore / impair one link of a running lab."""
    lab = COLLECTOR.index().get(p.get("lab_id"))
    if lab is None:
        return 404, {"error": "unknown lab id"}
    if not lab.get("running"):
        return 409, {"error": "the lab is not running"}
    who = handler._who()
    op = p.get("op")
    if op == "fail":
        return 200, LINKS.fail(COLLECTOR.index, lab, p.get("link"), p.get("mode") or "cut",
                               p.get("duration"), who)
    if op == "restore":
        return 200, LINKS.restore(COLLECTOR.index, lab, p.get("link"), who)
    if op == "impair":
        return 200, LINKS.impair(lab, p.get("link"), p.get("direction") or "both",
                                 p.get("params") or {}, who)
    return 400, {"error": "unknown op"}


def _lab_names():
    return {l.get("name") for l in COLLECTOR.snapshot().get("labs", [])}


def _free_mgmt_subnet():
    """A 172.20.X.0/24 no lab and no docker network uses yet."""
    import ipaddress
    used = [l.get("mgmt_subnet") for l in COLLECTOR.snapshot().get("labs", []) if l.get("mgmt_subnet")]
    rc, out, _ = run([DOCKER, "network", "inspect", "--format",
                      "{{range .IPAM.Config}}{{.Subnet}} {{end}}"]
                     + (run([DOCKER, "network", "ls", "-q"], timeout=20)[1].split() or ["none"]), timeout=30)
    used += out.split()
    nets = []
    for u in used:
        try:
            nets.append(ipaddress.ip_network(str(u), strict=False))
        except ValueError:
            pass
    for x in range(21, 250):
        cand = ipaddress.ip_network("172.20.%d.0/24" % x)
        if not any(cand.overlaps(n) for n in nets if n.version == 4):
            return str(cand)
    return "172.20.20.0/24"


def _snap_post(handler, p):
    lab = COLLECTOR.index().get(p.get("lab_id"))
    if lab is None:
        return 404, {"error": "unknown lab id"}
    who = handler._who()
    op = p.get("op")
    if op == "create":
        name, note = (p.get("name") or "").strip(), p.get("note") or ""
        if not snapshots.NAME_RE.match(name):
            return 400, {"error": "give the snapshot a name (letters, digits, space . _ : -, up to 60)"}
        audit("REQUEST snapshot-create lab=%s name=%r from=%s" % (lab.get("name"), name, who))
        job, err = JOBS.start(lab, "snapshot", [("save the config of every router as %r" % name,
                                                  lambda log: snapshots.create(
                                                      COLLECTOR.index().get(lab["id"]) or lab,
                                                      name, note, log))])
        return (409, {"error": err}) if job is None else (200, {"job_id": job.id})
    if op == "delete":
        audit("SNAPSHOT-DELETE lab=%s id=%s from=%s" % (lab.get("name"), p.get("id"), who))
        return 200, snapshots.delete(lab, p.get("id"))
    if op == "restore":
        mode = p.get("mode")
        if mode not in ("live", "redeploy"):
            return 400, {"error": "mode must be live or redeploy"}
        only = [n for n in (p.get("nodes") or []) if isinstance(n, str)]
        steps = snapshots.restore_steps(lab, p.get("id"), mode, only, COLLECTOR.index, CLAB, audit)
        audit("REQUEST snapshot-restore lab=%s id=%s mode=%s nodes=%s from=%s"
              % (lab.get("name"), p.get("id"), mode, ",".join(only) or "all", who))
        job, err = JOBS.start(lab, "restore", steps)
        return (409, {"error": err}) if job is None else (200, {"job_id": job.id})
    return 400, {"error": "unknown op"}


def _import_post(handler, p):
    """Import from git / a containerlab example / a catalogue template."""
    who = handler._who()
    src = p.get("source")
    want = (p.get("name") or "").strip().lower() or None
    if want and not labio.NAME_RE.match(want):
        return 400, {"error": "the lab name must match [a-z][a-z0-9-]{0,30}"}
    if src == "template":
        if not want:
            return 400, {"error": "give the new lab a name"}
        if os.path.exists(os.path.join(TOPO_BASE, want)):
            return 409, {"error": "%s already exists" % os.path.join(TOPO_BASE, want)}
        scn = p.get("scenario")
        if scn or labio.built(p.get("template")):
            # a hand-built design or an advanced scenario: its generator writes every file
            if scn:
                files = labio.scenario_files(scn, p.get("preset") or "", want, p.get("images") or {},
                                             _free_mgmt_subnet())
            else:
                files = labio.built_files(p.get("template"), want, p.get("images") or {}, _free_mgmt_subnet())
            target = os.path.join(TOPO_BASE, want)
            for rel, text in files.items():
                full = os.path.normpath(os.path.join(target, rel))
                if not full.startswith(target + os.sep):
                    return 400, {"error": "refusing to write outside %s" % target}
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w") as fh:
                    fh.write(text)
                if rel.endswith(".py"):
                    os.chmod(full, 0o755)
            topo = os.path.join(target, "%s.clab.yml" % want)
            audit("CATALOG %s -> %s from=%s" % (("scenario=%s preset=%s" % (scn, p.get("preset"))) if scn
                                                else "template=%s" % p.get("template"), want, who))
            return 200, {"topology_path": topo, "lab_id": lab_id(topo), "warnings": [],
                         "written": sorted(files)}
        spec = labio.template_spec(p.get("template"), want, p.get("images") or {})
        spec["mgmt_subnet"] = _free_mgmt_subnet()
        status, body = _topology_impl({"spec": spec})
        if status == 200:
            audit("CATALOG template=%s -> %s from=%s" % (p.get("template"), want, who))
        return status, body
    if src == "example":
        lines = []
        res = labio.import_example(p.get("example"), want, _lab_names(), lines.append)
        audit("IMPORT example %s -> %s from=%s" % (p.get("example"), res["dir"], who))
        res["log"] = lines
        return 200, res
    if src == "git":
        url, sub = p.get("url") or "", p.get("subdir") or ""
        if not labio.GIT_URL_RE.match(url):
            return 400, {"error": "give an https:// git URL"}
        audit("REQUEST import-git %s subdir=%s from=%s" % (url, sub or "-", who))

        def clone(log):
            res = labio.import_git(url, sub, want, _lab_names(), log)
            log("imported into %s" % res["dir"])
        job, err = IMG_JOBS.start({"id": "import", "name": url}, "import", [("import " + url, clone)])
        return (409, {"error": err}) if job is None else (200, {"job_id": job.id})
    return 400, {"error": "unknown source"}


def _trace_post(handler, p):
    """Walk the forwarding tables from a node to an address (or another node)."""
    lab = COLLECTOR.index().get(p.get("lab_id"))
    if lab is None:
        return 404, {"error": "unknown lab id"}
    if not lab.get("running"):
        return 409, {"error": "the lab is not running"}
    src, dst = str(p.get("src") or ""), str(p.get("dst") or "").strip()
    names = {n["name"] for n in lab.get("nodes") or []}
    if src not in names:
        return 400, {"error": "pick a source node"}
    poller = LIVE.poller(lab["id"])
    dst_node = None
    if dst in names:
        dst_node = dst
        dst = pathtrace.node_address(lab, poller, dst)
        if not dst:
            return 400, {"error": "could not find an address on %s - give an IP" % dst_node}
    try:
        res = pathtrace.trace(lab, poller, src, dst, p.get("src_vrf") or None)
    except pathtrace.TraceError as exc:
        return 400, {"error": str(exc)}
    res.update({"src": src, "dst": dst, "dst_node": dst_node, "when": time.time()})
    return 200, res


def _conv_post(handler, p):
    lab = COLLECTOR.index().get(p.get("lab_id"))
    if lab is None:
        return 404, {"error": "unknown lab id"}
    op, who = p.get("op"), handler._who()
    if op == "save":
        return 200, convergence.save(lab, p.get("scenario") or {})
    if op == "delete":
        return 200, convergence.delete(lab, p.get("id"))
    if op == "delete_run":
        return 200, convergence.delete_run(lab, p.get("id"))
    if op == "run":
        if not lab.get("running"):
            return 409, {"error": "the lab is not running"}
        sc = convergence.validate(lab, p.get("scenario") or {})
        audit("REQUEST convergence lab=%s test=%r link=%s from=%s"
              % (lab.get("name"), sc["name"], linkctl.link_key(sc["link"]), who))
        job, err = JOBS.start(lab, "convergence test", [("convergence test %r" % sc["name"],
            lambda log: convergence.run(COLLECTOR.index, lab["id"], sc, LINKS, LIVE, audit, log))])
        return (409, {"error": err}) if job is None else (200, {"job_id": job.id})
    return 400, {"error": "unknown op"}


def _guide_run_post(handler, p):
    """Run one check of the lab's guide (the command comes from the guide file, never the client)."""
    lab = COLLECTOR.index().get(p.get("lab_id"))
    if lab is None:
        return 404, {"error": "unknown lab id"}
    cid = str(p.get("check") or "")[:16]
    audit("GUIDE check lab=%s check=%s from=%s" % (lab.get("name"), cid, handler._who()))
    try:
        return 200, guide.run(lab, LIVE.poller(lab["id"]) if lab.get("running") else None, cid)
    except guide.GuideError as exc:
        return exc.code, {"error": str(exc)}


def _capture_post(handler, p):
    who = handler._who()
    op = p.get("op")
    if op == "start":
        lab = COLLECTOR.index().get(p.get("lab_id"))
        if lab is None:
            return 404, {"error": "unknown lab id"}
        return 200, CAPTURES.start(lab, p.get("node"), p.get("port"), p.get("filter"),
                                   p.get("max_packets"), p.get("max_seconds"), who)
    if op == "stop":
        return 200, CAPTURES.stop(p.get("id"), who)
    if op == "delete":
        return 200, CAPTURES.delete(p.get("id"), who)
    return 400, {"error": "unknown op"}


MANAGE_POST = {
    "/api/trace": _trace_post,
    "/api/guide/run": _guide_run_post,
    "/api/convergence": _conv_post,
    "/api/snapshots": _snap_post,
    "/api/lab/import": _import_post,
    "/api/link": _link_post,
    "/api/capture": _capture_post,
    "/api/topo/plan": _topo_plan_post,
    "/api/topo/apply": _topo_apply_post,
    "/api/edit/file": _edit_post,
    "/api/images": _images_post,
    "/api/networks": _networks_post,
    "/api/lan/settings": _lan_settings_post,
    "/api/lan/suggest": _lan_suggest_post,
    "/api/lan/lab": _lan_lab_post,
    "/api/trash": _trash_post,
}


for _p, _fn in devcfg.POST_ROUTES.items():
    MANAGE_POST[_p] = (lambda fn: lambda h, p: fn(
        p, COLLECTOR.index(), lambda m: audit(m + " from=%s" % h._who())))(_fn)


def main():
    global CLAB_VERSION, AUTH
    if not shutil.which("clab") and not os.path.exists(CLAB):
        raise SystemExit("clab not found at %s" % CLAB)
    CLAB_VERSION = clab_version()
    AUTH = auth.Auth(log=audit)
    if not AUTH.enabled:
        print("login is switched off - anyone who reaches the port controls the labs", flush=True)
    LAN.start_gateway(log=audit)
    COLLECTOR.start()
    LINKS.resume(COLLECTOR.index)
    # give the first collection a moment so the UI's first paint has data
    for _ in range(30):
        if COLLECTOR.snapshot().get("updated"):
            break
        time.sleep(0.2)
    srv = ThreadingHTTPServer((BIND_HOST, BIND_PORT), Handler)
    srv.daemon_threads = True
    print("clab-dashboard listening on %s:%d" % (BIND_HOST, BIND_PORT), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
