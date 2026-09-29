#!/usr/bin/env python3
"""CI smoke test: sign in, deploy the 'ci' lab through the API, check that
both nodes run and can ping each other, destroy it. Assumes the lab file
/opt/clab-topologies/ci/ci.clab.yml exists."""

import http.cookiejar
import json
import subprocess
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8080"
jar = http.cookiejar.CookieJar()
web = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def call(path, body=None):
    req = urllib.request.Request(BASE + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with web.open(req, timeout=30) as r:
        return json.load(r)


def wait_job(job_id, timeout=300):
    end = time.time() + timeout
    while time.time() < end:
        j = call("/api/job/%s" % job_id)
        if not j["running"]:
            print("\n".join(j["lines"][-15:]))
            return j["rc"]
        time.sleep(2)
    sys.exit("job %s timed out" % job_id)


def lab():
    for _ in range(30):
        for l in call("/api/state").get("labs", []):
            if l.get("name") == "ci":
                return l
        time.sleep(2)
    sys.exit("lab 'ci' was not discovered")


pw = subprocess.check_output(["sudo", "cat", "/var/lib/clab-dashboard/initial-admin-password"]).decode().strip()
print(call("/api/auth/login", {"user": "admin", "password": pw}))
print(call("/api/auth/me"))

l = lab()
rc = wait_job(call("/api/action", {"lab_id": l["id"], "action": "deploy"})["job_id"])
assert rc == 0, "deploy failed"
time.sleep(5)
l = lab()
assert l.get("running"), "lab is not running: %s" % l
subprocess.check_call(["sudo", "docker", "exec", "clab-ci-a", "ip", "addr", "add", "10.0.0.1/30", "dev", "eth1"])
subprocess.check_call(["sudo", "docker", "exec", "clab-ci-b", "ip", "addr", "add", "10.0.0.2/30", "dev", "eth1"])
subprocess.check_call(["sudo", "docker", "exec", "clab-ci-a", "ping", "-c", "3", "-W", "2", "10.0.0.2"])
rc = wait_job(call("/api/action", {"lab_id": l["id"], "action": "destroy"})["job_id"])
assert rc == 0, "destroy failed"
print("ok")
