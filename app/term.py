#!/usr/bin/env python3
"""
Launch an SSH session to one containerlab node, for ttyd to attach a browser to.

ttyd is started with --url-arg, so whatever sits in ?arg= on the URL arrives
here as argv. That makes validation the whole job of this script: the argument
is only ever used after it has been matched against the set of containers that
containerlab currently reports as running. Anything else exits without running
a command.

Credentials come from the lab's own topology file (USERNAME / PASSWORD on the
node or its kind), so they are never sent to the browser. Native containers
(FRR, SR Linux, plain linux) have no SSH login of their own and get a
`docker exec` into vtysh, sr_cli or a shell instead.
"""

import json
import os
import re
import subprocess
import sys
import sysbin

CLAB = sysbin.find("clab", "containerlab")
DOCKER = sysbin.find("docker")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")
FRR_IMAGE_RE = re.compile(r"(^|/)frr(outing)?(/frr)?(:|$)|frrouting/")
DEFAULT_USER = "clab"
DEFAULT_PASS = "clab@123"

SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "ConnectTimeout=10",
    # old vrnetlab images (CSR1000v 16.09 and friends) offer SHA-1 kex and an
    # ssh-rsa host key only; re-enabling them is harmless against modern nodes
    "-o", "KexAlgorithms=+diffie-hellman-group14-sha1",
    "-o", "HostKeyAlgorithms=+ssh-rsa",
    "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
    "-o", "PubkeyAuthentication=no",
]


def die(msg, code=1):
    sys.stderr.write("\r\n  %s\r\n\r\n" % msg)
    sys.stderr.flush()
    # Give the browser a moment to render before ttyd tears the tab down.
    try:
        import time
        time.sleep(6)
    except Exception:                                  # noqa: BLE001
        pass
    sys.exit(code)


def running_nodes():
    """container name -> {lab, path, ip} for everything clab has deployed."""
    try:
        p = subprocess.run([CLAB, "inspect", "--all", "--format", "json"],
                           capture_output=True, text=True, timeout=60)
        data = json.loads(p.stdout or "{}")
    except Exception:                                  # noqa: BLE001
        return {}
    groups = data.values() if isinstance(data, dict) else [data]
    out = {}
    for group in groups:
        for c in group:
            nm = c.get("name")
            if not nm:
                continue
            out[nm] = {
                "lab": c.get("lab_name"),
                "path": c.get("absLabPath") or c.get("labPath"),
                "ip": (c.get("ipv4_address") or "").split("/")[0],
                "state": c.get("state"),
                "kind": c.get("kind"),
                "image": c.get("image"),
            }
    return out


def credentials(topo_path, lab, container):
    """USERNAME / PASSWORD for this node, from the topology file."""
    user, pw = DEFAULT_USER, DEFAULT_PASS
    if not topo_path or not os.path.isfile(topo_path):
        return user, pw
    try:
        import yaml
        with open(topo_path) as fh:
            doc = yaml.safe_load(fh) or {}
    except Exception:                                  # noqa: BLE001
        return user, pw

    topo = doc.get("topology") or {}
    nodes = topo.get("nodes") or {}
    kinds = topo.get("kinds") or {}
    short = container
    prefix = "clab-%s-" % (lab or "")
    if lab and short.startswith(prefix):
        short = short[len(prefix):]

    ncfg = nodes.get(short) if isinstance(nodes, dict) else None
    ncfg = ncfg if isinstance(ncfg, dict) else {}
    kcfg = kinds.get(ncfg.get("kind")) if isinstance(kinds, dict) else None
    kcfg = kcfg if isinstance(kcfg, dict) else {}

    env = {}
    env.update(kcfg.get("env") or {})
    env.update(ncfg.get("env") or {})
    if env.get("USERNAME"):
        user = str(env["USERNAME"])
    if env.get("PASSWORD"):
        pw = str(env["PASSWORD"])
    # the credentials the router was really started with win (containerlab
    # passes --username/--password; for n9kv it ignores the topology's env)
    try:
        import json as _json
        out = subprocess.run([DOCKER, "inspect", "-f", "{{json .Config.Cmd}}", container],
                             capture_output=True, text=True, timeout=15).stdout
        cmd = _json.loads(out or "null") or []
        args = {cmd[i][2:]: cmd[i + 1] for i in range(len(cmd) - 1)
                if cmd[i] in ("--username", "--password")}
        if args.get("username") and args.get("password"):
            user, pw = args["username"], args["password"]
    except Exception:                                  # noqa: BLE001
        pass
    return user, pw


def main():
    args = [a for a in sys.argv[1:] if a.strip()]
    if len(args) != 1:
        die("This terminal expects exactly one node name.")
    target = args[0].strip()
    if not NAME_RE.match(target):
        die("Refusing to open a session for %r." % target)

    nodes = running_nodes()
    info = nodes.get(target)
    if info is None:
        die("%s is not a running containerlab node.\n  Running now: %s"
            % (target, ", ".join(sorted(nodes)) or "nothing"))
    if info.get("state") != "running":
        die("%s is not running (state: %s)." % (target, info.get("state")))
    # native containers have no SSH server of their own: attach to their CLI
    kind, image = info.get("kind") or "", info.get("image") or ""
    shell = None
    if kind == "nokia_srlinux":
        shell = ["sr_cli"]
    elif kind == "linux" and FRR_IMAGE_RE.search(image):
        shell = ["vtysh"]
    elif kind == "linux":
        shell = ["sh", "-c", "command -v bash >/dev/null && exec bash || exec sh"]
    if shell:
        sys.stdout.write("attaching to %s (%s) ...\r\n" % (target, "FRR vtysh" if shell == ["vtysh"]
                                                             else "SR Linux CLI" if shell == ["sr_cli"]
                                                             else "shell"))
        sys.stdout.flush()
        os.execv(DOCKER, [DOCKER, "exec", "-it", target] + shell)

    ip = info.get("ip")
    if not ip:
        die("%s has no management address yet - it may still be booting." % target)

    user, pw = credentials(info.get("path"), info.get("lab"), target)

    sys.stdout.write("connecting to %s (%s) as %s ...\r\n" % (target, ip, user))
    sys.stdout.flush()

    ssh = ["ssh", "-tt"] + SSH_OPTS + ["%s@%s" % (user, ip)]
    sshpass = sysbin.find("sshpass")
    if pw and os.path.exists(sshpass):
        os.execv(sshpass, [sshpass, "-p", pw] + ssh)
    os.execvp("ssh", ssh)


if __name__ == "__main__":
    main()
