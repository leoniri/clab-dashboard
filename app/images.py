#!/usr/bin/env python3
"""
Container image and docker network management for the dashboard.

Read-only helpers here; anything that changes the host (pull, load, tag,
remove) is run by the caller as a job so its output streams to the UI and
lands in the audit log. Every argument that reaches docker is validated here
first - the browser never gets to hand docker a flag.
"""

import json
import re
import subprocess
import sysbin

DOCKER = sysbin.find("docker")

# name[:tag][@digest], registry host allowed. No leading '-', so it can never
# be read as an option.
REF_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,200}(:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?"
                    r"(@sha256:[a-f0-9]{64})?$")
ID_RE = re.compile(r"^(sha256:)?[a-f0-9]{12,64}$")
NET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SEARCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,80}$")

# Public images that containerlab can run out of the box. Vendor router
# images (IOS-XE, IOS-XR, NX-OS, cEOS, vJunos...) are not publicly pullable;
# they come from a vendor download and are built with vrnetlab or loaded
# from a tarball.
CATALOGUE = [
    {"ref": "ghcr.io/nokia/srlinux:latest", "kind": "nokia_srlinux", "type": "router",
     "about": "Nokia SR Linux - full NOS, free, no licence"},
    {"ref": "quay.io/frrouting/frr:10.4.1", "kind": "linux", "type": "router",
     "about": "FRRouting - IS-IS, OSPF, BGP, SR-MPLS, SRv6 on Linux"},
    {"ref": "ghcr.io/srl-labs/network-multitool:latest", "kind": "linux", "type": "server",
     "about": "host with iperf3, tcpdump, curl, nmap, ssh - a good lab client"},
    {"ref": "nicolaka/netshoot:latest", "kind": "linux", "type": "server",
     "about": "network troubleshooting swiss-army knife"},
    {"ref": "alpine:3.20", "kind": "linux", "type": "server",
     "about": "tiny Linux host"},
    {"ref": "ubuntu:24.04", "kind": "linux", "type": "server",
     "about": "general-purpose Linux host"},
    {"ref": "debian:12-slim", "kind": "linux", "type": "server",
     "about": "general-purpose Linux host"},
]


def _run(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as exc:                              # noqa: BLE001
        return 1, "", str(exc)


def _json_lines(out):
    res = []
    for line in out.splitlines():
        line = line.strip()
        if line:
            try:
                res.append(json.loads(line))
            except ValueError:
                pass
    return res


def list_images(labs=None, hints=None):
    """Images with usage: containers using them and labs that reference them."""
    rc, out, err = _run([DOCKER, "image", "ls", "--no-trunc", "--format", "{{json .}}"])
    if rc != 0:
        return {"images": [], "error": err.strip()[:300]}
    rc, cout, _ = _run([DOCKER, "ps", "-a", "--no-trunc",
                        "--format", "{{json .}}"])
    containers = _json_lines(cout) if rc == 0 else []

    # which labs reference which image (running or not)
    lab_refs = {}
    for lab in labs or []:
        for n in lab.get("nodes") or []:
            lab_refs.setdefault(n.get("image"), set()).add(lab.get("name") or "?")
    hints = hints or {}

    images = []
    for im in _json_lines(out):
        repo, tag = im.get("Repository"), im.get("Tag")
        ref = "%s:%s" % (repo, tag) if repo != "<none>" else None
        full_id = im.get("ID", "")
        short = full_id.replace("sha256:", "")[:12]
        users = [c for c in containers
                 if c.get("Image") in (ref, short, full_id)
                 or (ref and tag == "latest" and c.get("Image") == repo)]
        h = hints.get(ref) or {}
        images.append({
            "ref": ref, "repo": repo, "tag": tag, "id": short,
            "size": im.get("Size"), "created": im.get("CreatedAt", "")[:19],
            "created_since": im.get("CreatedSince"),
            "containers": len(users),
            "running": sum(1 for c in users if c.get("State") == "running"),
            "container_names": sorted(c.get("Names", "") for c in users)[:20],
            "labs": sorted(lab_refs.get(ref, ())) if ref else [],
            "kind": h.get("kind"), "os": h.get("os"), "note": h.get("note"),
            "configurable": h.get("configurable"),
            "dangling": ref is None,
        })
    images.sort(key=lambda i: (i["dangling"], (i["repo"] or "").lower(), i["tag"] or ""))
    return {"images": images, "catalogue": CATALOGUE}


def disk_usage():
    rc, out, _ = _run([DOCKER, "system", "df", "--format", "{{json .}}"])
    return _json_lines(out) if rc == 0 else []


def search_hub(term):
    if not SEARCH_RE.match(term or ""):
        raise ValueError("search term may contain letters, digits and . _ / - only")
    rc, out, err = _run([DOCKER, "search", "--limit", "25", "--no-trunc",
                         "--format", "{{json .}}", term], timeout=30)
    if rc != 0:
        raise ValueError(err.strip()[:300] or "docker search failed")
    return [{"name": r.get("Name"), "about": r.get("Description"),
             "stars": r.get("StarCount"), "official": r.get("IsOfficial") in ("[OK]", True, "true")}
            for r in _json_lines(out)]


def check_ref(ref):
    ref = (ref or "").strip()
    if not REF_RE.match(ref):
        raise ValueError("not a valid image reference: %r" % ref)
    return ref


def check_image_id(x):
    x = (x or "").strip()
    if ID_RE.match(x):
        return x
    return check_ref(x)


# --------------------------------------------------------------------------
# networks
# --------------------------------------------------------------------------

BUILTIN_NETS = {"bridge", "host", "none"}


def list_networks(labs=None):
    rc, out, err = _run([DOCKER, "network", "ls", "--no-trunc", "--format", "{{json .}}"])
    if rc != 0:
        return {"networks": [], "error": err.strip()[:300]}
    nets = _json_lines(out)
    ids = [n["ID"] for n in nets]
    detail = {}
    if ids:
        rc, dout, _ = _run([DOCKER, "network", "inspect"] + ids)
        try:
            for d in json.loads(dout or "[]"):
                detail[d["Id"]] = d
        except ValueError:
            pass
    # mgmt network name -> labs that declare it
    decl = {}
    for lab in labs or []:
        net = lab.get("mgmt_network") or "clab"
        decl.setdefault(net, set()).add(lab.get("name") or "?")

    res = []
    for n in nets:
        d = detail.get(n["ID"], {})
        cfg = ((d.get("IPAM") or {}).get("Config") or [{}])
        ctrs = d.get("Containers") or {}
        opts = d.get("Options") or {}
        res.append({
            "name": n.get("Name"), "id": n.get("ID", "")[:12], "driver": n.get("Driver"),
            "subnet": ", ".join(c.get("Subnet", "") for c in cfg if c.get("Subnet")),
            "gateway": ", ".join(c.get("Gateway", "") for c in cfg if c.get("Gateway")),
            "bridge": opts.get("com.docker.network.bridge.name")
                      or ("br-" + n.get("ID", "")[:12] if n.get("Driver") == "bridge"
                          and n.get("Name") != "bridge" else None),
            "containers": sorted(c.get("Name", "") for c in ctrs.values()),
            "labs": sorted(decl.get(n.get("Name"), ())),
            "builtin": n.get("Name") in BUILTIN_NETS,
            "created": (d.get("Created") or "")[:19],
        })
    res.sort(key=lambda x: (x["builtin"], x["name"]))
    return {"networks": res}


def check_net(name):
    name = (name or "").strip()
    if not NET_RE.match(name) or name in BUILTIN_NETS:
        raise ValueError("not a removable network: %r" % name)
    return name
