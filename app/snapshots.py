#!/usr/bin/env python3
"""
Lab snapshots: the configuration of every router at one moment, by name, and
a way back to it.

  <lab dir>/.clabd-snapshots/<lab name>/<id>/
      meta.json          name, note, when, per-node platform / result
      <node>.raw         the running config exactly as the box printed it
                         (SR Linux: full JSON) - what a live restore replaces with
      <node>.cfg         the same, cleaned into startup-config form (devcfg's
                         cleaning) - what a redeploy restore boots from

The directory is a dot-directory, so lab discovery and the file editor never
see it; it is owned like the lab directory.

Restore, per node, one of:

  live      replace the running config in place, no reboot:
              IOS-XE   SCP to bootflash:, `configure replace ... force`
              IOS-XR   SCP to harddisk:/, `load` + `commit replace`
              NX-OS    SCP to bootflash:, `configure replace`
              FRR      frr-reload.py --reload (computes and applies the diff)
              SR Linux `load file` of the JSON into a candidate, commit
            Only a node that is running now, in the same lab, can be restored
            live; the raw config carries the management addressing it had.
  redeploy  write every node's cleaned config into its startup file (the
            previous startup files stay in the lab's edit history), then
            `clab deploy --reconfigure`. Slow, but works for anything and
            for a lab that is not running.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time

import devcfg
import editor
import sysbin

SNAP_DIR = ".clabd-snapshots"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.:-]{0,59}$")
ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}(-[0-9]+)?$")
TRASH_DIR = "/var/lib/clab-dashboard/trash"
DOCKER = sysbin.find("docker")


class SnapError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


def _root(lab):
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", lab.get("name") or "lab")
    return os.path.join(os.path.dirname(lab["path"]), SNAP_DIR, name)


def _own(path, lab):
    st = os.stat(os.path.dirname(lab["path"]))
    for dirpath, dirs, files in os.walk(path):
        os.chown(dirpath, st.st_uid, st.st_gid)
        for f in files:
            os.chown(os.path.join(dirpath, f), st.st_uid, st.st_gid)


def _dir(lab, sid):
    if not isinstance(sid, str) or not ID_RE.match(sid):
        raise SnapError("bad snapshot id")
    d = os.path.join(_root(lab), sid)
    if not os.path.isfile(os.path.join(d, "meta.json")):
        raise SnapError("no such snapshot", 404)
    return d


def configurable_nodes(lab):
    """[(node, platform, running container)] for every node we can snapshot."""
    out = []
    for n in lab.get("nodes") or []:
        plat = devcfg.platform_of(n.get("kind"), n.get("image"))
        if not plat:
            continue
        c = next((c for c in lab.get("containers") or [] if c.get("short") == n["name"]), None)
        out.append((n["name"], plat, c if c and c.get("state") == "running" else None))
    return out


def _raw(lab, node, c, plat):
    """(raw running config, cleaned startup form)."""
    conn = devcfg._connect(lab, node, c, plat, timeout=120)
    try:
        if plat == "srl":
            rc, raw = conn.exec(["sr_cli", "-d", "info from running / | as json"], timeout=90)
            if rc != 0 or not raw.lstrip().startswith("{"):
                raise SnapError("%s: could not read the configuration: %s" % (node, raw[:200]))
            flat = conn.send_command("info flat", read_timeout=90)
            return raw, devcfg.clean_srl(flat)
        raw = devcfg._fetch_running(conn)
    finally:
        conn.disconnect()
    return raw, devcfg._clean(plat, raw, lab, node)


# --------------------------------------------------------------------------
# create / list / delete
# --------------------------------------------------------------------------

def create(lab, name, note, log):
    if not NAME_RE.match(name or ""):
        raise SnapError("give the snapshot a name (letters, digits, space . _ : -, up to 60)")
    nodes = configurable_nodes(lab)
    if not any(c for _, _, c in nodes):
        raise SnapError("no running router in this lab to take a snapshot of", 409)
    sid = time.strftime("%Y%m%d-%H%M%S")
    root = _root(lab)
    d = os.path.join(root, sid)
    i = 1
    while os.path.exists(d):
        i += 1
        d = os.path.join(root, "%s-%d" % (sid, i))
    sid = os.path.basename(d)
    os.makedirs(d)
    meta = {"id": sid, "name": name, "note": (note or "")[:500], "created": time.time(),
            "lab": lab.get("name"), "nodes": {}}
    lock = threading.Lock()

    def one(node, plat, c):
        t0 = time.time()
        try:
            raw, clean = _raw(lab, node, c, plat)
            with open(os.path.join(d, node + ".raw"), "w") as fh:
                fh.write(raw)
            with open(os.path.join(d, node + ".cfg"), "w") as fh:
                fh.write(clean)
            res = {"platform": devcfg.PLATFORM_NAMES[plat], "ok": True, "lines": len(clean.splitlines()),
                   "seconds": round(time.time() - t0, 1)}
            log("%s: %d lines (%s)" % (node, res["lines"], res["platform"]))
        except Exception as exc:                        # noqa: BLE001
            res = {"platform": devcfg.PLATFORM_NAMES.get(plat, plat), "ok": False, "error": str(exc)[:300]}
            log("%s: FAILED - %s" % (node, res["error"]))
        with lock:
            meta["nodes"][node] = res

    threads = []
    for node, plat, c in nodes:
        if c is None:
            meta["nodes"][node] = {"platform": devcfg.PLATFORM_NAMES[plat], "ok": False,
                                   "error": "not running"}
            log("%s: skipped - not running" % node)
            continue
        t = threading.Thread(target=one, args=(node, plat, c), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=300)
    ok = sum(1 for v in meta["nodes"].values() if v.get("ok"))
    if not ok:
        shutil.rmtree(d, ignore_errors=True)
        raise SnapError("no node could be read - nothing saved")
    with open(os.path.join(d, "meta.json"), "w") as fh:
        json.dump(meta, fh, indent=1)
    _own(os.path.dirname(root), lab)
    log("snapshot %r saved: %d of %d routers" % (name, ok, len(meta["nodes"])))
    return meta


def list_(lab):
    root = _root(lab)
    out = []
    try:
        ids = sorted(os.listdir(root), reverse=True)
    except OSError:
        return out
    for sid in ids:
        try:
            with open(os.path.join(root, sid, "meta.json")) as fh:
                out.append(json.load(fh))
        except (OSError, ValueError):
            continue
    return out


def delete(lab, sid):
    d = _dir(lab, sid)
    os.makedirs(TRASH_DIR, exist_ok=True)
    dest = os.path.join(TRASH_DIR, "%s-snapshot-%s-%s" % (time.strftime("%Y%m%d-%H%M%S"),
                                                        re.sub(r"[^A-Za-z0-9_.-]", "_", lab.get("name") or ""), sid))
    shutil.move(d, dest)
    return {"ok": True, "moved_to": dest}


def read_node(lab, sid, node):
    d = _dir(lab, sid)
    if not devcfg.NODE_RE.match(node or ""):
        raise SnapError("bad node name")
    p = os.path.join(d, node + ".cfg")
    if not os.path.isfile(p):
        raise SnapError("%s is not in this snapshot" % node, 404)
    with open(p) as fh:
        return fh.read()


def diff(lab_index, lab, sid, node):
    """Unified diff: snapshot -> the node's config now (both in cleaned form)."""
    import difflib
    old = read_node(lab, sid, node)
    now = devcfg.running_config(lab_index, lab["id"], node)["text"]
    d = list(difflib.unified_diff(old.splitlines(), now.splitlines(), "snapshot", "running now",
                                  lineterm="", n=2))
    return {"node": node, "diff": "\n".join(d), "same": not d}


# --------------------------------------------------------------------------
# restore
# --------------------------------------------------------------------------

def _ios_body(raw):
    return "\n".join(l for l in raw.replace("\r", "").splitlines()
                     if not re.match(r"^(Building configuration|Current configuration|"
                                     r"!Command:|!Running configuration|!Time:|"
                                     r"[A-Z][a-z]{2} [A-Z][a-z]{2} +\d+ \d\d:)", l)) + "\n"


def _scp(conn, local, remote, fs):
    from netmiko import file_transfer
    r = file_transfer(conn, source_file=local, dest_file=remote, file_system=fs,
                      direction="put", overwrite_file=True)
    if not r.get("file_transferred") and not r.get("file_exists"):
        raise SnapError("could not copy the config to %s" % fs)


def restore_live(lab, node, plat, c, raw_path, log):
    with open(raw_path) as fh:
        raw = fh.read()
    if plat == "frr":
        conn = devcfg.ExecConn(c["name"], plat)
        rc, out = conn.exec(["sh", "-c", "cat > /tmp/clabd-snap.conf && "
                             "/usr/lib/frr/frr-reload.py --reload /tmp/clabd-snap.conf; rc=$?; "
                             "rm -f /tmp/clabd-snap.conf; exit $rc"],
                            stdin=devcfg.clean_frr(raw), timeout=180)
        if rc != 0:
            raise SnapError("frr-reload failed: %s" % out.strip().splitlines()[-1:])
        return
    if plat == "srl":
        subprocess.run([DOCKER, "cp", raw_path, "%s:/tmp/clabd-snap.json" % c["name"]],
                       check=True, capture_output=True, timeout=60)
        conn = devcfg.ExecConn(c["name"], plat)
        rc, out = conn.exec(["sr_cli", "-ed", "--post", "commit now"],
                            stdin="load file /tmp/clabd-snap.json\n", timeout=180)
        conn.exec(["rm", "-f", "/tmp/clabd-snap.json"], timeout=20)
        if rc != 0 or "rror" in out:
            raise SnapError("commit failed: %s" % out.strip()[-300:])
        return
    body = _ios_body(raw)
    tmp = "/tmp/clabd-snap-%s-%s.cfg" % (lab.get("name"), node)
    with open(tmp, "w") as fh:
        fh.write(body)
    conn = devcfg._connect(lab, node, c, plat, timeout=600)
    try:
        if plat == "cisco_xe":
            conn.send_config_set(["ip scp server enable"])
            _scp(conn, tmp, "clabd-snap.cfg", "bootflash:")
            out = conn.send_command_timing("configure replace bootflash:clabd-snap.cfg force",
                                           read_timeout=300, last_read=5)
            if "Rollback Done" not in out and "rollback done" not in out.lower():
                raise SnapError("configure replace: %s" % out.strip()[-300:])
            conn.send_command_timing("delete /force bootflash:clabd-snap.cfg", read_timeout=30)
        elif plat == "cisco_nxos":
            conn.send_config_set(["feature scp-server"])
            _scp(conn, tmp, "clabd-snap.cfg", "bootflash:")
            out = conn.send_command_timing("configure replace bootflash:clabd-snap.cfg",
                                           read_timeout=600, last_read=10)
            if re.search(r"(fail|error)", out, re.I) and "successfully" not in out.lower():
                raise SnapError("configure replace: %s" % out.strip()[-300:])
            conn.send_command_timing("delete bootflash:clabd-snap.cfg no-prompt", read_timeout=30)
        elif plat == "cisco_xr":
            _scp(conn, tmp, "clabd-snap.cfg", "harddisk:")
            out = conn.config_mode()
            if devcfg._XR_INCONSISTENT in out:
                devcfg._xr_abort(conn)
                conn.send_command_timing("clear configuration inconsistency", read_timeout=180, last_read=4)
                conn.config_mode()
            out = conn.send_command_timing("load harddisk:/clabd-snap.cfg", read_timeout=120, last_read=3)
            if re.search(r"(Couldn't|error|failed)", out, re.I):
                devcfg._xr_abort(conn)
                raise SnapError("load: %s" % out.strip()[-300:])
            out = conn.send_command_timing("commit replace", read_timeout=60, last_read=3)
            if "Continue" in out or "[no]" in out:
                out += conn.send_command_timing("yes", read_timeout=300, last_read=5)
            if re.search(r"(Failed to commit|% ?Failed)", out):
                failed = conn.send_command_timing("show configuration failed", read_timeout=60)
                devcfg._xr_abort(conn)
                raise SnapError("commit replace failed: %s" % failed.strip()[-300:])
            conn.send_command_timing("end", read_timeout=30)
        else:
            raise SnapError("no live restore for %s" % plat)
    finally:
        try:
            conn.disconnect()
        except Exception:                               # noqa: BLE001
            pass
        try:
            os.remove(tmp)
        except OSError:
            pass


def restore_steps(lab, sid, mode, only, lab_index_fn, clab, audit):
    """Job steps for a restore. mode: live | redeploy."""
    d = _dir(lab, sid)
    with open(os.path.join(d, "meta.json")) as fh:
        meta = json.load(fh)
    nodes = [n for n, v in meta["nodes"].items() if v.get("ok") and (not only or n in only)]
    if not nodes:
        raise SnapError("nothing to restore - the snapshot has none of those nodes")
    current = {n: (p, c) for n, p, c in configurable_nodes(lab)}
    missing = [n for n in nodes if n not in current]
    if missing:
        raise SnapError("%s no longer in the lab" % ", ".join(missing))

    if mode == "live":
        down = [n for n in nodes if current[n][1] is None]
        if down:
            raise SnapError("%s not running - use restore by redeploy" % ", ".join(down), 409)

        def run(log):
            errors = []
            lock = threading.Lock()

            def one(n):
                p, c = current[n]
                t0 = time.time()
                try:
                    restore_live(lab_index_fn().get(lab["id"]) or lab, n, p, c,
                                 os.path.join(d, n + ".raw"), log)
                    log("%s: replaced (%s, %.0f s)" % (n, devcfg.PLATFORM_NAMES[p], time.time() - t0))
                except Exception as exc:              # noqa: BLE001
                    with lock:
                        errors.append(n)
                    log("%s: FAILED - %s" % (n, exc))
            ts = [threading.Thread(target=one, args=(n,), daemon=True) for n in nodes]
            for t in ts:
                t.start()
            for t in ts:
                t.join(timeout=900)
            if errors:
                raise RuntimeError("restore failed on %s" % ", ".join(errors))
        return [("replace the running config of %s with snapshot %r" % (", ".join(nodes), meta["name"]), run)]

    def write(log):
        cur = lab_index_fn().get(lab["id"]) or lab
        for n in nodes:
            p = current[n][0]
            rel, repoint = devcfg._startup_rel(cur, n, p)
            with open(os.path.join(d, n + ".cfg")) as fh:
                text = fh.read()
            full = os.path.join(os.path.dirname(cur["path"]), rel)
            editor.write_file(cur, rel, text, None, create=not os.path.isfile(full))
            if repoint and p == "frr":
                devcfg._add_node_bind(cur, n, rel, "/etc/frr/frr.conf")
            elif repoint:
                devcfg._set_node_startup(cur, n, rel)
            cur = lab_index_fn().get(lab["id"]) or cur
            log("%s: snapshot written to %s%s" % (n, rel, " (topology now points at it)" if repoint else ""))
    return [("write snapshot %r into the startup files" % meta["name"], write),
            [clab, "deploy", "--reconfigure", "-t", lab["path"]]]
