#!/usr/bin/env python3
"""
Vendor router images -> containerlab images.

Vendors ship virtual routers as disk images or installer ISOs, not as
containers. containerlab runs them through vrnetlab: a container that boots
the VM with QEMU. This module turns an uploaded vendor file into such an image:

  installer ISO  (IOS-XE: c8000v, csr1000v)
      boot the ISO headless in QEMU with an empty disk attached as IDE (vrnetlab
      attaches its disk as IDE, the install must match); the installer writes
      IOS-XE to the disk and reboots; "Press RETURN to get started" on the
      serial console means the disk is ready; stop QEMU; compact the disk
  qcow2 disk image
      used as is
  then
      vrnetlab `make docker-build` in a private copy of the vrnetlab tree
      (never in the user's checkout), with an explicit tag so an existing
      image is only replaced when asked to

Platform and version come from the file itself where possible (the IOS-XE ISO
carries <platform>-mono-universalk9.<version>.SPA.pkg), else from the name.
"""

import os
import re
import shutil
import signal
import subprocess
import time
import sysbin

DOCKER = sysbin.find("docker")
QEMU = sysbin.find("qemu-system-x86_64")
QEMU_IMG = sysbin.find("qemu-img")
ISOINFO = sysbin.find("isoinfo")
RSYNC = sysbin.find("rsync")
MAKE = sysbin.find("make")

BUILD_DIR = "/var/lib/clab-dashboard/builds"
# The vrnetlab tree used to package VM images: CLABD_VRNETLAB, else a checkout
# already on the host, else one cloned into the data directory on first use.
VRNETLAB_URL = "https://github.com/srl-labs/vrnetlab.git"
VRNETLAB_CANDIDATES = [os.environ.get("CLABD_VRNETLAB", ""),
                       os.path.join(sysbin.DATA_DIR, "vrnetlab"), "/opt/vrnetlab",
                       "/root/vrnetlab"]


def vrnetlab_src(log=None):
    for d in VRNETLAB_CANDIDATES:
        if d and os.path.isdir(os.path.join(d, "cisco")) and os.path.isdir(os.path.join(d, "common")):
            return d
    dest = os.path.join(sysbin.DATA_DIR, "vrnetlab")
    if log:
        log("  no vrnetlab checkout on this host - cloning %s" % VRNETLAB_URL)
    _stream([sysbin.find("git"), "clone", "--depth", "1", VRNETLAB_URL, dest], log or (lambda x: None))
    return dest
INSTALL_TIMEOUT = 40 * 60
TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,160}\.(iso|qcow2)$", re.I)
VER = r"(\d+\.\d+\.\d+[a-z]?)"
# a version in a file name: not glued to a word before it (universalk9.17.12.05b
# must give 17.12.05b, not 9.17.12)
NVER = r"(?<![0-9A-Za-z])(\d+\.\d+\.\d+[a-z]?)(?![0-9])"

PLATFORMS = {
    "c8000v": {
        "label": "Cisco Catalyst 8000V (IOS-XE)", "kind": "cisco_c8000v",
        "dir": "cisco/c8000v", "repo": "vrnetlab/cisco_c8000v",
        "iso": re.compile(r"^/c8000v-mono-universalk9\.%s\.SPA\.pkg$" % VER),
        "name": re.compile(r"c8000v.*?%s" % NVER, re.I),
        "qcow2": "c8000v-universalk9.{v}.qcow2", "make": ["MODE=autonomous"],
        "inputs": ("iso", "qcow2"), "verified": "iso",
    },
    "csr1000v": {
        "label": "Cisco CSR 1000v (IOS-XE)", "kind": "cisco_csr1000v",
        "dir": "cisco/csr1000v", "repo": "vrnetlab/cisco_csr1000v",
        "iso": re.compile(r"^/csr1000v-mono-universalk9\.%s\.SPA\.pkg$" % VER),
        "name": re.compile(r"csr1000v.*?%s" % NVER, re.I),
        "qcow2": "csr1000v-universalk9.{v}.qcow2", "make": [],
        "inputs": ("iso", "qcow2"), "verified": None,
    },
    "n9kv": {
        "label": "Cisco Nexus 9000v (NX-OS)", "kind": "cisco_n9kv",
        "dir": "cisco/n9kv", "repo": "vrnetlab/cisco_n9kv",
        "iso": None, "name": re.compile(r"n9kv-(.+?)\.qcow2$", re.I),
        # Cisco's own download names -> vrnetlab's n9kv-<model>-<version> tag
        "names": [
            (re.compile(r"^nexus(9\d{3})v(?:64)?[.-](\d+\.\d+\.\d+(?:\.[A-Za-z0-9]+)*)\.qcow2$", re.I),
             lambda m: "%s-%s" % (m.group(1), m.group(2))),
            (re.compile(r"^nxosv(?:-final)?[.-](\d+\.\d+\.\d+(?:\.[A-Za-z0-9]+)*)\.qcow2$", re.I),
             lambda m: m.group(1)),
        ],
        "qcow2": "n9kv-{v}.qcow2", "make": [], "inputs": ("qcow2",), "verified": "qcow2",
    },
    "xrv9k": {
        "label": "Cisco IOS XRv 9000 (IOS-XR)", "kind": "cisco_xrv9k",
        "dir": "cisco/xrv9k", "repo": "vrnetlab/cisco_xrv9k",
        "iso": None, "name": re.compile(r"xrv9k.*?(?<![0-9A-Za-z])(\d+\.\d+\.\d+)(?![0-9])", re.I),
        "qcow2": "xrv9k-fullk9-x-{v}.qcow2", "make": [], "inputs": ("qcow2",), "verified": None,
    },
}


class VendorError(Exception):
    pass


def platforms():
    out = []
    for key, p in PLATFORMS.items():
        out.append({"id": key, "label": p["label"], "kind": p["kind"], "repo": p["repo"],
                    "inputs": list(p["inputs"]), "verified": p["verified"]})
    return out


def _run(cmd, timeout=120):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as exc:                              # noqa: BLE001
        return 1, "", str(exc)


def guess(name):
    """(platform, version) from a file name alone - a hint before uploading."""
    ext = name.rsplit(".", 1)[-1].lower()
    for key, p in PLATFORMS.items():
        for rx, fmt in p.get("names", []):
            m = rx.match(name)
            if m and ext in p["inputs"]:
                return key, fmt(m)
    for key, p in PLATFORMS.items():
        m = p["name"].search(name)
        if m and ext in p["inputs"]:
            return key, m.group(1)
    for key, p in PLATFORMS.items():
        if p["name"].search(name):
            return key, None
    return None, None


def detect(path, platform=None, version=None):
    """(platform, version, how) from the file's content, falling back to its name,
    then to what the user picked (qcow2 only - an ISO is always read)."""
    name = os.path.basename(path)
    ext = name.rsplit(".", 1)[-1].lower()
    if ext == "iso":
        rc, out, err = _run([ISOINFO, "-R", "-f", "-i", path], timeout=120)
        if rc != 0:
            raise VendorError("not a readable ISO image: %s" % (err.strip() or "isoinfo failed"))
        for key, p in PLATFORMS.items():
            if not p["iso"]:
                continue
            for line in out.splitlines():
                m = p["iso"].match(line.strip())
                if m:
                    return key, m.group(1), "from %s inside the ISO" % line.strip().lstrip("/")
        raise VendorError("this ISO is not an installer this dashboard can convert. Supported "
                          "installer ISOs: " + ", ".join(p["label"] for p in PLATFORMS.values()
                                                         if "iso" in p["inputs"]))
    if ext == "qcow2":
        rc, out, _ = _run([QEMU_IMG, "info", path], timeout=60)
        if rc != 0 or "file format: qcow2" not in out:
            raise VendorError("not a qcow2 disk image")
        key, ver = guess(name)
        if key and ver:
            return key, ver, "from the file name"
        if platform in PLATFORMS and "qcow2" in PLATFORMS[platform]["inputs"] and version:
            return platform, version, "as chosen in the upload form"
        raise VendorError("cannot tell the platform from the file name %s - pick the platform "
                          "and a tag in the form" % name)
    raise VendorError("only .iso and .qcow2 files are supported")


def image_exists(ref):
    rc, _, _ = _run([DOCKER, "image", "inspect", ref], timeout=30)
    return rc == 0


def check(name, tag):
    """Pre-upload check the UI calls first: platform guess and tag conflicts."""
    if not NAME_RE.match(name or ""):
        raise VendorError("the file must be a .iso or .qcow2 with a plain file name")
    key, ver = guess(name)
    res = {"platform": key, "version": ver, "platforms": platforms()}
    if key:
        p = PLATFORMS[key]
        res["label"] = p["label"]
        res["accepts"] = name.rsplit(".", 1)[-1].lower() in p["inputs"]
        t = tag or ver
        if t:
            ref = "%s:%s" % (p["repo"], t)
            res["ref"] = ref
            res["exists"] = image_exists(ref)
    return res


# --------------------------------------------------------------------------
# the build
# --------------------------------------------------------------------------

def _stream(cmd, log, cwd=None, timeout=3600, every=None):
    """Run cmd, log its output; every=N logs only every Nth line (noisy tools)."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1, cwd=cwd)
    t0, n = time.time(), 0
    for line in p.stdout:
        n += 1
        line = line.rstrip()
        if line and (every is None or n % every == 0 or re.search(r"error|ERROR|fail", line)):
            log("  " + line[-240:])
        if time.time() - t0 > timeout:
            p.kill()
            raise VendorError("%s timed out after %d s" % (os.path.basename(cmd[0]), timeout))
    p.wait()
    if p.returncode != 0:
        raise VendorError("%s exited with %d" % (" ".join(cmd[:3]), p.returncode))


def install_iso(iso, disk, log):
    """Run the IOS-XE installer ISO onto disk; returns when IOS-XE is up."""
    rc, _, err = _run([QEMU_IMG, "create", "-f", "qcow2", disk, "16G"])
    if rc != 0:
        raise VendorError("qemu-img create: %s" % err.strip())
    cmd = [QEMU, "-enable-kvm", "-cpu", "host", "-smp", "2", "-m", "4096",
           "-drive", "if=ide,file=%s,format=qcow2" % disk,
           "-cdrom", iso, "-boot", "order=dc", "-nographic", "-nic", "none"]
    log("$ " + " ".join(cmd))
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, start_new_session=True)
    os.set_blocking(p.stdout.fileno(), False)
    t0, last_log, buf, milestones = time.time(), 0, b"", set()
    marks = [(b"Installing", "installer running"), (b"Rebooting", "installer finished, rebooting"),
             (b"System booted in", "IOS-XE booting from the new disk"),
             (b"Press RETURN to get started", None)]
    try:
        while True:
            if p.poll() is not None:
                raise VendorError("qemu exited (%s) before IOS-XE came up" % p.returncode)
            try:
                chunk = p.stdout.read(65536)
            except (BlockingIOError, TypeError):
                chunk = None
            if chunk:
                buf = (buf + chunk)[-200000:]
                for pat, msg in marks:
                    if pat in buf and pat not in milestones:
                        milestones.add(pat)
                        if msg:
                            log("  %s (%ds)" % (msg, time.time() - t0))
                if b"Press RETURN to get started" in buf:
                    log("  IOS-XE is up on the new disk (%ds) - stopping the VM" % (time.time() - t0))
                    break
            if time.time() - last_log > 30:
                tail = buf[-400:].decode("utf-8", "replace").replace("\r", "\n")
                lines = [l.strip() for l in tail.split("\n") if l.strip()]
                log("  ... %ds%s" % (time.time() - t0, (": " + lines[-1][:120]) if lines else ""))
                last_log = time.time()
            if time.time() - t0 > INSTALL_TIMEOUT:
                raise VendorError("the installer did not finish within %d min" % (INSTALL_TIMEOUT // 60))
            time.sleep(0.5)
    finally:
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()


def build(src, platform, version, tag, log, replace=False, boottest=True, avoid_subnets=()):
    """The whole pipeline for an uploaded file. Cleans up after itself."""
    p = PLATFORMS[platform]
    tag = tag or version
    if not tag or not TAG_RE.match(tag):
        raise VendorError("bad image tag %r" % tag)
    ref = "%s:%s" % (p["repo"], tag)
    if image_exists(ref) and not replace:
        raise VendorError("%s already exists - choose another tag or allow replacing it" % ref)
    work = os.path.dirname(src)
    t0 = time.time()
    try:
        ext = src.rsplit(".", 1)[-1].lower()
        qcow = os.path.join(work, p["qcow2"].format(v=version))
        if ext == "iso":
            log("step 1/3: installing %s from the ISO onto a new disk" % p["label"])
            raw = os.path.join(work, "install-disk.qcow2")
            install_iso(src, raw, log)
            log("  compacting the disk")
            _stream([QEMU_IMG, "convert", "-O", "qcow2", raw, qcow], log)
            os.remove(raw)
            os.remove(src)              # the uploaded ISO is no longer needed
            log("  disk ready: %s (%.1f GB)" % (os.path.basename(qcow), os.path.getsize(qcow) / 1024**3))
        else:
            log("step 1/3: qcow2 disk image - no install needed")
            if os.path.basename(src) != os.path.basename(qcow):
                os.rename(src, qcow)

        log("step 2/3: preparing a private vrnetlab build tree")
        tree = os.path.join(work, "vrnetlab")
        _stream([RSYNC, "-a", "--exclude", "*.qcow2", "--exclude", "*.iso", "--exclude", "*.tgz",
                 "--exclude", "*.vmdk", "--exclude", "cidfile", "--exclude", ".venv",
                 vrnetlab_src(log).rstrip("/") + "/", tree + "/"], log)
        pdir = os.path.join(tree, p["dir"])
        if not os.path.isdir(pdir):
            raise VendorError("vrnetlab has no %s directory" % p["dir"])
        shutil.move(qcow, os.path.join(pdir, os.path.basename(qcow)))

        log("step 3/3: building %s with vrnetlab (this boots the router once more to "
            "prepare it - a few minutes)" % ref)
        cmd = [MAKE, "-C", pdir, "IMAGE=%s" % os.path.basename(qcow), "VERSION=%s" % tag] \
            + p["make"] + ["docker-build"]
        log("$ " + " ".join(cmd))
        _stream(cmd, log, timeout=3600)
        if not image_exists(ref):
            raise VendorError("vrnetlab finished but %s does not exist" % ref)
        rc, out, _ = _run([DOCKER, "image", "inspect", "-f", "{{.Size}}", ref])
        size = int(out.strip() or 0)
        log("ready: %s (%.1f GB, containerlab kind %s) after %d min - it is in the builder "
            "palette and can be used in any topology" % (ref, size / 1024**3, p["kind"],
                                                         (time.time() - t0) // 60))
    finally:
        # our own scratch directory: the upload, the disks and the build tree
        shutil.rmtree(work, ignore_errors=True)
        log("cleaned up %s" % work)
    if boottest:
        boot_test(ref, p["kind"], log, avoid_subnets)
    else:
        log("boot test skipped - use 'boot test' on the image to check it later")


# --------------------------------------------------------------------------
# boot test: does the image actually come up in containerlab?
# --------------------------------------------------------------------------
# A build finishing only proves the image exists. The test starts it as a
# one-node lab (in BUILD_DIR, outside the lab scan roots, so it never shows up
# as a lab), waits for vrnetlab's health check, logs in with the credentials
# containerlab really gave it and runs "show version". The lab is always torn
# down again - with --cleanup, which also removes its own private mgmt network.

CLAB = sysbin.find("clab", "containerlab")
TESTS_FILE = "/var/lib/clab-dashboard/image-tests.json"
BOOT = {    # kind -> netmiko driver, boot timeout (s), RAM needed (MB)
    "cisco_c8000v":      ("cisco_xe", 900, 4096),
    "cisco_csr1000v":    ("cisco_xe", 900, 4096),
    "cisco_n9kv":        ("cisco_nxos", 1200, 10240),
    "cisco_xrv9k":       ("cisco_xr", 1800, 16384),
    "cisco_xrd_vrouter": ("cisco_xr", 900, 8192),
}
BOOT_ENV = {"USERNAME": "clab", "PASSWORD": "clab@123"}


def load_tests():
    import json
    try:
        with open(TESTS_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_test(ref, res):
    import json
    tests = load_tests()
    tests[ref] = res
    os.makedirs(os.path.dirname(TESTS_FILE), exist_ok=True)
    tmp = TESTS_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(tests, fh, indent=1, sort_keys=True)
    os.replace(tmp, TESTS_FILE)


def _free_subnet(avoid):
    import ipaddress
    used = [ipaddress.ip_network(a, strict=False) for a in avoid if a]
    rc, out, _ = _run([DOCKER, "network", "ls", "-q"])
    if rc == 0 and out.split():
        rc, out, _ = _run([DOCKER, "network", "inspect", "-f",
                           "{{range .IPAM.Config}}{{.Subnet}} {{end}}"] + out.split())
        for s in out.split():
            try:
                used.append(ipaddress.ip_network(s, strict=False))
            except ValueError:
                pass
    for third in range(254, 199, -1):
        net = ipaddress.ip_network("172.31.%d.0/24" % third)
        if not any(net.overlaps(u) for u in used):
            return str(net)
    raise VendorError("no free 172.31.200-254.0/24 subnet for the boot test")


def _mem_available_mb():
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


def boot_test(ref, kind, log, avoid_subnets=()):
    """Start ref as a one-node lab and check it boots. Returns the result dict
    (also stored in TESTS_FILE); raises VendorError when it does not boot."""
    import json
    if kind not in BOOT:
        raise VendorError("no boot test for kind %s" % kind)
    driver, timeout, ram = BOOT[kind]
    free = _mem_available_mb()
    if free and free < ram + 2048:
        raise VendorError("not enough free memory for a boot test: %s needs about %d GB, "
                          "%.1f GB is free - stop a lab and test again"
                          % (ref, ram // 1024, free / 1024))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = "clabd-boottest-%s" % stamp[-6:]
    work = os.path.join(BUILD_DIR, "boottest-" + stamp)
    os.makedirs(work, exist_ok=True)
    subnet = _free_subnet(avoid_subnets)
    topo = os.path.join(work, "%s.clab.yml" % name)
    with open(topo, "w") as fh:
        fh.write("# throw-away lab: containerlab dashboard image boot test\n"
                 "name: %s\nmgmt:\n  network: %s-mgmt\n  ipv4-subnet: %s\n"
                 "topology:\n  nodes:\n    dut:\n      kind: %s\n      image: %s\n"
                 "      env:\n%s"
                 % (name, name, subnet, kind, ref,
                    "".join("        %s: \"%s\"\n" % kv for kv in BOOT_ENV.items())))
    cname = "clab-%s-dut" % name
    t0 = time.time()
    res = {"ok": False, "when": time.strftime("%Y-%m-%d %H:%M"), "kind": kind}
    try:
        log("boot test: starting %s as a one-node lab (%s, mgmt %s)" % (ref, name, subnet))
        _stream([CLAB, "deploy", "-t", topo], log, every=25, timeout=600)
        last, status = 0, ""
        while time.time() - t0 < timeout:
            rc, out, _ = _run([DOCKER, "inspect", "-f",
                               "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}",
                               cname], timeout=20)
            status = out.strip()
            if status == "running healthy":
                break
            if not status.startswith("running"):
                raise VendorError("the container stopped (%s)" % (status or "gone"))
            if time.time() - last > 30:
                log("  booting ... %ds" % (time.time() - t0))
                last = time.time()
            time.sleep(5)
        else:
            raise VendorError("not healthy after %d min" % (timeout // 60))
        boot_s = int(time.time() - t0)
        log("  healthy after %ds - logging in" % boot_s)
        rc, out, _ = _run([DOCKER, "inspect", "-f", "{{json .Config.Cmd}} {{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", cname])
        cmd_json, _, ip = out.strip().rpartition(" ")
        try:
            cmd = json.loads(cmd_json or "[]") or []
        except ValueError:
            cmd = []
        creds = {cmd[i][2:]: cmd[i + 1] for i in range(len(cmd) - 1)
                 if cmd[i] in ("--username", "--password")}
        user = creds.get("username") or BOOT_ENV["USERNAME"]
        pw = creds.get("password") or BOOT_ENV["PASSWORD"]
        from netmiko import ConnectHandler
        conn = ConnectHandler(device_type=driver, host=ip, username=user, password=pw,
                              conn_timeout=30, auth_timeout=30, fast_cli=False)
        try:
            ver = conn.send_command("show version", read_timeout=90)
        finally:
            conn.disconnect()
        vline = next((l.strip() for l in ver.splitlines()
                      if re.search(r"(Software|NXOS|NX-OS|IOS XR|IOS XE).*[Vv]ersion|^\s*NXOS: version|"
                                   r"Cisco IOS XE Software|Cisco IOS XR Software", l)), "")
        log("  logged in as %s - %s" % (user, vline or "show version answered"))
        res.update({"ok": True, "boot_s": boot_s, "version": vline[:160], "login": user})
        log("boot test PASSED: %s boots in containerlab in %ds and accepts SSH logins" % (ref, boot_s))
        return res
    except Exception as exc:                            # noqa: BLE001
        res["error"] = str(exc).splitlines()[0][:300] if str(exc) else exc.__class__.__name__
        rc, out, _ = _run([DOCKER, "logs", "--tail", "400", cname], timeout=30)
        tail = [l for l in out.splitlines()
                if re.search(r"ERROR|WARN|error|fail|timeout|Traceback|spins|restart", l)][-12:]
        if tail:
            log("  last warnings from the node's boot log:")
            for l in tail:
                log("    " + l[-200:])
        res["log"] = tail
        log("boot test FAILED: %s - %s" % (ref, res["error"]))
        raise VendorError("%s did not pass the boot test: %s" % (ref, res["error"]))
    finally:
        _save_test(ref, res)
        _run([CLAB, "destroy", "-t", topo, "--cleanup"], timeout=300)
        shutil.rmtree(work, ignore_errors=True)
        log("boot test lab removed")
