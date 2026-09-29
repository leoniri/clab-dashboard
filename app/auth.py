#!/usr/bin/env python3
"""
Login for the dashboard.

The dashboard runs as root and can deploy, destroy and reconfigure anything
containerlab manages, so every page, API call and web-terminal session needs a
signed-in user. Stdlib only:

* users live in DATA_DIR/auth.json with PBKDF2-SHA256 password hashes;
* a session is a signed cookie (HMAC-SHA256 over user, expiry and the user's
  password generation), so it survives a backend restart and a password
  change signs that user out everywhere;
* failed logins back off per client address;
* nginx asks GET /api/auth/check before it opens a web terminal, so ttyd -
  which nginx proxies directly - is behind the same login.

Auth can be switched off (install.sh --no-auth, or "enabled": false in
auth.json) for a dashboard that only listens on 127.0.0.1.

Command line (run as root):
    auth.py init            create auth.json with user admin and a random
                            password, printed once and kept in
                            DATA_DIR/initial-admin-password; no-op when it exists
    auth.py passwd USER     set (or create) USER's password, asks for it
    auth.py reset [USER]    new random password for USER (default admin), printed
    auth.py deluser USER
    auth.py list
    auth.py enable | disable
"""

import base64
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time

import sysbin

AUTH_FILE = os.path.join(sysbin.DATA_DIR, "auth.json")
INITIAL_PW_FILE = os.path.join(sysbin.DATA_DIR, "initial-admin-password")
COOKIE = "clabd_session"
SESSION_TTL = 7 * 24 * 3600
PBKDF2_ITER = 310_000
USER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
MIN_PASSWORD = 8

# Reachable without a session: the login page and what it loads, the health
# probe the installer uses, and the endpoints that establish a session.
PUBLIC_PATHS = {"/login.html", "/ui.css", "/favicon.svg", "/api/health",
                "/api/auth/login", "/api/auth/check", "/api/auth/me"}


class AuthError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


# ------------------------------------------------------------------ hashing
def hash_password(pw):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, PBKDF2_ITER)
    return "pbkdf2_sha256$%d$%s$%s" % (PBKDF2_ITER, salt.hex(), dk.hex())


def verify_password(pw, stored):
    try:
        algo, it, salt, dk = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        got = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(got.hex(), dk)
    except (ValueError, AttributeError):
        return False


# A hash to verify against when the user does not exist, so an unknown user
# costs the same time as a wrong password.
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def random_password():
    # 16 characters from an alphabet without look-alikes
    alpha = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alpha) for _ in range(16))


# ------------------------------------------------------------------ storage
def _read():
    try:
        with open(AUTH_FILE) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None


def _write(data):
    os.makedirs(os.path.dirname(AUTH_FILE), exist_ok=True)
    tmp = AUTH_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, AUTH_FILE)


def _new_store():
    return {"enabled": True, "secret": secrets.token_hex(32), "users": {}}


def init_store():
    """Create auth.json with an admin user if there is none. Returns the new
    admin password, or None when the file already existed."""
    if _read() is not None:
        return None
    data = _new_store()
    pw = random_password()
    data["users"]["admin"] = {"hash": hash_password(pw), "gen": 1, "created": int(time.time())}
    _write(data)
    fd = os.open(INITIAL_PW_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(pw + "\n")
    return pw


# ------------------------------------------------------------------ runtime
class Auth:
    def __init__(self, log=None):
        self.lock = threading.Lock()
        self.log = log or (lambda text: None)
        self.fails = {}                 # ip -> [count, first_ts, locked_until]
        pw = None
        if os.environ.get("CLABD_AUTH", "").lower() not in ("off", "0", "false", "no"):
            pw = init_store()
        if pw:
            self.log("AUTH created user admin; initial password in %s" % INITIAL_PW_FILE)
        self._mtime = None
        self.data = _read() or dict(_new_store(), enabled=False)
        if os.environ.get("CLABD_AUTH", "").lower() in ("off", "0", "false", "no"):
            self.data["enabled"] = False

    @property
    def enabled(self):
        return bool(self.data.get("enabled", True))

    def fresh(self):
        """Pick up changes made with the command line (password resets,
        enable/disable) without a restart; costs one stat() per request."""
        try:
            m = os.stat(AUTH_FILE).st_mtime_ns
        except OSError:
            return
        if m == self._mtime:
            return
        with self.lock:
            d = _read()
            if d is not None:
                self._mtime = m
                self.data = d
                if os.environ.get("CLABD_AUTH", "").lower() in ("off", "0", "false", "no"):
                    self.data["enabled"] = False

    # -- sessions ---------------------------------------------------------
    def _sign(self, payload):
        key = bytes.fromhex(self.data["secret"])
        return base64.urlsafe_b64encode(
            hmac.new(key, payload.encode(), hashlib.sha256).digest()).decode().rstrip("=")

    def issue(self, user):
        u = self.data["users"][user]
        exp = int(time.time()) + SESSION_TTL
        payload = "%s|%d|%d" % (user, exp, u.get("gen", 1))
        body = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        return body + "." + self._sign(payload), exp

    def session_user(self, cookie_header):
        """User name for a valid session cookie, else None."""
        token = _cookie(cookie_header, COOKIE)
        if not token or "." not in token:
            return None
        body, sig = token.rsplit(".", 1)
        try:
            payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
            user, exp, gen = payload.split("|")
            exp, gen = int(exp), int(gen)
        except (ValueError, UnicodeDecodeError):
            return None
        if not hmac.compare_digest(sig, self._sign(payload)):
            return None
        if exp < time.time():
            return None
        u = self.data.get("users", {}).get(user)
        if not u or u.get("gen", 1) != gen:
            return None
        return user

    # -- login ------------------------------------------------------------
    def _throttle(self, ip):
        now = time.time()
        with self.lock:
            f = self.fails.get(ip)
            if f and f[2] > now:
                raise AuthError("too many failed attempts - try again in %d s"
                                % int(f[2] - now + 1), 429)

    def _failed(self, ip):
        now = time.time()
        with self.lock:
            f = self.fails.get(ip)
            if not f or now - f[1] > 900:
                f = [0, now, 0]
            f[0] += 1
            if f[0] >= 5:
                # 5 failures -> 30 s, doubling per further failure, max 15 min
                f[2] = now + min(900, 30 * 2 ** (f[0] - 5))
            self.fails[ip] = f
            if len(self.fails) > 10000:
                self.fails.clear()

    def login(self, user, password, ip):
        self._throttle(ip)
        u = self.data.get("users", {}).get(user or "")
        ok = verify_password(password or "", u["hash"] if u else _DUMMY_HASH)
        if not (u and ok):
            self._failed(ip)
            self.log("AUTH login failed user=%r from=%s" % (str(user)[:40], ip))
            raise AuthError("wrong user name or password", 401)
        with self.lock:
            self.fails.pop(ip, None)
        self.log("AUTH login user=%s from=%s" % (user, ip))
        return self.issue(user)

    def change_password(self, user, current, new, ip):
        self._throttle(ip)
        u = self.data.get("users", {}).get(user)
        if not u or not verify_password(current or "", u["hash"]):
            self._failed(ip)
            raise AuthError("current password is wrong", 403)
        _check_new(new)
        with self.lock:
            d = _read() or self.data
            du = d["users"][user]
            du["hash"] = hash_password(new)
            du["gen"] = du.get("gen", 1) + 1
            _write(d)
            self.data = d
            self._mtime = os.stat(AUTH_FILE).st_mtime_ns
        _drop_initial(user)
        self.log("AUTH password changed user=%s from=%s" % (user, ip))
        return self.issue(user)


def _check_new(pw):
    if not isinstance(pw, str) or len(pw) < MIN_PASSWORD:
        raise AuthError("the new password needs at least %d characters" % MIN_PASSWORD)
    if len(pw) > 256:
        raise AuthError("password too long")


def _drop_initial(user):
    # the initial password file is only useful until admin picks their own
    if user == "admin":
        try:
            os.remove(INITIAL_PW_FILE)
        except OSError:
            pass


def _cookie(header, name):
    for part in (header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return None


def cookie_header(value, max_age, secure):
    return "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax%s" % (
        COOKIE, value, max_age, "; Secure" if secure else "")


# ---------------------------------------------------------------- command line
def _cli(argv):
    if os.geteuid() != 0:
        raise SystemExit("run as root")
    cmd = argv[0] if argv else "help"
    if cmd == "init":
        pw = init_store()
        if pw:
            print(pw)
        return
    data = _read()
    if data is None:
        data = _new_store()
    users = data.setdefault("users", {})
    if cmd in ("passwd", "reset"):
        user = argv[1] if len(argv) > 1 else ("admin" if cmd == "reset" else None)
        if not user or not USER_RE.match(user):
            raise SystemExit("user name: 1-32 of A-Z a-z 0-9 _ . -")
        if cmd == "reset":
            pw = random_password()
        else:
            pw = getpass.getpass("new password for %s: " % user)
            if pw != getpass.getpass("again: "):
                raise SystemExit("passwords differ")
            try:
                _check_new(pw)
            except AuthError as exc:
                raise SystemExit(str(exc))
        u = users.setdefault(user, {"gen": 0, "created": int(time.time())})
        u["hash"] = hash_password(pw)
        u["gen"] = u.get("gen", 0) + 1
        _write(data)
        _drop_initial(user)
        print("%s: %s" % (user, pw) if cmd == "reset" else "password set for %s" % user)
    elif cmd == "deluser":
        if len(argv) < 2 or argv[1] not in users:
            raise SystemExit("no such user")
        del users[argv[1]]
        _write(data)
    elif cmd == "list":
        for name in sorted(users):
            print(name)
    elif cmd in ("enable", "disable"):
        data["enabled"] = cmd == "enable"
        _write(data)
        print("login %sd" % cmd)
    else:
        print(__doc__.split("Command line", 1)[1])


if __name__ == "__main__":
    _cli(sys.argv[1:])
