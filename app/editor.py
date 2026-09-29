#!/usr/bin/env python3
"""
Editing existing labs.

Two ways in:

* The file editor works on any lab. It edits text in place - the topology
  file is never parsed and re-serialised, so comments and layout survive.
  What it may touch is decided here, never by the client: the topology file,
  the files the topology references (startup-config, binds), a lab's
  configs/ directory, and README/ADDRESSING beside the topology. Nothing
  outside the lab's directory, nothing clab generated.

* Builder labs reopen in the builder. builder_spec() returns the spec the
  builder saved (.builder.json) or, for labs made before that existed, one
  reconstructed from the topology file's builder-* labels.

Every save first copies the current file to
<lab dir>/.clabd-history/<file>/<timestamp>, so any edit can be undone.
Discovery skips dot-directories, so history never shows up as a lab.
"""

import hashlib
import ipaddress
import os
import re
import shutil
import time

import yaml

HISTORY = ".clabd-history"
SPEC_FILE = ".builder.json"
MAX_BYTES = 1024 * 1024
KEEP_VERSIONS = 50
NEW_FILE_RE = re.compile(r"^configs/[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")
SIDE_FILES = ("README.md", "README.txt", "ADDRESSING.txt")


class EditError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# which files
# --------------------------------------------------------------------------

def _refs(topo_path):
    """Files a topology references, relative to its directory."""
    try:
        with open(topo_path) as fh:
            doc = yaml.safe_load(fh) or {}
    except Exception:                                    # noqa: BLE001
        return []
    topo = (doc.get("topology") or {}) if isinstance(doc, dict) else {}
    blocks = []
    for sect in ("defaults",):
        if isinstance(topo.get(sect), dict):
            blocks.append(topo[sect])
    for sect in ("kinds", "nodes", "groups"):
        if isinstance(topo.get(sect), dict):
            blocks += [v for v in topo[sect].values() if isinstance(v, dict)]
    out = []
    for b in blocks:
        sc = b.get("startup-config")
        if isinstance(sc, str) and "\n" not in sc:
            out.append(sc)
        for bind in b.get("binds") or []:
            if isinstance(bind, str):
                out.append(bind.split(":", 1)[0])
    return out


def _inside(base, rel):
    """Absolute path of rel if it stays inside base (symlinks resolved), else None."""
    if not rel or rel.startswith("/") or "\x00" in rel:
        return None
    full = os.path.normpath(os.path.join(base, rel))
    real = os.path.realpath(full)
    base_real = os.path.realpath(base)
    if not real.startswith(base_real + os.sep):
        return None
    parts = os.path.relpath(full, base).split(os.sep)
    if parts[0] == HISTORY or parts[0].startswith("clab-") or ".." in parts:
        return None
    return full


def editable_files(lab):
    """[{path (relative), group, exists}] for a lab - the whole allow-list."""
    topo = lab["path"]
    base = os.path.dirname(topo)
    seen, out = set(), []

    def add(rel, group):
        rel = os.path.normpath(rel)
        if rel in seen:
            return
        full = _inside(base, rel)
        if full is None:
            return
        if os.path.exists(full) and not os.path.isfile(full):
            return
        seen.add(rel)
        out.append({"path": rel, "group": group, "exists": os.path.isfile(full),
                    "size": os.path.getsize(full) if os.path.isfile(full) else 0})

    add(os.path.basename(topo), "topology")
    for r in _refs(topo):
        add(r, "referenced by the topology")
    cfg = os.path.join(base, "configs")
    if os.path.isdir(cfg):
        for fn in sorted(os.listdir(cfg)):
            if os.path.isfile(os.path.join(cfg, fn)) and not fn.startswith("."):
                add(os.path.join("configs", fn), "configs/")
    for fn in SIDE_FILES:
        if os.path.isfile(os.path.join(base, fn)):
            add(fn, "notes")
    # files that are gone (a node removed in the builder, say) but still have
    # history, so they can be looked at and brought back
    hist = os.path.join(base, HISTORY)
    if os.path.isdir(hist):
        for enc in sorted(os.listdir(hist)):
            rel = enc.replace("__", os.sep)
            if rel not in seen and not os.path.exists(os.path.join(base, rel)) \
                    and (rel == os.path.basename(topo) or NEW_FILE_RE.match(rel)
                         or rel in SIDE_FILES):
                add(rel, "removed - in history")
    return out


def _resolve(lab, rel, must_exist=True):
    allowed = {f["path"]: f for f in editable_files(lab)}
    rel = os.path.normpath(rel or "")
    if rel not in allowed:
        if not must_exist and NEW_FILE_RE.match(rel):
            full = _inside(os.path.dirname(lab["path"]), rel)
            if full:
                return full, rel
        raise EditError("%s is not one of this lab's editable files" % rel, 403)
    full = _inside(os.path.dirname(lab["path"]), rel)
    if full is None:
        raise EditError("refusing path %s" % rel, 403)
    return full, rel


# --------------------------------------------------------------------------
# read / write
# --------------------------------------------------------------------------

def read_file(lab, rel):
    full, rel = _resolve(lab, rel)
    if not os.path.isfile(full):
        return {"path": rel, "content": "", "sha": sha(""), "exists": False}
    if os.path.getsize(full) > MAX_BYTES:
        raise EditError("%s is larger than 1 MB - edit it on the host" % rel)
    with open(full, "rb") as fh:
        raw = fh.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise EditError("%s is not a UTF-8 text file" % rel)
    return {"path": rel, "content": text, "sha": sha(text), "exists": True,
            "mtime": os.path.getmtime(full)}


def _own_like(path, ref):
    """Give path (and any history dirs above it) the owner of ref, so a lab in
    someone's home directory does not collect root-owned files."""
    try:
        st = os.stat(ref)
        top = os.path.join(ref, HISTORY)
        p = path
        while True:
            os.chown(p, st.st_uid, st.st_gid)
            if p == top or os.path.dirname(p) == p or not p.startswith(top):
                break
            p = os.path.dirname(p)
    except OSError:
        pass


def _hist_dir(lab, rel):
    base = os.path.dirname(lab["path"])
    return os.path.join(base, HISTORY, rel.replace(os.sep, "__"))


def _snapshot(lab, rel, full, reason):
    """Copy the current file into history. Returns the version id or None."""
    if not os.path.isfile(full):
        return None
    d = _hist_dir(lab, rel)
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, _unique_stamp([d]))
    shutil.copy2(full, dst)
    with open(dst + ".why", "w") as fh:
        fh.write(reason)
    base = os.path.dirname(lab["path"])
    _own_like(dst + ".why", base)
    _own_like(dst, base)
    # keep the newest KEEP_VERSIONS; older ones go (they are our own copies)
    vers = sorted(v for v in os.listdir(d) if not v.endswith(".why"))
    for old in vers[:-KEEP_VERSIONS]:
        for p in (os.path.join(d, old), os.path.join(d, old + ".why")):
            try:
                os.remove(p)
            except OSError:
                pass
    return os.path.basename(dst)


def _write_atomic(full, text):
    """Replace full with text, keeping the original owner and mode."""
    st = os.stat(full) if os.path.exists(full) else None
    parent = os.path.dirname(full)
    if st is None:
        pst = os.stat(parent)
    tmp = full + ".clabd-tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    if st is not None:
        os.chmod(tmp, st.st_mode & 0o7777)
        os.chown(tmp, st.st_uid, st.st_gid)
    else:
        os.chmod(tmp, 0o644)
        os.chown(tmp, pst.st_uid, pst.st_gid)
    os.replace(tmp, full)


def check_topology(text, lab):
    """Parse-check a topology before it is saved. Returns (errors, warnings)."""
    errors, warnings = [], []
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = " (line %d, column %d)" % (mark.line + 1, mark.column + 1) if mark else ""
        return ["YAML does not parse%s: %s" % (where, getattr(exc, "problem", None) or exc)], []
    if not isinstance(doc, dict):
        return ["the file must be a YAML mapping"], []
    name = doc.get("name")
    if not name or not isinstance(name, str):
        errors.append("'name' is missing")
    topo = doc.get("topology")
    if not isinstance(topo, dict):
        errors.append("'topology' is missing or not a mapping")
        return errors, warnings
    nodes = topo.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        errors.append("topology.nodes must be a non-empty mapping")
        nodes = {}
    kinds = topo.get("kinds") if isinstance(topo.get("kinds"), dict) else {}
    defaults = topo.get("defaults") if isinstance(topo.get("defaults"), dict) else {}
    for nname, ncfg in nodes.items():
        ncfg = ncfg if isinstance(ncfg, dict) else {}
        kind = ncfg.get("kind") or defaults.get("kind")
        if not kind:
            errors.append("node %s has no kind (and topology.defaults has none)" % nname)
        elif not (ncfg.get("image") or (kinds.get(kind) or {}).get("image")
                  or defaults.get("image")) and kind not in ("bridge", "ovs-bridge", "host"):
            warnings.append("node %s: no image for kind %s" % (nname, kind))
        ip = ncfg.get("mgmt-ipv4")
        if ip:
            try:
                ipaddress.ip_address(str(ip))
            except ValueError:
                errors.append("node %s: mgmt-ipv4 %r is not an address" % (nname, ip))
        sc = ncfg.get("startup-config")
        if isinstance(sc, str) and "\n" not in sc and not sc.startswith("/"):
            if not os.path.isfile(os.path.join(os.path.dirname(lab["path"]), sc)):
                warnings.append("node %s: startup-config %s does not exist yet" % (nname, sc))
    special = ("host", "mgmt-net", "macvlan", "vxlan", "vxlan-stitch", "dummy", "bridge")
    ports = {}
    for i, link in enumerate(topo.get("links") or [], 1):
        eps = link.get("endpoints") if isinstance(link, dict) else None
        if not isinstance(eps, list) or len(eps) != 2:
            if isinstance(link, dict) and link.get("type"):
                continue                   # new-style typed link; clab checks it
            errors.append("link %d must have exactly two endpoints" % i)
            continue
        for ep in eps:
            if not isinstance(ep, str) or ":" not in ep:
                errors.append("link %d: endpoint %r must be node:interface" % (i, ep))
                continue
            n, _, itf = ep.partition(":")
            if n not in nodes and n not in special:
                errors.append("link %d: %s is not a node in this topology" % (i, n))
            key = (n, itf)
            if key in ports and n not in special:
                errors.append("%s:%s is used by link %d and link %d" % (n, itf, ports[key], i))
            ports[key] = i
    if lab.get("running") and name and name != lab.get("name"):
        errors.append("the lab is running as '%s' - renaming it now would orphan the "
                      "running containers; stop it first" % lab.get("name"))
    return errors, warnings


def write_file(lab, rel, text, base_sha, create=False):
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise EditError("file too large (limit 1 MB)")
    full, rel = _resolve(lab, rel, must_exist=not create)
    current = ""
    if os.path.isfile(full):
        with open(full, encoding="utf-8", errors="replace") as fh:
            current = fh.read()
    elif not create:
        raise EditError("%s does not exist" % rel, 404)
    if os.path.isfile(full) and create:
        raise EditError("%s already exists" % rel, 409)
    if base_sha is not None and sha(current) != base_sha:
        raise EditError("%s changed on disk since you opened it - reload before saving"
                        % rel, 409)
    warnings = []
    if rel == os.path.basename(lab["path"]):
        errors, warnings = check_topology(text, lab)
        if errors:
            e = EditError("the topology was not saved:\n- " + "\n- ".join(errors))
            e.errors = errors
            raise e
    if text == current and os.path.isfile(full):
        return {"path": rel, "sha": sha(text), "unchanged": True, "warnings": warnings}
    os.makedirs(os.path.dirname(full), exist_ok=True)
    version = _snapshot(lab, rel, full, "before edit")
    _write_atomic(full, text)
    return {"path": rel, "sha": sha(text), "saved_version": version, "warnings": warnings}


def history(lab, rel):
    full, rel = _resolve(lab, rel)
    d = _hist_dir(lab, rel)
    out = []
    if os.path.isdir(d):
        for v in sorted((v for v in os.listdir(d) if not v.endswith(".why")), reverse=True):
            p = os.path.join(d, v)
            why = ""
            try:
                with open(p + ".why") as fh:
                    why = fh.read().strip()
            except OSError:
                pass
            out.append({"version": v, "size": os.path.getsize(p), "why": why,
                        "when": "%s-%s-%s %s:%s:%s" % (v[0:4], v[4:6], v[6:8], v[9:11],
                                                     v[11:13], v[13:15])})
    return out


def read_version(lab, rel, version):
    full, rel = _resolve(lab, rel)
    if not re.match(r"^[0-9]{8}-[0-9]{6}(-[0-9]+)?$", version or ""):
        raise EditError("bad version id")
    p = os.path.join(_hist_dir(lab, rel), version)
    if not os.path.isfile(p):
        raise EditError("no such version", 404)
    with open(p, encoding="utf-8", errors="replace") as fh:
        return fh.read()


# --------------------------------------------------------------------------
# builder round-trip
# --------------------------------------------------------------------------

def _unique_stamp(dirs):
    """A version id not used yet in any of dirs (two saves can share a second)."""
    base = time.strftime("%Y%m%d-%H%M%S")
    stamp, n = base, 1
    while any(os.path.exists(os.path.join(d, stamp)) for d in dirs):
        n += 1
        stamp = "%s-%d" % (base, n)
    return stamp


def snapshot_dir(target, reason):
    """Before the builder overwrites a lab, keep every current file."""
    hist = os.path.join(target, HISTORY)
    existing = [os.path.join(hist, d) for d in os.listdir(hist)] if os.path.isdir(hist) else []
    stamp = _unique_stamp(existing)
    kept = []
    for root, dirs, files in os.walk(target):
        rel_root = os.path.relpath(root, target)
        dirs[:] = [d for d in dirs if d != HISTORY and not d.startswith("clab-")]
        for fn in files:
            rel = os.path.normpath(os.path.join(rel_root, fn))
            if rel.endswith(".clabd-tmp"):
                continue
            d = os.path.join(target, HISTORY, rel.replace(os.sep, "__"))
            os.makedirs(d, exist_ok=True)
            shutil.copy2(os.path.join(root, fn), os.path.join(d, stamp))
            with open(os.path.join(d, stamp + ".why"), "w") as fh:
                fh.write(reason)
            _own_like(os.path.join(d, stamp + ".why"), target)
            _own_like(os.path.join(d, stamp), target)
            kept.append(rel)
    return stamp, kept


def _header_value(text, key):
    m = re.search(r"^#\s*%s:\s*(\S+)" % re.escape(key), text, re.M)
    return m.group(1) if m else None


def builder_spec(lab, kinds):
    """(spec, how, warnings) for reopening a lab in the builder."""
    import json
    base = os.path.dirname(lab["path"])
    saved = os.path.join(base, SPEC_FILE)
    if os.path.isfile(saved):
        with open(saved) as fh:
            spec = json.load(fh)
        # keep node positions in step with anything moved in the topology file
        return spec, "saved", []

    with open(lab["path"]) as fh:
        text = fh.read()
    doc = yaml.safe_load(text) or {}
    topo = doc.get("topology") or {}
    kcfg = topo.get("kinds") or {}
    defaults = topo.get("defaults") or {}
    warnings = []
    nodes, ids = [], {}
    auto = 0
    for i, (nname, ncfg) in enumerate((topo.get("nodes") or {}).items(), 1):
        ncfg = ncfg if isinstance(ncfg, dict) else {}
        kind = ncfg.get("kind") or defaults.get("kind") or "linux"
        image = ncfg.get("image") or (kcfg.get(kind) or {}).get("image") or defaults.get("image")
        # builder kinds that are written as another clab kind (frr -> linux)
        blabels = ncfg.get("labels") if isinstance(ncfg.get("labels"), dict) else {}
        if blabels.get("builder-kind") in kinds:
            kind = str(blabels["builder-kind"])
        if kind not in kinds:
            warnings.append("%s is %s - the builder has no template for that kind, "
                            "so it was left out" % (nname, kind))
            continue
        labels = ncfg.get("labels") if isinstance(ncfg.get("labels"), dict) else {}
        pos = str(labels.get("builder-pos") or "")
        try:
            x, y = (float(v) for v in pos.split(","))
        except ValueError:
            auto += 1
            x, y = 80 + (auto % 5) * 170, 80 + (auto // 5) * 130
        node = {"id": "n%d" % i, "name": str(nname).lower(), "kind": kind,
                "image": image or "", "x": x, "y": y}
        for lab_key, key in (("builder-role", "role"), ("builder-vrf", "vrf"),
                             ("graph-icon", "icon")):
            if labels.get(lab_key):
                node[key] = str(labels[lab_key])
        if labels.get("builder-asn"):
            try:
                node["asn"] = int(labels["builder-asn"])
            except ValueError:
                pass
        ids[str(nname)] = node["id"]
        nodes.append(node)
    links = []
    for link in topo.get("links") or []:
        eps = link.get("endpoints") if isinstance(link, dict) else None
        if not isinstance(eps, list) or len(eps) != 2:
            continue
        a, b = (str(e).split(":", 1)[0] for e in eps)
        if a in ids and b in ids:
            links.append({"a": ids[a], "b": ids[b]})
        else:
            warnings.append("link %s - %s left out (endpoint not in the builder)" % (a, b))

    igp = (_header_value(text, "IGP") or "isis").lower()
    link_line = re.search(r"^#\s*Links:\s*(\S+)\s+in\s+/(\d+)", text, re.M)
    mgmt = doc.get("mgmt") or {}
    spec = {
        "name": doc.get("name") or lab.get("name"),
        "igp": igp if igp in ("isis", "ospf", "none") else "isis",
        "loopback_subnet": _header_value(text, "Loopbacks") or "10.0.0.0/24",
        "link_subnet": link_line.group(1) if link_line else "10.10.0.0/16",
        "link_prefix": int(link_line.group(2)) if link_line else 30,
        "mgmt_subnet": mgmt.get("ipv4-subnet") or "172.20.20.0/24",
        "nodes": nodes, "links": links,
    }
    if any(n.get("role") for n in nodes):
        warnings.append("BGP roles and AS numbers were read from the node labels; check "
                        "the Services section (SR / BGP mode / L3VPN) before saving - "
                        "labs saved before this version did not record it")
    return spec, "reconstructed", warnings
