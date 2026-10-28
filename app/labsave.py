#!/usr/bin/env python3
"""
Save current config: make a running lab's present state the state it starts
from next time.

  * every router's running config, cleaned into startup form, is written into
    the startup file the topology uses for it (devcfg.fetch_startup /
    write_startup - the previous file goes to the lab's edit history, and a
    node without its own file gets one and the topology is pointed at it)
  * the map layout, when the page sends one, goes into the topology as each
    node's `builder-pos` label, so the drawing survives a browser change
  * a marker, <lab dir>/.clabd-saved/<lab>.json, records what was saved

Topology design changes made in the dashboard (live edits) are already written
to the topology file as they are applied; nothing more is needed for those.

The marker is written last, so its mtime is newer than every file the save
touched. server.config_state() counts it as a change since deploy, which makes
the next start a `deploy --reconfigure` - a plain deploy would reuse
clab-<lab>/ and boot the old configuration.

Plain linux nodes are not saved: their setup comes from `exec:` lines in the
topology, and a running shell has no config to read back.
"""

import io
import json
import os
import re
import threading
import time

import devcfg
import editor
import snapshots

MARK_DIR = ".clabd-saved"
# map coordinates are builder coordinates x 1.7 (topology.html)
MAP_SCALE = 1.7


def marker_path(topo_path, lab_name):
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", lab_name or "lab")
    return os.path.join(os.path.dirname(topo_path), MARK_DIR, name + ".json")


def read_marker(topo_path, lab_name):
    """(marker dict, mtime) or (None, 0.0)."""
    p = marker_path(topo_path, lab_name)
    try:
        with open(p) as fh:
            return json.load(fh), os.path.getmtime(p)
    except (OSError, ValueError):
        return None, 0.0


def clean_layout(layout, node_names):
    """{node: [x, y]} from the client, kept only for real topology nodes."""
    out = {}
    if not isinstance(layout, dict):
        return out
    for n, xy in layout.items():
        if n not in node_names or not isinstance(xy, (list, tuple)) or len(xy) != 2:
            continue
        try:
            x, y = float(xy[0]), float(xy[1])
        except (TypeError, ValueError):
            continue
        if abs(x) < 1e6 and abs(y) < 1e6:
            out[n] = (int(round(x / MAP_SCALE)), int(round(y / MAP_SCALE)))
    return out


def _write_layout(lab, pos):
    """Set labels.builder-pos on each node; returns how many changed."""
    with open(lab["path"]) as fh:
        text = fh.read()
    y = devcfg._pick_yaml(text)
    doc = y.load(text)
    nodes = doc["topology"]["nodes"]
    changed = 0
    for n, (x, yy) in sorted(pos.items()):
        if nodes.get(n) is None:
            nodes[n] = {}
        labels = nodes[n].get("labels")
        if labels is None:
            nodes[n]["labels"] = labels = {}
        val = "%d,%d" % (x, yy)
        if str(labels.get("builder-pos")) != val:
            labels["builder-pos"] = val
            changed += 1
    if changed:
        buf = io.StringIO()
        y.dump(doc, buf)
        editor.write_file(lab, os.path.basename(lab["path"]), buf.getvalue(), editor.sha(text))
    return changed


def save_step(lab_fn, layout, audit, who):
    """A job step (label, fn) that saves the lab; lab_fn() returns the lab as now."""

    def run(log):
        lab = lab_fn()
        if lab.get("parse_error"):
            raise RuntimeError("the topology file has errors - fix it in the editor first")
        targets, results = [], {}
        for node, plat, c in snapshots.configurable_nodes(lab):
            if c is None:
                results[node] = {"ok": False, "error": "not running - previous startup config kept"}
                log("%s: skipped - not running, its previous startup config is kept" % node)
            else:
                targets.append((node, plat))
        routers = {n for n, _, _ in snapshots.configurable_nodes(lab)}
        for n in lab.get("nodes") or []:
            if n["name"] not in routers:
                log("%s: no config to save (%s - set up by the topology file)" % (n["name"], n.get("kind")))
        if not targets:
            raise RuntimeError("no running router in this lab to save")

        # reading is slow (a minute on a big XR box) - do it in parallel
        fetched, lock = {}, threading.Lock()

        def one(node):
            t0 = time.time()
            try:
                plat, text = devcfg.fetch_startup(lab, node)
                with lock:
                    fetched[node] = (plat, text)
                log("%s: read %d lines (%.0f s)" % (node, len(text.splitlines()), time.time() - t0))
            except Exception as exc:                    # noqa: BLE001
                with lock:
                    results[node] = {"ok": False, "error": str(exc)[:300]}
                log("%s: FAILED to read - %s" % (node, exc))

        ts = [threading.Thread(target=one, args=(n,), daemon=True) for n, _ in targets]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=300)
        for n, _ in targets:
            if n not in fetched and n not in results:
                results[n] = {"ok": False, "error": "timed out reading the config"}
                log("%s: FAILED - timed out reading the config" % n)

        # writing may rewrite the topology file, so one node at a time
        for n in sorted(fetched):
            plat, text = fetched[n]
            try:
                r = devcfg.write_startup(lab, n, plat, text, audit)
                results[n] = {"ok": True, "path": r["path"], "lines": len(text.splitlines()),
                              "platform": devcfg.PLATFORM_NAMES.get(plat, plat)}
                log("%s: saved to %s%s" % (n, r["path"],
                                           " (topology now points at it)" if r["topology_updated"] else ""))
            except Exception as exc:                    # noqa: BLE001
                results[n] = {"ok": False, "error": str(exc)[:300]}
                log("%s: FAILED to write - %s" % (n, exc))

        moved = 0
        pos = clean_layout(layout, {n["name"] for n in lab.get("nodes") or []})
        if pos:
            try:
                moved = _write_layout(lab, pos)
                log("map layout: %s" % ("%d node position(s) written to the topology" % moved
                                        if moved else "unchanged"))
            except Exception as exc:                    # noqa: BLE001
                log("map layout: not saved - %s" % exc)

        ok = sorted(n for n, v in results.items() if v.get("ok"))
        failed = sorted(n for n, v in results.items() if not v.get("ok") and n in dict(targets))
        if ok:
            mark = {"saved": time.time(), "by": who, "lab": lab.get("name"),
                    "nodes": results, "layout_nodes": len(pos)}
            p = marker_path(lab["path"], lab.get("name"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                json.dump(mark, fh, indent=1)
            snapshots._own(os.path.dirname(p), lab)
        audit("LAB-SAVE lab=%s saved=%s failed=%s layout=%d from=%s"
              % (lab.get("name"), ",".join(ok) or "-", ",".join(failed) or "-", moved, who))
        if failed:
            raise RuntimeError("could not save %s%s" % (", ".join(failed),
                               " - the others were saved" if ok else ""))
        log("saved %d router(s). The next start boots from these configs (deploy --reconfigure)." % len(ok))

    return ("save the running config of every router", run)
