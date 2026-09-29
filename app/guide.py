"""
Lab guides in the topology view: the lab's goal, the role of each node and
checks that prove the lab does what it is for (scenario_guides.py writes them
as lab-guide.json when a catalogue scenario is created).

GET  /api/guide?lab=<id>            the guide (or just the README when there is none)
POST /api/guide/run {lab_id, check} run one check, return output and pass / fail

The browser only names a check. The command comes from the lab's own guide
file, and even that is held to read-only verbs (show / ping / traceroute) and
a conservative character set before it goes anywhere near a node.
"""

import json
import os
import re
import subprocess
import time

import devcfg

GUIDE_FILE = "lab-guide.json"
README_FILE = "README.md"
MAX_OUT = 20000

SAFE_CLI = re.compile(r"^(show|ping|traceroute) [A-Za-z0-9 _.:/\[\]-]+$")
ADDR = re.compile(r"^[0-9A-Fa-f:.]{2,45}$")
IFNAME = re.compile(r"^[a-z][a-z0-9.-]{0,14}$")
REPLY = re.compile(r"bytes from ([0-9A-Fa-f:.]*[0-9A-Fa-f.])")      # busybox adds a ":" after the address


class GuideError(Exception):
    def __init__(self, msg, code=400):
        Exception.__init__(self, msg)
        self.code = code


def _read(path, limit=512 * 1024):
    try:
        if os.path.getsize(path) > limit:
            return None
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def load(lab):
    """{"guide": dict or None, "readme": text or None} for a lab."""
    d = lab.get("dir") or ""
    guide = None
    raw = _read(os.path.join(d, GUIDE_FILE))
    if raw:
        try:
            guide = json.loads(raw)
            if not isinstance(guide, dict) or not isinstance(guide.get("checks"), list):
                guide = None
        except ValueError:
            guide = None
    return {"guide": guide, "readme": _read(os.path.join(d, README_FILE), 256 * 1024)}


def platform(node):
    p = devcfg.platform_of(node.get("kind"), node.get("image"))
    if p is None and node.get("kind") == "linux":
        return "linux"
    return p


def _ping_argv(plat, src, dst):
    base = ["ping", "-c", "3", "-W", "2", "-t", "16", "-I", src, dst]
    if plat == "srl":
        # SR Linux: the default network-instance lives in its own namespace
        return ["ip", "netns", "exec", "srbase-default"] + base
    return base


def run(lab, poller, check_id):
    g = load(lab)["guide"]
    if not g:
        raise GuideError("this lab has no guide", 404)
    chk = next((c for c in g["checks"] if c.get("id") == check_id), None)
    if chk is None:
        raise GuideError("no such check", 404)
    if not lab.get("running"):
        raise GuideError("the lab is not running - start it first", 409)
    name = chk.get("node")
    node = next((n for n in lab.get("nodes") or [] if n.get("name") == name), None)
    cont = next((c for c in lab.get("containers") or [] if c.get("short") == name), None)
    if node is None:
        raise GuideError("%s is not a node of this lab any more" % name, 409)
    if not cont or cont.get("state") != "running":
        raise GuideError("%s is not running" % name, 409)
    plat = platform(node)
    t0 = time.time()
    res = {"id": check_id, "node": name, "when": t0}
    if chk.get("kind") == "ping":
        src, dst = str(chk.get("src") or ""), str(chk.get("dst") or "")
        if not ADDR.match(dst) or not (ADDR.match(src) or IFNAME.match(src)):
            raise GuideError("the check's addresses are not valid")
        if plat not in ("frr", "linux", "srl"):
            raise GuideError("pings are only run from FRR, SR Linux or linux nodes")
        try:
            p = subprocess.run([devcfg.DOCKER, "exec", cont["name"]] + _ping_argv(plat, src, dst),
                               capture_output=True, text=True, timeout=30)
            out = (p.stdout + p.stderr)
        except subprocess.TimeoutExpired:
            out = "ping timed out"
        got = sorted(set(REPLY.findall(out)))
        want = int(chk.get("responders") or 1)
        res.update(output=out[:MAX_OUT], ok=len(got) >= want, count=len(got),
                   summary="%d of %d expected responder%s answered%s" % (
                       len(got), want, "" if want == 1 else "s", (": " + ", ".join(got)) if got else ""))
    else:
        cmd = str(chk.get("cmd") or "")
        if not SAFE_CLI.match(cmd):
            raise GuideError("the check's command is not a plain show command")
        if plat is None:
            raise GuideError("%s is %s - no CLI access for this kind" % (name, node.get("kind")))
        try:
            out = poller.command(lab, name, cont, plat, cmd, timeout=60) or ""
        except Exception as exc:                            # noqa: BLE001
            raise GuideError("%s: %s" % (name, str(exc).splitlines()[0][:300] if str(exc) else exc), 502)
        exp = chk.get("expect")
        if exp:
            try:
                n = len(re.findall(exp, out, re.M))
            except re.error:
                n = 0
            want = int(chk.get("min") or 1)
            res.update(ok=n >= want, count=n,
                       summary=("found %d time%s" % (n, "" if n == 1 else "s")) + ("" if n >= want else
                                                                                 " - expected at least %d" % want))
        else:
            res.update(ok=None, summary="read the output")
        res["output"] = out[:MAX_OUT]
    res["took"] = round(time.time() - t0, 1)
    return res
