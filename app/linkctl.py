#!/usr/bin/env python3
"""
Link failures and impairments on a running lab.

A link is named by its two endpoints exactly as the topology file writes them
({a, a_if, b, b_if}); it must be one of the lab's own links, so the client can
never point this at an arbitrary interface.

Failing a link - two ways, because routers that run as VMs cannot see the
carrier of their container's veth:

  cut       both container-side interfaces are set down in their network
            namespace. Native containers (FRR, SR Linux, linux) lose carrier at
            once; a vrnetlab VM (IOS-XE, IOS-XR, NX-OS) keeps its NIC up and
            notices only when its hellos time out (IS-IS ~30 s, OSPF ~40 s, BFD
            much faster if configured) - a "silent" failure, like a broken
            transport circuit.
  shutdown  each end is shut the way that node would be: `shutdown` on the
            interface of a VM router (pushed over SSH, not saved), admin-state
            disable on SR Linux, link down in the kernel for native linux/FRR.
            Both ends see the interface go down immediately.

Either can restore itself after a given number of seconds; pending restores are
kept in STATE_FILE so a dashboard restart does not leave a link down forever.
The tc redirects vrnetlab uses between ethN and the VM's tapN survive a
down/up (verified on c8000v 17.12), so restoring needs nothing else.

Impairments are netem on the egress of one container interface, set with
`clab tools netem`: impairing side A delays/drops what A sends to B. "both"
does both ends. On a vrnetlab router the redirect from the VM to ethN goes
through ethN's root qdisc, so netem applies to VMs too.
"""

import json
import os
import re
import subprocess
import threading
import time
import sysbin

CLAB = sysbin.find("clab", "containerlab")
DOCKER = sysbin.find("docker")
NSENTER = sysbin.find("nsenter")
IP = sysbin.find("ip")
TC = sysbin.find("tc")
STATE_FILE = "/var/lib/clab-dashboard/link-failures.json"
MAX_DURATION = 24 * 3600

VM_KINDS = {"cisco_c8000v": "xe", "cisco_csr1000v": "xe", "cisco_xrd_vrouter": "xr",
            "cisco_n9kv": "nxos"}
PORT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,30}$")

# netem knobs: name -> (clab flag, unit, max)
IMPAIR = {
    "delay_ms":    ("--delay", "ms", 60000),
    "jitter_ms":   ("--jitter", "ms", 60000),
    "loss_pct":    ("--loss", "", 100),
    "rate_kbit":   ("--rate", "", 100_000_000),
    "corrupt_pct": ("--corruption", "", 100),
}


class LinkError(Exception):
    pass


def _run(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as exc:                              # noqa: BLE001
        return 1, "", str(exc)


def _pid(cname):
    rc, out, _ = _run([DOCKER, "inspect", "-f", "{{.State.Pid}}", cname], timeout=20)
    return int(out.strip()) if rc == 0 and out.strip().isdigit() and out.strip() != "0" else 0


def link_key(l):
    a = "%s:%s" % (l["a"], l["a_if"])
    b = "%s:%s" % (l["b"], l["b_if"])
    return "|".join(sorted((a, b)))


class Links:
    def __init__(self, audit):
        self.audit = audit
        self.lock = threading.Lock()
        self.state = {}             # "<lab>|<link key>" -> failure record
        self.timers = {}
        self._load()

    # -- persistence --------------------------------------------------------
    def _load(self):
        try:
            with open(STATE_FILE) as fh:
                self.state = json.load(fh) or {}
        except (OSError, ValueError):
            self.state = {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(self.state, fh, indent=1)
            os.replace(tmp, STATE_FILE)
        except OSError:
            pass

    def resume(self, lab_index_fn):
        """After a restart: re-arm pending restores, restore overdue ones now."""
        for k, rec in list(self.state.items()):
            if not rec.get("restore_at"):
                continue
            delay = max(1.0, rec["restore_at"] - time.time())
            self._arm(k, delay, lab_index_fn)

    # -- lookup -------------------------------------------------------------
    @staticmethod
    def find_link(lab, l):
        if not isinstance(l, dict):
            raise LinkError("no link given")
        for k in ("a", "a_if", "b", "b_if"):
            if not isinstance(l.get(k), str) or not PORT_RE.match(l[k]):
                raise LinkError("bad link endpoint %r" % l.get(k))
        want = link_key(l)
        for x in lab.get("links") or []:
            if link_key(x) == want:
                if not (x.get("a_known") and x.get("b_known")):
                    raise LinkError("one end of this link is outside the lab")
                return x
        raise LinkError("%s is not a link of %s" % (want.replace("|", " - "), lab.get("name")))

    @staticmethod
    def _end(lab, node, port):
        """(container name, kind, image) for one running link end."""
        c = next((c for c in lab.get("containers") or [] if c.get("short") == node), None)
        if not c or c.get("state") != "running":
            raise LinkError("%s is not running" % node)
        nent = next((n for n in lab.get("nodes") or [] if n.get("name") == node), {})
        return c["name"], nent.get("kind"), nent.get("image")

    # -- kernel state -------------------------------------------------------
    @staticmethod
    def iface_state(cname):
        """{ifname: {"up": admin up, "oper": operstate, "netem": {...}|None}}"""
        pid = _pid(cname)
        if not pid:
            return {}
        rc, out, _ = _run([NSENTER, "-t", str(pid), "-n", "sh", "-c",
                           "%s -j link show; echo '@@@'; %s -j qdisc show" % (IP, TC)], timeout=20)
        if rc != 0 or "@@@" not in out:
            return {}
        links_js, qd_js = out.split("@@@", 1)
        res = {}
        try:
            for i in json.loads(links_js or "[]"):
                res[i.get("ifname")] = {"up": "UP" in (i.get("flags") or []),
                                        "oper": i.get("operstate"), "netem": None}
            for q in json.loads(qd_js or "[]"):
                if q.get("kind") != "netem" or q.get("dev") not in res:
                    continue
                o = q.get("options") or {}
                ne = {}
                d = o.get("delay") or {}
                if d.get("delay"):
                    ne["delay_ms"] = round(d["delay"] * 1000, 3)
                if d.get("jitter"):
                    ne["jitter_ms"] = round(d["jitter"] * 1000, 3)
                if (o.get("loss-random") or {}).get("loss"):
                    ne["loss_pct"] = round(o["loss-random"]["loss"] * 100, 3)
                if (o.get("corrupt") or {}).get("corrupt"):
                    ne["corrupt_pct"] = round(o["corrupt"]["corrupt"] * 100, 3)
                if (o.get("rate") or {}).get("rate"):
                    ne["rate_kbit"] = int(o["rate"]["rate"] * 8 / 1000)
                res[q["dev"]]["netem"] = ne or None
        except (ValueError, TypeError, AttributeError):
            return {}
        return res

    def status(self, lab):
        """Per link: kernel state of both ends, netem, and any failure we made."""
        by_c = {}
        for c in lab.get("containers") or []:
            if c.get("state") == "running":
                by_c[c["short"]] = self.iface_state(c["name"])
        out = {}
        name = lab.get("name")
        for l in lab.get("links") or []:
            if not (l.get("a_known") and l.get("b_known")):
                continue
            ent = {}
            for side in ("a", "b"):
                st = (by_c.get(l[side]) or {}).get(l[side + "_if"])
                ent[side] = st
            rec = self.state.get("%s|%s" % (name, link_key(l)))
            ent["failed"] = rec
            ent["down"] = any(ent[s] and (not ent[s]["up"] or ent[s]["oper"] == "DOWN")
                              for s in ("a", "b"))
            ent["impaired"] = any(ent[s] and ent[s]["netem"] for s in ("a", "b"))
            out[l["id"]] = ent
        # forget failures of a lab that is no longer running: a redeploy
        # brings every link back up anyway
        if not lab.get("running"):
            with self.lock:
                gone = [k for k in self.state if k.startswith(name + "|")]
                for k in gone:
                    self.state.pop(k, None)
                    t = self.timers.pop(k, None)
                    if t:
                        t.cancel()
                if gone:
                    self._save()
        return out

    def status_records(self, lab):
        """{link key: failure record} for this lab."""
        pre = "%s|" % lab.get("name")
        with self.lock:
            return {k[len(pre):]: v for k, v in self.state.items() if k.startswith(pre)}

    # -- failing and restoring ---------------------------------------------
    def _ends(self, lab, l):
        return [(l[s], l[s + "_if"]) + self._end(lab, l[s], l[s + "_if"]) for s in ("a", "b")]

    def _kernel(self, cname, port, up):
        pid = _pid(cname)
        if not pid:
            raise LinkError("%s is not running" % cname)
        rc, _, err = _run([NSENTER, "-t", str(pid), "-n", IP, "link", "set", port,
                           "up" if up else "down"], timeout=20)
        if rc != 0:
            raise LinkError("%s %s: %s" % (cname, port, err.strip()[:200]))

    def _device(self, lab_index_fn, lab, node, kind, port, up, log):
        """Admin up/down on the device itself."""
        import devcfg
        import topoedit
        m = topoedit.PORT_RE.match(port)
        num = int(m.group(1)) if m else 0
        name = topoedit.iface_name(kind, num)
        if kind == "nokia_srlinux":
            cfg = "set / interface %s admin-state %s" % (name, "enable" if up else "disable")
        else:
            cfg = "interface %s\n %s" % (name, "no shutdown" if up else "shutdown")
        r = devcfg.push_config(lab_index_fn(), lab["id"], node, cfg, save_startup_after=False)
        if not r.get("ok"):
            raise LinkError("%s: %s" % (node, "; ".join(r.get("errors") or ["push failed"])))
        log("%s: %s %s" % (node, name, "no shutdown" if up else "shutdown"))

    def _apply(self, lab_index_fn, lab, l, mode, up, log):
        for node, port, cname, kind, image in self._ends(lab, l):
            if mode == "shutdown" and (kind in VM_KINDS or kind == "nokia_srlinux"):
                self._device(lab_index_fn, lab, node, kind, port, up, log)
            else:
                self._kernel(cname, port, up)
                log("%s: %s %s" % (node, port, "up" if up else "down"))

    def fail(self, lab_index_fn, lab, link, mode, duration, who):
        l = self.find_link(lab, link)
        if mode not in ("cut", "shutdown"):
            raise LinkError("mode must be cut or shutdown")
        try:
            duration = int(duration or 0)
        except (TypeError, ValueError):
            raise LinkError("duration must be a number of seconds")
        if duration < 0 or duration > MAX_DURATION:
            raise LinkError("duration must be 0 (until restored) to %d seconds" % MAX_DURATION)
        key = "%s|%s" % (lab.get("name"), link_key(l))
        if key in self.state:
            raise LinkError("this link is already failed - restore it first")
        lines = []
        self.audit("LINK-FAIL lab=%s link=%s mode=%s duration=%s from=%s"
                   % (lab.get("name"), link_key(l), mode, duration or "-", who))
        self._apply(lab_index_fn, lab, l, mode, False, lines.append)
        rec = {"mode": mode, "since": time.time(), "by": who,
               "restore_at": (time.time() + duration) if duration else None,
               "link": {k: l[k] for k in ("a", "a_if", "b", "b_if")}}
        with self.lock:
            self.state[key] = rec
            self._save()
        if duration:
            self._arm(key, duration, lab_index_fn)
        return {"ok": True, "log": lines, "failure": rec}

    def restore(self, lab_index_fn, lab, link, who):
        l = self.find_link(lab, link)
        key = "%s|%s" % (lab.get("name"), link_key(l))
        rec = self.state.get(key) or {"mode": "cut"}
        lines = []
        self.audit("LINK-RESTORE lab=%s link=%s mode=%s from=%s"
                   % (lab.get("name"), link_key(l), rec["mode"], who))
        # bring the kernel side up in any case: a cut made outside the dashboard
        # (or a failed shutdown push) is repaired too
        self._apply(lab_index_fn, lab, l, rec["mode"], True, lines.append)
        if rec["mode"] == "shutdown":
            for node, port, cname, kind, image in self._ends(lab, l):
                try:
                    self._kernel(cname, port, True)
                except LinkError:
                    pass
        with self.lock:
            self.state.pop(key, None)
            t = self.timers.pop(key, None)
            if t:
                t.cancel()
            self._save()
        return {"ok": True, "log": lines}

    def _arm(self, key, delay, lab_index_fn):
        def fire():
            rec = self.state.get(key)
            if not rec:
                return
            lab_name = key.split("|", 1)[0]
            lab = next((x for x in lab_index_fn().values() if x.get("name") == lab_name
                        and x.get("running")), None)
            try:
                if lab is None:
                    raise LinkError("lab %s is not running" % lab_name)
                self.restore(lab_index_fn, lab, rec["link"], "timer")
            except Exception as exc:                  # noqa: BLE001
                self.audit("LINK-RESTORE lab=%s link=%s by timer FAILED: %s"
                           % (lab_name, key.split("|", 1)[1], exc))
                with self.lock:
                    self.state.pop(key, None)
                    self.timers.pop(key, None)
                    self._save()
        t = threading.Timer(delay, fire)
        t.daemon = True
        with self.lock:
            old = self.timers.pop(key, None)
            if old:
                old.cancel()
            self.timers[key] = t
        t.start()

    # -- impairments --------------------------------------------------------
    def impair(self, lab, link, direction, params, who):
        l = self.find_link(lab, link)
        sides = {"both": ("a", "b"), "a": ("a",), "b": ("b",)}.get(direction)
        if not sides:
            raise LinkError("direction must be both, a or b")
        args = []
        clean = {}
        for k, (flag, unit, mx) in IMPAIR.items():
            v = (params or {}).get(k)
            if v in (None, "", 0, "0"):
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise LinkError("%s must be a number" % k)
            if v < 0 or v > mx:
                raise LinkError("%s must be between 0 and %s" % (k, mx))
            clean[k] = v
            val = ("%d" % v) if k == "rate_kbit" else ("%g" % v)
            args += [flag, val + unit]
        if "jitter_ms" in clean and "delay_ms" not in clean:
            raise LinkError("jitter needs a delay")
        self.audit("LINK-IMPAIR lab=%s link=%s dir=%s %s from=%s"
                   % (lab.get("name"), link_key(l), direction,
                      " ".join("%s=%g" % kv for kv in sorted(clean.items())) or "clear", who))
        out = []
        for s in sides:
            cname, _, _ = self._end(lab, l[s], l[s + "_if"])
            if clean:
                cmd = [CLAB, "tools", "netem", "set", "-n", cname, "-i", l[s + "_if"]] + args
            else:
                cmd = [CLAB, "tools", "netem", "reset", "-n", cname, "-i", l[s + "_if"]]
            rc, so, se = _run(cmd, timeout=60)
            if rc != 0:
                raise LinkError("%s %s: %s" % (l[s], l[s + "_if"],
                                                (se or so).strip().splitlines()[-1:] or "netem failed"))
            out.append("%s %s: %s" % (l[s], l[s + "_if"], ", ".join(
                "%s %g" % (k, v) for k, v in sorted(clean.items())) or "impairments cleared"))
        return {"ok": True, "log": out}
