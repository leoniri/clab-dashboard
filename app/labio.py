#!/usr/bin/env python3
"""
Getting labs in and out: export as a zip, import from an archive, a git
repository or containerlab's bundled examples, and a catalogue of ready-made
labs.

Everything imported lands in its own new directory under TOPO_BASE - never on
top of an existing one - and is picked up by the normal lab discovery.
Archives are unpacked member by member: absolute paths, `..`, links and
device files are refused, and there are size and file-count limits.

The catalogue has three shelves:
  templates   builder specs (TEMPLATES below) generated through builder.py,
              with each node's image picked from the local images of its kind
  examples    containerlab's own lab-examples (/etc/containerlab/lab-examples)
  community   public lab repositories that need only freely pullable images
"""

import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
import zipfile

import yaml
import sysbin

TOPO_BASE = "/opt/clab-topologies"
STAGE_DIR = "/var/lib/clab-dashboard/import"
EXAMPLES_DIR = "/etc/containerlab/lab-examples"
GIT = sysbin.find("git")
MAX_FILES = 5000
MAX_UNPACKED = 500 * 1024 * 1024
MAX_EXPORT_FILE = 100 * 1024 * 1024
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
GIT_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(:[0-9]+)?/[A-Za-z0-9._~/-]+?(\.git)?/?$")
TOPO_RE = re.compile(r"\.clab\.ya?ml$")

# clab accepts short aliases for some kinds
KIND_ALIASES = {"srl": "nokia_srlinux", "ceos": "arista_ceos", "crpd": "juniper_crpd",
                "sonic-vs": "sonic-vs", "vr-sros": "nokia_sros", "xrd": "cisco_xrd",
                "c8000v": "cisco_c8000v", "vr-csr": "cisco_csr1000v", "vr-n9kv": "cisco_n9kv",
                "vr-xrv9k": "cisco_xrv9k", "vr-vmx": "juniper_vmx", "vr-veos": "arista_veos"}
# kinds that run from a public image anyone can pull
FREE_KINDS = {"linux", "nokia_srlinux", "bridge", "ovs-bridge", "host", "ext-container",
              "k8s-kind", "sonic-vs"}

PUBLIC_IMAGES = {"frr": "quay.io/frrouting/frr:10.4.1",
                 "nokia_srlinux": "ghcr.io/nokia/srlinux:latest",
                 "linux": "alpine:3.20"}

COMMUNITY = [
    {"id": "srl-getting-started", "url": "https://github.com/srl-labs/srlinux-getting-started",
     "title": "SR Linux getting started",
     "summary": "Three SR Linux nodes and two clients - the official SR Linux tutorial lab.",
     "tags": ["SR Linux", "starter"]},
    {"id": "srl-rt5-l3evpn", "url": "https://github.com/srl-labs/srl-rt5-l3evpn-basics-lab",
     "title": "L3 EVPN (RT-5) basics",
     "summary": "EVPN route-type 5 between SR Linux leaves and FRR - pairs with the SR Linux L3 EVPN tutorial.",
     "tags": ["SR Linux", "FRR", "EVPN"]},
    {"id": "srl-evpn-mh", "url": "https://github.com/srl-labs/srl-evpn-mh-lab",
     "title": "EVPN multihoming",
     "summary": "SR Linux leaf/spine fabric with an all-active multihomed host.",
     "tags": ["SR Linux", "EVPN"]},
    {"id": "srl-telemetry", "url": "https://github.com/srl-labs/srl-telemetry-lab",
     "title": "Streaming telemetry stack",
     "summary": "SR Linux fabric with gnmic, Prometheus, Loki and Grafana dashboards (about 10 containers).",
     "tags": ["SR Linux", "telemetry", "gNMI"]},
    {"id": "srl-opergroup", "url": "https://github.com/srl-labs/opergroup-lab",
     "title": "Oper-groups with telemetry",
     "summary": "SR Linux oper-group demo: uplink failures propagate to host-facing ports, watched in Grafana.",
     "tags": ["SR Linux", "telemetry"]},
    {"id": "srl-ansible", "url": "https://github.com/srl-labs/intent-based-ansible-lab",
     "title": "Intent-based fabric with Ansible",
     "summary": "Six SR Linux nodes configured from Ansible intents.",
     "tags": ["SR Linux", "automation"]},
]


def _grid(names_xy):
    return [{"id": n, "name": n, "x": x, "y": y} for n, x, y in names_xy]


TEMPLATES = [
    {"id": "frr-srmpls-l3vpn", "title": "SR-MPLS L3VPN core (FRR)",
     "summary": "Four FRR routers in a ring running IS-IS with SR-MPLS, a route reflector, two PEs "
                "and two customer sites in one VRF. Everything free, boots in seconds.",
     "tags": ["FRR", "IS-IS", "SR-MPLS", "BGP", "L3VPN"],
     "spec": {"igp": "isis", "services": {"sr": True, "bgp": "rr", "l3vpn": True},
              "nodes": [dict(n, kind="frr") for n in _grid(
                  [("pe1", 60, 160), ("rr1", 260, 40), ("p1", 260, 280), ("pe2", 460, 160),
                   ("ce1", -140, 160), ("ce2", 660, 160)])],
              "roles": {"pe1": "PE", "pe2": "PE", "rr1": "RR", "p1": "P", "ce1": "CE", "ce2": "CE"},
              "links": [("pe1", "rr1"), ("rr1", "pe2"), ("pe2", "p1"), ("p1", "pe1"),
                        ("ce1", "pe1"), ("ce2", "pe2")]}},
    {"id": "srl-isis-bgp", "title": "IS-IS + BGP core (SR Linux)",
     "summary": "Four SR Linux routers in a square with IS-IS and full-mesh iBGP; two FRR customer "
                "routers peer over eBGP and reach each other across the core.",
     "tags": ["SR Linux", "FRR", "IS-IS", "BGP"],
     "spec": {"igp": "isis", "services": {"sr": False, "bgp": "full-mesh", "l3vpn": False},
              "nodes": [dict(n, kind="nokia_srlinux", icon="router") for n in _grid(
                  [("pe1", 60, 60), ("p1", 300, 60), ("p2", 60, 260), ("pe2", 300, 260)])]
                       + [dict(n, kind="frr") for n in _grid([("ce1", -160, 60), ("ce2", 520, 260)])],
              "roles": {"pe1": "PE", "pe2": "PE", "p1": "P", "p2": "P", "ce1": "CE", "ce2": "CE"},
              "links": [("pe1", "p1"), ("p1", "pe2"), ("pe2", "p2"), ("p2", "pe1"),
                        ("ce1", "pe1"), ("ce2", "pe2")]}},
    {"id": "frr-ospf-starter", "title": "OSPF starter (FRR)",
     "summary": "Three FRR routers in a triangle running OSPF area 0, with a host behind two of them. "
                "A small lab to try link failures, impairments and packet capture on.",
     "tags": ["FRR", "OSPF", "starter"],
     "spec": {"igp": "ospf", "services": {},
              "nodes": [dict(n, kind="frr") for n in _grid([("r1", 60, 60), ("r2", 360, 60), ("r3", 210, 260)])]
                       + [dict(n, kind="linux") for n in _grid([("h1", -140, 60), ("h2", 560, 60)])],
              "links": [("r1", "r2"), ("r2", "r3"), ("r3", "r1"), ("h1", "r1"), ("h2", "r2")]}},
    {"id": "multivendor-isis", "title": "Multi-vendor IS-IS interop",
     "summary": "IOS-XE, IOS-XR, SR Linux and FRR in one IS-IS ring - watch the adjacencies come up "
                "between four implementations. Needs the IOS-XE and IOS-XR images.",
     "tags": ["IOS-XE", "IOS-XR", "SR Linux", "FRR", "IS-IS"],
     "spec": {"igp": "isis", "services": {},
              "nodes": [{"id": "xe1", "name": "xe1", "kind": "cisco_c8000v", "x": 60, "y": 60},
                        {"id": "xr1", "name": "xr1", "kind": "cisco_xrd_vrouter", "x": 360, "y": 60},
                        {"id": "srl1", "name": "srl1", "kind": "nokia_srlinux", "icon": "router", "x": 360, "y": 260},
                        {"id": "frr1", "name": "frr1", "kind": "frr", "x": 60, "y": 260}],
              "links": [("xe1", "xr1"), ("xr1", "srl1"), ("srl1", "frr1"), ("frr1", "xe1")]}},
    {"id": "mixed-srmpls-l3vpn", "title": "Mixed-vendor SR-MPLS L3VPN",
     "summary": "IOS-XE and IOS-XR PEs across an FRR P core with SR-MPLS and VPNv4; FRR CEs in one "
                "VRF. Needs the IOS-XE and IOS-XR images.",
     "tags": ["IOS-XE", "IOS-XR", "FRR", "SR-MPLS", "L3VPN"],
     "spec": {"igp": "isis", "services": {"sr": True, "bgp": "full-mesh", "l3vpn": True},
              "nodes": [{"id": "pe1", "name": "pe1", "kind": "cisco_c8000v", "x": 60, "y": 160},
                        {"id": "p1", "name": "p1", "kind": "frr", "x": 260, "y": 60},
                        {"id": "p2", "name": "p2", "kind": "frr", "x": 260, "y": 260},
                        {"id": "pe2", "name": "pe2", "kind": "cisco_xrd_vrouter", "x": 460, "y": 160},
                        {"id": "ce1", "name": "ce1", "kind": "frr", "x": -140, "y": 160},
                        {"id": "ce2", "name": "ce2", "kind": "frr", "x": 660, "y": 160}],
              "roles": {"pe1": "PE", "pe2": "PE", "p1": "P", "p2": "P", "ce1": "CE", "ce2": "CE"},
              "links": [("pe1", "p1"), ("p1", "pe2"), ("pe2", "p2"), ("p2", "pe1"),
                        ("ce1", "pe1"), ("ce2", "pe2")]}},
]


# Hand-built labs (catlabs.py): complete SP designs the builder cannot draw.
# counts = nodes per image slot, for the image pickers and the RAM estimate.
import catlabs
BUILT = [
    {"id": "xr-sp-srmpls", "gen": catlabs.sp_srmpls, "links": 16,
     "title": "SP core — SR-MPLS L3VPN (IOS-XR)",
     "summary": "Three PEs, three P routers and a route reflector on IOS-XR, every PE dual-homed. IS-IS with "
                "SR-MPLS and TI-LFA, VPNv4 + VPNv6 reflected by the RR, five CEs in two VRFs.",
     "tags": ["IOS-XR", "SR-MPLS", "TI-LFA", "RR", "L3VPN", "6VPE"],
     "counts": {"cisco_xrd_vrouter": 7, "frr": 5}},
    {"id": "xr-sp-srv6", "gen": catlabs.sp_srv6, "links": 16,
     "title": "SP core — SRv6 uSID L3VPN (IOS-XR)",
     "summary": "The same SP core on an IPv6-only SRv6 micro-SID transport: locators per node, TI-LFA, VPNv4 "
                "+ VPNv6 with per-VRF uDT4/uDT6 SIDs through the RR, five CEs in two VRFs.",
     "tags": ["IOS-XR", "SRv6", "uSID", "TI-LFA", "RR", "L3VPN"],
     "counts": {"cisco_xrd_vrouter": 7, "frr": 5}},
]


def built(tid):
    return next((b for b in BUILT if b["id"] == tid), None)


def built_files(tid, name, images, mgmt):
    b = built(tid)
    for k in b["counts"]:
        if not images.get(k):
            raise ImportError_("no image for %s" % k)
    return b["gen"](name, images, mgmt)


class ImportError_(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _free_dir(name):
    """A new directory name under TOPO_BASE: name, name-2, name-3, ..."""
    base = re.sub(r"[^a-z0-9-]", "-", (name or "lab").lower()).strip("-")[:28] or "lab"
    if not base[0].isalpha():
        base = "lab-" + base
    cand, i = base, 1
    while os.path.exists(os.path.join(TOPO_BASE, cand)):
        i += 1
        cand = "%s-%d" % (base, i)
    return cand


def _topologies(root):
    out = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and not d.startswith("clab-")]
        for f in files:
            if TOPO_RE.search(f) and not f.startswith(".state"):
                out.append(os.path.relpath(os.path.join(dirpath, f), root))
    return sorted(out)


def _topo_facts(path):
    try:
        with open(path) as fh:
            doc = yaml.safe_load(fh) or {}
    except Exception:                                   # noqa: BLE001
        return {"name": None, "kinds": {}, "images": []}
    topo = doc.get("topology") or {}
    kinds, images = {}, set()
    kcfg = topo.get("kinds") or {}
    groups = topo.get("groups") or {}
    defaults = topo.get("defaults") or {}
    for n, c in (topo.get("nodes") or {}).items():
        c = c if isinstance(c, dict) else {}
        g = groups.get(c.get("group")) if isinstance(groups, dict) else None
        g = g if isinstance(g, dict) else {}
        k = c.get("kind") or g.get("kind") or defaults.get("kind") or "linux"
        k = KIND_ALIASES.get(k, k)
        kinds[k] = kinds.get(k, 0) + 1
        img = (c.get("image") or g.get("image") or ((kcfg.get(c.get("kind") or g.get("kind")) or {})
               .get("image") if isinstance(kcfg, dict) else None) or defaults.get("image"))
        if img:
            images.add(str(img))
    return {"name": doc.get("name"), "kinds": kinds, "images": sorted(images)}


def _local_images():
    try:
        out = subprocess.run([sysbin.find("docker"), "images", "--format", "{{.Repository}}:{{.Tag}}"],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:                                   # noqa: BLE001
        return set()
    return set(out.split())


def _norm_image(ref):
    return ref if ":" in ref.rsplit("/", 1)[-1] else ref + ":latest"


def _rename_lab(topo_path, new_name, log):
    """Point `name:` at the new name (text edit, comments kept)."""
    with open(topo_path) as fh:
        text = fh.read()
    new, n = re.subn(r"^name:\s*.*$", "name: %s" % new_name, text, count=1, flags=re.M)
    if n:
        with open(topo_path, "w") as fh:
            fh.write(new)
        log("lab renamed to %s (another lab already uses its name)" % new_name)


def _chown_tree(path, uid=0, gid=0):
    for dirpath, dirs, files in os.walk(path):
        os.chown(dirpath, uid, gid)
        for f in files:
            try:
                os.chown(os.path.join(dirpath, f), uid, gid, follow_symlinks=False)
            except OSError:
                pass


def place(stage_root, want_name, taken_names, log, source):
    """Move an unpacked tree into TOPO_BASE/<free name>. Returns a result dict."""
    topos = _topologies(stage_root)
    if not topos:
        raise ImportError_("no *.clab.yml topology file in it")
    # a single top-level directory is the lab itself, not a wrapper around it
    entries = [e for e in os.listdir(stage_root) if not e.startswith(".")]
    root = stage_root
    if len(entries) == 1 and os.path.isdir(os.path.join(stage_root, entries[0])):
        root = os.path.join(stage_root, entries[0])
    first = _topo_facts(os.path.join(root, _topologies(root)[0]))
    dname = _free_dir(want_name or first.get("name") or os.path.basename(root))
    dest = os.path.join(TOPO_BASE, dname)
    os.makedirs(TOPO_BASE, exist_ok=True)
    shutil.move(root, dest)
    _chown_tree(dest)
    placed = []
    for rel in _topologies(dest):
        full = os.path.join(dest, rel)
        facts = _topo_facts(full)
        if facts.get("name") in taken_names:
            nn = dname if len(_topologies(dest)) == 1 else "%s-%s" % (dname, re.sub(
                r"[^a-z0-9-]", "-", str(facts["name"]).lower()))[:31]
            _rename_lab(full, nn, log)
            facts["name"] = nn
        placed.append({"path": full, "name": facts.get("name"), "kinds": facts["kinds"],
                       "images": facts["images"]})
        log("topology %s (lab %s)" % (full, facts.get("name")))
    with open(os.path.join(dest, ".imported.json"), "w") as fh:
        json.dump({"source": source, "when": time.time()}, fh, indent=1)
    local = _local_images()
    missing = sorted({i for p in placed for i in p["images"] if _norm_image(i) not in local})
    if missing:
        log("images not on this host yet (containerlab pulls public ones at deploy): %s"
            % ", ".join(missing))
    return {"dir": dest, "labs": placed, "missing_images": missing}


# --------------------------------------------------------------------------
# archives
# --------------------------------------------------------------------------

def _safe_member(name):
    n = name.replace("\\", "/")
    if n.startswith("/") or "\x00" in n:
        return None
    parts = [p for p in n.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def unpack(archive, dest):
    """Unpack a .zip / .tar(.gz|.xz|.bz2) / single topology file into dest, safely."""
    os.makedirs(dest, exist_ok=True)
    total, count = 0, 0
    low = archive.lower()
    if TOPO_RE.search(low):
        shutil.copy(archive, os.path.join(dest, os.path.basename(archive).split("-", 2)[-1]))
        return
    if low.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                rel = _safe_member(info.filename)
                if rel is None:
                    raise ImportError_("unsafe path in the archive: %s" % info.filename)
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    continue                            # symlink: skip
                if info.is_dir():
                    os.makedirs(os.path.join(dest, rel), exist_ok=True)
                    continue
                count += 1
                total += info.file_size
                if count > MAX_FILES or total > MAX_UNPACKED:
                    raise ImportError_("the archive is too big (limit %d files / %d MB)"
                                       % (MAX_FILES, MAX_UNPACKED // 1024 // 1024))
                out = os.path.join(dest, rel)
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with z.open(info) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        return
    try:
        tf = tarfile.open(archive)
    except tarfile.TarError:
        raise ImportError_("not a zip or tar archive")
    with tf:
        for m in tf.getmembers():
            rel = _safe_member(m.name)
            if rel is None:
                raise ImportError_("unsafe path in the archive: %s" % m.name)
            if m.isdir():
                os.makedirs(os.path.join(dest, rel), exist_ok=True)
                continue
            if not m.isfile():
                continue                                # links, devices, fifos
            count += 1
            total += m.size
            if count > MAX_FILES or total > MAX_UNPACKED:
                raise ImportError_("the archive is too big (limit %d files / %d MB)"
                                   % (MAX_FILES, MAX_UNPACKED // 1024 // 1024))
            out = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            src = tf.extractfile(m)
            with open(out, "wb") as dst:
                shutil.copyfileobj(src, dst)


def import_archive(path, want_name, taken, log):
    stage = os.path.join(STAGE_DIR, time.strftime("%Y%m%d-%H%M%S") + "-%d" % os.getpid())
    try:
        unpack(path, stage)
        return place(stage, want_name, taken, log, {"archive": os.path.basename(path)})
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def import_git(url, subdir, want_name, taken, log):
    if not GIT_URL_RE.match(url or ""):
        raise ImportError_("give an https:// git URL")
    sub = _safe_member(subdir) if subdir else None
    if subdir and sub is None:
        raise ImportError_("bad sub-directory")
    stage = os.path.join(STAGE_DIR, time.strftime("%Y%m%d-%H%M%S") + "-git")
    try:
        os.makedirs(STAGE_DIR, exist_ok=True)
        log("cloning %s" % url)
        p = subprocess.run([GIT, "clone", "--depth", "1", "--quiet", url, stage],
                           capture_output=True, text=True, timeout=600,
                           env={"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin"})
        if p.returncode != 0:
            raise ImportError_("git clone failed: %s" % (p.stderr.strip().splitlines() or ["?"])[-1])
        shutil.rmtree(os.path.join(stage, ".git"), ignore_errors=True)
        src = stage
        if sub:
            src = os.path.join(stage, sub)
            if not os.path.isdir(src):
                raise ImportError_("%s is not a directory of that repository" % sub)
        wrap = stage + "-wrap"
        os.makedirs(wrap)
        shutil.move(src, os.path.join(wrap, "lab"))
        name = want_name or re.sub(r"\.git$", "", url.rstrip("/").rsplit("/", 1)[-1])
        return place(wrap, name, taken, log, {"git": url, "subdir": sub})
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(stage + "-wrap", ignore_errors=True)


def import_example(example, want_name, taken, log):
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,60}$", example or ""):
        raise ImportError_("bad example name")
    src = os.path.join(EXAMPLES_DIR, example)
    if not os.path.isdir(src):
        raise ImportError_("no such example", 404)
    stage = os.path.join(STAGE_DIR, time.strftime("%Y%m%d-%H%M%S") + "-ex")
    try:
        os.makedirs(stage)
        shutil.copytree(src, os.path.join(stage, example), symlinks=False)
        return place(stage, want_name or example, taken, log, {"example": example})
    finally:
        shutil.rmtree(stage, ignore_errors=True)


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------

def export_members(lab, dedicated, with_snapshots):
    """[(absolute path, name in the zip)] for a lab."""
    import editor
    base = os.path.dirname(lab["path"])
    top = re.sub(r"[^A-Za-z0-9_.-]", "_", lab.get("name") or "lab")
    out, skipped = [], []

    def add(full):
        if os.path.islink(full) or not os.path.isfile(full):
            return
        if os.path.getsize(full) > MAX_EXPORT_FILE:
            skipped.append(os.path.relpath(full, base))
            return
        out.append((full, "%s/%s" % (top, os.path.relpath(full, base))))

    def walk(d):
        for dirpath, dirs, files in os.walk(d):
            keep = []
            for x in dirs:
                p = os.path.join(dirpath, x)
                if x == editor.HISTORY or (x.startswith("clab-") and os.path.isfile(
                        os.path.join(p, ".state.clab.yaml"))):
                    continue
                if x == ".clabd-snapshots" and not with_snapshots:
                    continue
                if x.startswith(".") and x != ".clabd-snapshots":
                    continue
                keep.append(x)
            dirs[:] = keep
            for f in files:
                if f.startswith(".") and f not in (".builder.json",) and ".clabd-snapshots" not in dirpath:
                    continue
                add(os.path.join(dirpath, f))

    if dedicated:
        walk(base)
    else:
        add(lab["path"])
        for f in editor.editable_files(lab):
            if f.get("exists") and f["path"] != os.path.basename(lab["path"]):
                add(os.path.join(base, f["path"]))
        for r in editor._refs(lab["path"]):
            full = editor._inside(base, r)
            if full and os.path.isdir(full):
                walk(full)
        if with_snapshots:
            snap = os.path.join(base, ".clabd-snapshots", re.sub(r"[^A-Za-z0-9_.-]", "_", lab.get("name") or ""))
            if os.path.isdir(snap):
                walk(snap)
    seen, uniq = set(), []
    for full, arc in out:
        if arc not in seen:
            seen.add(arc)
            uniq.append((full, arc))
    return uniq, skipped


def export_zip(lab, dedicated, with_snapshots):
    members, skipped = export_members(lab, dedicated, with_snapshots)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for full, arc in members:
            z.write(full, arc)
        if skipped:
            z.writestr("%s/SKIPPED.txt" % arc.split("/")[0],
                       "Left out of the export (over %d MB each):\n%s\n"
                       % (MAX_EXPORT_FILE // 1024 // 1024, "\n".join(skipped)))
    return buf.getvalue(), len(members), skipped


# --------------------------------------------------------------------------
# catalogue
# --------------------------------------------------------------------------

def _readme_summary(d):
    for fn in ("README.md", "readme.md", "README"):
        p = os.path.join(d, fn)
        if os.path.isfile(p):
            try:
                with open(p, errors="replace") as fh:
                    text = fh.read(4000)
            except OSError:
                return None
            text = re.sub(r"```.*?```", "", text, flags=re.S)
            for para in re.split(r"\n\s*\n", text):
                para = para.strip()
                if para and len(para) > 25 and not para.startswith(("#", "!", "<", "[!", "|", "$", "-", "*")):
                    return re.sub(r"\s+", " ", re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", para))[:260]
    return None


def examples():
    out = []
    try:
        names = sorted(os.listdir(EXAMPLES_DIR))
    except OSError:
        return out
    local = _local_images()
    for name in names:
        d = os.path.join(EXAMPLES_DIR, name)
        topos = _topologies(d) if os.path.isdir(d) else []
        if not topos:
            continue
        facts = [_topo_facts(os.path.join(d, t)) for t in topos]
        kinds, images = {}, set()
        for f in facts:
            for k, v in f["kinds"].items():
                kinds[k] = kinds.get(k, 0) + v
            images |= set(f["images"])
        missing = sorted(i for i in images if _norm_image(i) not in local)
        out.append({"id": name, "title": name, "summary": _readme_summary(d),
                    "kinds": kinds, "images": sorted(images), "missing_images": missing,
                    "free": all(k in FREE_KINDS for k in kinds), "topologies": topos})
    return out


def templates(builder):
    """Built-in templates with the image chosen per kind from local images."""
    imgs = builder.list_images()
    by_kind = {}
    for im in imgs:
        if im.get("kind"):
            by_kind.setdefault(im["kind"], []).append(im["image"])
    out = []
    for t in TEMPLATES:
        kinds = {}
        for n in t["spec"]["nodes"]:
            kinds[n["kind"]] = kinds.get(n["kind"], 0) + 1
        choice, missing = {}, []
        for k in kinds:
            if by_kind.get(k):
                choice[k] = by_kind[k][0]
            elif k in PUBLIC_IMAGES:
                choice[k] = PUBLIC_IMAGES[k]
                missing.append(PUBLIC_IMAGES[k])
            else:
                choice[k] = None
        ram = sum(builder.KINDS[n["kind"]]["ram_mb"] for n in t["spec"]["nodes"])
        out.append({"id": t["id"], "title": t["title"], "summary": t["summary"], "tags": t["tags"],
                    "kinds": kinds, "images": choice, "missing_images": missing,
                    "unavailable": sorted(k for k, v in choice.items() if not v),
                    "est_ram_mb": ram, "nodes": len(t["spec"]["nodes"]),
                    "links": len(t["spec"]["links"])})
    for t in BUILT:
        choice, missing = {}, []
        for k in t["counts"]:
            if by_kind.get(k):
                choice[k] = by_kind[k][0]
            elif k in PUBLIC_IMAGES:
                choice[k] = PUBLIC_IMAGES[k]
                missing.append(PUBLIC_IMAGES[k])
            else:
                choice[k] = None
        ram = sum(builder.KINDS[k]["ram_mb"] * n for k, n in t["counts"].items())
        out.append({"id": t["id"], "title": t["title"], "summary": t["summary"], "tags": t["tags"],
                    "kinds": dict(t["counts"]), "images": choice, "missing_images": missing,
                    "unavailable": sorted(k for k, v in choice.items() if not v),
                    "est_ram_mb": ram, "nodes": sum(t["counts"].values()), "links": t["links"],
                    "built": True})
    return out


# Advanced scenarios (scenarios.py): Inter-AS, CsC and other SP designs, one card
# per scenario with a choice of verified platform presets.
import scenarios

_SCN_CACHE = {}


def scenario_listing(builder):
    """Every scenario with its verified presets: images per slot, RAM, the drawing."""
    imgs = builder.list_images()
    by_kind = {}
    for im in imgs:
        if im.get("kind"):
            by_kind.setdefault(im["kind"], []).append(im["image"])
    out = []
    for sc in scenarios.SCENARIOS:
        presets = []
        for pid in sc["verified"]:
            key = (sc["id"], pid)
            if key not in _SCN_CACHE:
                _SCN_CACHE[key] = scenarios.describe(sc, pid)
            d = dict(_SCN_CACHE[key])
            choice, missing = {}, []
            for k in d["kinds"]:
                if by_kind.get(k):
                    choice[k] = by_kind[k][0]
                elif k in PUBLIC_IMAGES:
                    choice[k] = PUBLIC_IMAGES[k]
                    missing.append(PUBLIC_IMAGES[k])
                else:
                    choice[k] = None
            d.update(images=choice, missing_images=missing,
                     unavailable=sorted(k for k, v in choice.items() if not v))
            presets.append(d)
        if presets:
            out.append({"id": sc["id"], "family": sc["family"], "title": sc["title"], "summary": sc["summary"],
                        "tags": sc["tags"], "presets": presets})
    return out


def scenario_files(sid, preset, name, images, mgmt):
    sc = scenarios.scenario(sid)
    if sc is None or preset not in sc["verified"]:
        raise ImportError_("no such scenario or preset", 404)
    need = scenarios.describe(sc, preset)["kinds"]
    for k in need:
        if not images.get(k):
            raise ImportError_("no image for %s" % k)
    return scenarios.generate(sid, preset, name, images, mgmt)


def template_spec(tid, name, images):
    t = next((t for t in TEMPLATES if t["id"] == tid), None)
    if t is None:
        raise ImportError_("no such template", 404)
    s = t["spec"]
    roles = s.get("roles") or {}
    nodes = []
    for n in s["nodes"]:
        img = (images or {}).get(n["kind"])
        if not img:
            raise ImportError_("no image for %s" % n["kind"])
        nn = {"id": n["id"], "name": n["name"], "kind": n["kind"], "image": img,
              "x": n["x"], "y": n["y"]}
        if n.get("icon"):
            nn["icon"] = n["icon"]
        if roles.get(n["name"]):
            nn["role"] = roles[n["name"]]
        nodes.append(nn)
    spec = {"name": name, "igp": s["igp"], "services": dict(s.get("services") or {}),
            "nodes": nodes, "links": [{"a": a, "b": b} for a, b in s["links"]]}
    return spec
