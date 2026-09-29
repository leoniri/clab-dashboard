#!/usr/bin/env python3
"""
Packet capture on lab links, viewed in the browser.

A capture runs tcpdump inside one node's network namespace, on one of its
data ports, writing a pcap under CAPTURE_DIR (packet-buffered, so the file is
readable while it grows). The browser polls for new packets; each poll cuts
just the new frames out with editcap and dissects only those with tshark, so
a long capture stays cheap. A display filter, or the detail of one packet,
dissects the whole file (a capture is capped at MAX_PACKETS).

The capture point is the container side of the port. On a vrnetlab router
that is ethN, which tc redirects to and from the VM's tapN - tcpdump there
sees both directions, exactly what goes over the wire. Native containers
(FRR, SR Linux, linux) are captured on the port itself.

The client names a lab id, a node and a port; the port must be one of that
node's links in the topology, and the container is resolved here. The BPF
and display filters go to tcpdump / tshark as single argv entries (never a
shell) and are restricted to a conservative character set anyway.
"""

import os
import re
import shutil
import subprocess
import threading
import time
import sysbin

NSENTER = sysbin.find("nsenter")
TCPDUMP = sysbin.find("tcpdump")
TSHARK = sysbin.find("tshark")
EDITCAP = sysbin.find("editcap")
DOCKER = sysbin.find("docker")
CAPTURE_DIR = "/var/lib/clab-dashboard/captures"
MAX_PACKETS = 20000
MAX_SECONDS = 3600
MAX_BYTES = 200 * 1024 * 1024
KEEP = 30                         # finished captures kept on disk
FILTER_RE = re.compile(r"^[A-Za-z0-9 _.:/()!&|=<>\[\]\-,\"]{0,300}$")
ID_RE = re.compile(r"^c[0-9]{1,6}-[0-9]{6}$")
FIELDS = ["frame.number", "frame.time_epoch", "_ws.col.def_src", "_ws.col.def_dst",
          "_ws.col.protocol", "frame.len", "_ws.col.info"]


class CaptureError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


def _pid(cname):
    try:
        out = subprocess.run([DOCKER, "inspect", "-f", "{{.State.Pid}}", cname],
                             capture_output=True, text=True, timeout=20).stdout.strip()
        return int(out) if out.isdigit() and out != "0" else 0
    except Exception:                                     # noqa: BLE001
        return 0


class Capture:
    def __init__(self, cid, lab, node, port, cname, bpf, max_packets, max_seconds, who):
        self.id = cid
        self.lab_id, self.lab = lab["id"], lab.get("name")
        self.node, self.port, self.cname = node, port, cname
        self.bpf = bpf
        self.max_packets, self.max_seconds = max_packets, max_seconds
        self.who = who
        self.path = os.path.join(CAPTURE_DIR, cid + ".pcap")
        self.started = time.time()
        self.stopped = None
        self.reason = None
        self.proc = None
        self.error = None

    def public(self):
        size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
        return {"id": self.id, "lab_id": self.lab_id, "lab": self.lab, "node": self.node,
                "port": self.port, "filter": self.bpf, "started": self.started,
                "stopped": self.stopped, "running": self.stopped is None, "bytes": size,
                "reason": self.reason, "error": self.error,
                "max_packets": self.max_packets, "max_seconds": self.max_seconds}


class Captures:
    def __init__(self, audit):
        self.audit = audit
        self.lock = threading.Lock()
        self.items = {}
        self.seq = 0
        os.makedirs(CAPTURE_DIR, exist_ok=True)
        # captures from before a restart: their tcpdump died with us
        for fn in sorted(os.listdir(CAPTURE_DIR)):
            m = re.match(r"^(c[0-9]{1,6}-[0-9]{6})\.pcap$", fn)
            if m:
                c = Capture(m.group(1), {"id": None, "name": "?"}, "?", "?", "?", "", 0, 0, "?")
                c.started = c.stopped = os.path.getmtime(os.path.join(CAPTURE_DIR, fn))
                c.reason = "from before the dashboard restarted"
                self.items[c.id] = c

    # -- lifecycle ----------------------------------------------------------
    def start(self, lab, node, port, bpf, max_packets, max_seconds, who):
        if not lab.get("running"):
            raise CaptureError("the lab is not running", 409)
        ports = set()
        for l in lab.get("links") or []:
            if l.get("a") == node:
                ports.add(l.get("a_if"))
            if l.get("b") == node:
                ports.add(l.get("b_if"))
        if port not in ports:
            raise CaptureError("%s has no link on %r" % (node, port))
        c = next((c for c in lab.get("containers") or [] if c.get("short") == node), None)
        if not c or c.get("state") != "running":
            raise CaptureError("%s is not running" % node, 409)
        bpf = (bpf or "").strip()
        if not FILTER_RE.match(bpf):
            raise CaptureError("the capture filter has characters tcpdump filters do not need")
        try:
            max_packets = max(1, min(MAX_PACKETS, int(max_packets or MAX_PACKETS)))
            max_seconds = max(5, min(MAX_SECONDS, int(max_seconds or 600)))
        except (TypeError, ValueError):
            raise CaptureError("limits must be numbers")
        pid = _pid(c["name"])
        if not pid:
            raise CaptureError("%s has no process" % node, 409)
        with self.lock:
            if sum(1 for x in self.items.values() if x.stopped is None) >= 6:
                raise CaptureError("six captures are already running - stop one first", 409)
            self.seq += 1
            cid = "c%d-%s" % (self.seq, time.strftime("%H%M%S"))
            cap = Capture(cid, lab, node, port, c["name"], bpf, max_packets, max_seconds, who)
            self.items[cid] = cap
        cmd = [NSENTER, "-t", str(pid), "-n", TCPDUMP, "-i", port, "-U", "-s", "0", "-Z", "root",
               "-n", "-c", str(max_packets), "-w", cap.path]
        if bpf:
            cmd.append(bpf)
        cap.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        # a bad filter makes tcpdump exit at once: report that instead of an empty capture
        time.sleep(0.4)
        if cap.proc.poll() is not None and cap.proc.returncode != 0:
            err = (cap.proc.stderr.read() or "").strip().splitlines()
            cap.stopped, cap.error = time.time(), (err[-1] if err else "tcpdump failed")
            raise CaptureError("tcpdump: %s" % cap.error)
        self.audit("CAPTURE-START id=%s lab=%s node=%s port=%s filter=%r from=%s"
                   % (cid, lab.get("name"), node, port, bpf, who))
        threading.Thread(target=self._watch, args=(cap,), daemon=True).start()
        self._prune()
        return cap.public()

    def _watch(self, cap):
        while cap.proc.poll() is None:
            if time.time() - cap.started > cap.max_seconds:
                cap.reason = "time limit (%d s)" % cap.max_seconds
                cap.proc.terminate()
            elif os.path.exists(cap.path) and os.path.getsize(cap.path) > MAX_BYTES:
                cap.reason = "size limit (%d MB)" % (MAX_BYTES // 1024 // 1024)
                cap.proc.terminate()
            time.sleep(1)
        if cap.reason is None:
            cap.reason = ("packet limit (%d)" % cap.max_packets if cap.proc.returncode == 0
                          else "stopped")
        cap.stopped = cap.stopped or time.time()

    def stop(self, cid, who):
        cap = self.get(cid)
        if cap.stopped is None and cap.proc:
            cap.reason = "stopped by %s" % who
            cap.proc.terminate()
            try:
                cap.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cap.proc.kill()
            cap.stopped = time.time()
            self.audit("CAPTURE-STOP id=%s from=%s" % (cid, who))
        return cap.public()

    def delete(self, cid, who):
        cap = self.get(cid)
        self.stop(cid, who)
        try:
            os.remove(cap.path)
        except OSError:
            pass
        with self.lock:
            self.items.pop(cid, None)
        return {"ok": True}

    def _prune(self):
        done = sorted((c for c in self.items.values() if c.stopped), key=lambda c: c.started)
        for c in done[:-KEEP] if len(done) > KEEP else []:
            try:
                os.remove(c.path)
            except OSError:
                pass
            self.items.pop(c.id, None)

    def get(self, cid):
        if not isinstance(cid, str) or not ID_RE.match(cid):
            raise CaptureError("bad capture id")
        cap = self.items.get(cid)
        if cap is None:
            raise CaptureError("no such capture", 404)
        return cap

    def list(self, lab_id=None):
        return [c.public() for c in sorted(self.items.values(), key=lambda c: -c.started)
                if lab_id in (None, c.lab_id)]

    # -- reading ------------------------------------------------------------
    @staticmethod
    def _tshark_rows(path, dfilter=None):
        cmd = [TSHARK, "-n", "-r", path, "-T", "fields", "-E", "separator=/t", "-E", "occurrence=f"]
        for f in FIELDS:
            cmd += ["-e", f]
        if dfilter:
            cmd += ["-Y", dfilter]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        rows = []
        for line in p.stdout.splitlines():
            f = line.split("\t")
            if len(f) < 7 or not f[0].isdigit():
                continue
            rows.append({"n": int(f[0]), "t": float(f[1] or 0), "src": f[2], "dst": f[3],
                         "proto": f[4], "len": int(f[5] or 0), "info": f[6]})
        err = [l for l in p.stderr.splitlines() if "Running as user" not in l
               and "cut short" not in l and l.strip()]
        return rows, err

    def packets(self, cid, since=0, limit=500, dfilter=None):
        cap = self.get(cid)
        body = cap.public()
        if not os.path.exists(cap.path) or os.path.getsize(cap.path) < 25:
            body.update({"packets": [], "total": 0})
            return body
        dfilter = (dfilter or "").strip()
        if dfilter:
            if not FILTER_RE.match(dfilter):
                raise CaptureError("the display filter has unexpected characters")
            rows, err = self._tshark_rows(cap.path, dfilter)
            if err and not rows:
                raise CaptureError("display filter: %s" % err[-1][:200])
            body.update({"packets": rows[-2000:], "total": len(rows), "filtered": True})
            return body
        since = max(0, int(since or 0))
        limit = max(1, min(2000, int(limit or 500)))
        tmp = os.path.join(CAPTURE_DIR, ".%s-%d.pcap" % (cid, threading.get_ident()))
        try:
            # only the new frames: editcap copies without dissecting
            subprocess.run([EDITCAP, "-r", cap.path, tmp, "%d-%d" % (since + 1, since + limit)],
                           capture_output=True, timeout=60)
            rows, _ = self._tshark_rows(tmp) if os.path.exists(tmp) else ([], [])
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        for i, r in enumerate(rows):
            r["n"] = since + i + 1
        body.update({"packets": rows, "total": since + len(rows)})
        return body

    def detail(self, cid, n):
        cap = self.get(cid)
        try:
            n = int(n)
        except (TypeError, ValueError):
            raise CaptureError("bad packet number")
        base = [TSHARK, "-n", "-r", cap.path, "-Y", "frame.number==%d" % n]
        tree = subprocess.run(base + ["-V"], capture_output=True, text=True, timeout=60).stdout
        hexd = subprocess.run(base + ["-x"], capture_output=True, text=True, timeout=60).stdout
        if not tree.strip():
            raise CaptureError("no packet %d" % n, 404)
        # -x prints the summary line first; keep only the hex dump
        hexd = "\n".join(l for l in hexd.splitlines() if re.match(r"^[0-9a-f]{4}  ", l))
        return {"n": n, "tree": tree, "hex": hexd}

    def pcap_path(self, cid):
        cap = self.get(cid)
        if not os.path.exists(cap.path):
            raise CaptureError("the capture file is gone", 404)
        name = "%s-%s-%s-%s.pcap" % (cap.lab, cap.node, cap.port, time.strftime(
            "%Y%m%d-%H%M%S", time.localtime(cap.started)))
        return cap.path, re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def have_tools():
    return all(shutil.which(x) or os.path.exists(x) for x in (TCPDUMP, TSHARK, EDITCAP))
