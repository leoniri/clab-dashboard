#!/usr/bin/env python3
"""
Built-in SSH gateway for lab nodes that have no SSH server of their own
(FRR, plain Linux hosts, other container-native NOSes while they boot).

LAN exposure DNATs port 22 of such a node's LAN address to this gateway
(<lan ip>:22 -> <lan ip>:port on this host). The gateway reads the address the
client dialled from its own socket, looks the node up (resolve()), and runs
the node's CLI in the container with `docker exec`:

    ssh user@<lan ip>                  interactive: vtysh / Cli / cli / sr_cli, else a shell
    ssh -t user@<lan ip> shell         a shell in the node instead of its CLI
    ssh user@<lan ip> "show ip route"  one command through the CLI (for scripts)

It lives inside the dashboard process: no system account, no sudo rule and
no change to the host's own sshd, so it works the same on any host the
dashboard is installed on. Credentials come from the dashboard's settings.
Only "session" channels with a shell or one command are allowed - no port
forwarding, no subsystems (sftp), no agent or X11.
"""

import fcntl
import hmac
import os
import select
import socket
import struct
import subprocess
import termios
import threading
import time

try:
    import paramiko
except ImportError:                                    # reported by status()
    paramiko = None

import sysbin

DOCKER = sysbin.find("docker")
KEY_FILE = os.path.join(sysbin.DATA_DIR, "sshgw_host_rsa_key")
MAX_SESSIONS = 32
FAIL_WINDOW, FAIL_MAX = 300, 10          # per source address
CLI_PROBE = ("for c in vtysh sr_cli Cli cli; do command -v $c >/dev/null 2>&1 "
             "&& { echo $c; exit 0; }; done; echo")
SHELL = ["sh", "-c", "command -v bash >/dev/null 2>&1 && exec bash -l || exec sh -l"]
# NOSes whose CLI *is* the shell (SONiC's show/config are shell commands; its
# vtysh is only the routing daemon's CLI)
SHELL_CLI_KINDS = ("sonic-vs", "sonic-docker", "sonic-vm", "host")


def _host_key():
    if os.path.isfile(KEY_FILE):
        return paramiko.RSAKey(filename=KEY_FILE)
    os.makedirs(os.path.dirname(KEY_FILE), exist_ok=True)
    key = paramiko.RSAKey.generate(3072)
    tmp = KEY_FILE + ".tmp"
    key.write_private_key_file(tmp)
    os.chmod(tmp, 0o600)
    os.replace(tmp, KEY_FILE)
    return key


def node_argv(container, command, tty, term, kind=None):
    """The docker exec command line for a login (command None) or one command."""
    cli = "" if kind in SHELL_CLI_KINDS else subprocess.run(
        [DOCKER, "exec", container, "sh", "-c", CLI_PROBE],
        capture_output=True, text=True, timeout=15).stdout.strip()
    argv = [DOCKER, "exec", "-i"] + (["-t"] if tty else []) + ["-e", "TERM=%s" % (term or "xterm"),
                                                              container]
    cmd = (command or "").strip()
    if cmd in ("shell", "sh", "bash"):
        return argv + SHELL
    if cmd:
        return argv + ([cli, "-c", cmd] if cli else ["sh", "-c", cmd])
    return argv + ([cli] if cli else SHELL)


class _Session(paramiko.ServerInterface if paramiko else object):
    def __init__(self, gw, peer):
        self.gw, self.peer = gw, peer
        self.ready = threading.Event()
        self.pty = None                  # (term, cols, rows)
        self.command = None
        self.fd = None                   # pty master once running
        self.user = None

    # -- authentication
    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        user, pw = self.gw.credentials()
        if not self.gw.allow_attempt(self.peer):
            return paramiko.AUTH_FAILED
        ok = hmac.compare_digest(username.encode(), user.encode()) and \
            hmac.compare_digest(password.encode(), pw.encode())
        if ok:
            self.user = username
            return paramiko.AUTH_SUCCESSFUL
        self.gw.failed(self.peer)
        time.sleep(1)
        return paramiko.AUTH_FAILED

    # -- channels: one session with a shell or a command, nothing else
    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, channel, term, width, height, pw, ph, modes):
        self.pty = (term.decode() if isinstance(term, bytes) else term, width, height)
        return True

    def check_channel_shell_request(self, channel):
        self.ready.set()
        return True

    def check_channel_exec_request(self, channel, command):
        self.command = command.decode("utf-8", "replace") if isinstance(command, bytes) else command
        self.ready.set()
        return True

    def check_channel_window_change_request(self, channel, width, height, pw, ph):
        if self.pty:
            self.pty = (self.pty[0], width, height)
        if self.fd is not None:
            _set_size(self.fd, width, height)
        return True

    def check_channel_env_request(self, channel, name, value):
        return False


def _set_size(fd, cols, rows):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows or 24, cols or 80, 0, 0))
    except OSError:
        pass


class Gateway:
    """resolve(local_ip) -> (container, label, kind) or (None, None, None);
    credentials() -> (user, password); log(text) for the audit log."""

    def __init__(self, resolve, credentials, log=lambda text: None):
        self.resolve, self.credentials, self.log = resolve, credentials, log
        self.port = None
        self.error = None if paramiko else "python paramiko is not installed"
        self._sock = None
        self._fails = {}
        self._lock = threading.Lock()
        self._sessions = 0

    # ------------------------------------------------------------ lifecycle
    def start(self, want_port):
        """Listen on want_port, or the next free port up to +50. Returns the
        port or None (then .error says why)."""
        if not paramiko:
            return None
        if self._sock and self.port == want_port:
            return self.port
        self.stop()
        try:
            self.key = _host_key()
        except Exception as exc:                          # noqa: BLE001
            self.error = "host key: %s" % exc
            return None
        last = None
        for port in range(want_port, want_port + 51):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", port))
                s.listen(64)
            except OSError as exc:
                s.close()
                last = exc
                continue
            self._sock, self.port, self.error = s, port, None
            threading.Thread(target=self._accept, args=(s,), daemon=True, name="sshgw").start()
            return port
        self.error = "no free port in %d-%d (%s)" % (want_port, want_port + 50, last)
        return None

    def stop(self):
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock, self.port = None, None

    @property
    def running(self):
        return self._sock is not None

    # --------------------------------------------------------- brute force
    def allow_attempt(self, peer):
        now = time.time()
        with self._lock:
            lst = [t for t in self._fails.get(peer, []) if now - t < FAIL_WINDOW]
            self._fails[peer] = lst
            return len(lst) < FAIL_MAX

    def failed(self, peer):
        with self._lock:
            self._fails.setdefault(peer, []).append(time.time())

    # ----------------------------------------------------------- sessions
    def _accept(self, s):
        while self._sock is s:
            try:
                conn, addr = s.accept()
            except OSError:
                return
            with self._lock:
                busy = self._sessions >= MAX_SESSIONS
                if not busy:
                    self._sessions += 1
            if busy:
                conn.close()
                continue
            threading.Thread(target=self._serve, args=(conn, addr[0]), daemon=True,
                             name="sshgw-%s" % addr[0]).start()

    def _serve(self, conn, peer):
        t = None
        try:
            local = conn.getsockname()[0]
            conn.settimeout(None)
            t = paramiko.Transport(conn)
            t.local_version = "SSH-2.0-clab-dashboard-gw"
            t.add_server_key(self.key)
            sess = _Session(self, peer)
            try:
                t.start_server(server=sess)
            except (paramiko.SSHException, EOFError, OSError):
                return
            chan = t.accept(60)
            if chan is None or not sess.ready.wait(30):
                return
            container, label, kind = self.resolve(local)
            if not container:
                chan.sendall_stderr(("no lab node is exposed at %s\r\n" % local).encode())
                chan.send_exit_status(1)
                return
            self.log("SSHGW %s login from %s%s" % (label, peer,
                                                   (" cmd=%r" % sess.command) if sess.command else ""))
            rc = self._run(chan, sess, container, kind)
            try:
                chan.send_exit_status(rc)
                chan.shutdown_write()
                chan.close()
            except Exception:                              # noqa: BLE001
                pass
        except Exception as exc:                           # noqa: BLE001
            self.log("SSHGW session from %s failed: %s" % (peer, exc))
        finally:
            with self._lock:
                self._sessions -= 1
            try:
                if t is not None:
                    time.sleep(0.5)                     # let the close reach the client first
                    t.close()
                else:
                    conn.close()
            except Exception:                              # noqa: BLE001
                pass

    def _run(self, chan, sess, container, kind=None):
        tty = sess.pty is not None
        argv = node_argv(container, sess.command, tty, sess.pty[0] if tty else None, kind)
        if tty:
            master, slave = os.openpty()
            _set_size(slave, sess.pty[1], sess.pty[2])
            proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                                    start_new_session=True, close_fds=True)
            os.close(slave)
            sess.fd = master
            try:
                self._pump_pty(chan, proc, master)
            finally:
                sess.fd = None
                os.close(master)
        else:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, close_fds=True)
            self._pump_pipes(chan, proc)
        try:
            return proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            return proc.wait()

    @staticmethod
    def _pump_pty(chan, proc, master):
        while True:
            r, _, _ = select.select([chan, master], [], [], 1.0)
            if chan in r:
                data = chan.recv(32768)
                if not data:                                # client went away
                    proc.terminate()
                    return
                os.write(master, data)
            if master in r:
                try:
                    data = os.read(master, 32768)
                except OSError:                             # EIO: the node's CLI exited
                    return
                if not data:
                    return
                chan.sendall(data)
            if chan.closed:
                proc.terminate()
                return

    @staticmethod
    def _pump_pipes(chan, proc):
        def feed():
            try:
                while True:
                    data = chan.recv(32768)
                    if not data:
                        break
                    proc.stdin.write(data)
                    proc.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                proc.stdin.close()
            except OSError:
                pass
        threading.Thread(target=feed, daemon=True).start()
        outs = {proc.stdout.fileno(): chan.sendall, proc.stderr.fileno(): chan.sendall_stderr}
        while outs:
            r, _, _ = select.select(list(outs), [], [], 1.0)
            for fd in r:
                data = os.read(fd, 32768)
                if not data:
                    outs.pop(fd)
                else:
                    outs[fd](data)
            if chan.closed:
                proc.terminate()
                return
