# Security

## What the dashboard can do

clab-dashboard is an admin tool. Its backend runs **as root** because containerlab,
Docker, network namespaces, iptables and packet capture need it. A signed-in user can:

- deploy, destroy and edit any lab the host can see, and run commands on lab nodes;
- pull, load, build and delete container images;
- add IP addresses to the host's LAN interface and NAT rules towards lab nodes (LAN access);
- open a shell on lab nodes through the web terminal and the SSH gateway.

Treat a dashboard login like root on the host.

## Defaults

- **Login on.** Every page, API call and web-terminal session needs a signed-in user. The
  installer generates a random `admin` password. Passwords are stored as PBKDF2-SHA256
  hashes in `/var/lib/clab-dashboard/auth.json` (mode 600). Sessions are signed HttpOnly
  cookies valid for 7 days; changing a password signs that user out everywhere.
  Failed logins back off per client address.
- The backend (127.0.0.1:8090) and the terminal (127.0.0.1:8091) listen on loopback only;
  nginx is the only public port, and asks the backend whether a browser is signed in
  before it opens a terminal.
- The browser sends lab ids, never file paths; the server resolves them against the labs it
  discovered. Terminal targets are checked against the running containers.
- Every action is written to `/var/log/clab-dashboard-actions.log` with the user and
  address, and to the journal.
- The SSH gateway's password is generated per install.

## Recommendations

- Change the generated password (*Account › Change password*), or set your own with
  `sudo clab-dashboard passwd admin`.
- The dashboard speaks plain HTTP. Beyond a trusted lab LAN, put it behind a TLS reverse
  proxy or a VPN, or install it with `--listen 127.0.0.1` and use an SSH tunnel:
  `ssh -L 8080:127.0.0.1:8080 you@labhost`.
- `--no-auth` is for loopback-only installs. With the login off, anyone who reaches the
  port has root on the host.
- Lab node credentials in generated topologies (for example `clab@123`) are lab defaults.
  Do not expose lab nodes to untrusted networks.

## Reporting a vulnerability

Please report security problems privately through GitHub's
[security advisories](https://github.com/leoniri/clab-dashboard/security/advisories/new)
rather than in a public issue.
