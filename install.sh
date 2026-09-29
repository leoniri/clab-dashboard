#!/usr/bin/env bash
# clab-dashboard installer.
#
# Installs Docker (when missing), the latest containerlab, and the dashboard,
# on Debian/Ubuntu or RHEL-family (Rocky, Alma, RHEL, CentOS Stream, Fedora):
#
#     sudo ./install.sh                     # web UI on port 8080, login on
#     sudo ./install.sh --port 9000         # another port
#     sudo ./install.sh --listen 127.0.0.1  # only this address (e.g. behind an SSH tunnel)
#     sudo ./install.sh --clab-version 0.79.0   # pin containerlab instead of latest
#     sudo ./install.sh --no-clab           # leave Docker + containerlab alone
#     sudo ./install.sh --no-auth           # switch the login off (only with --listen 127.0.0.1!)
#     sudo ./install.sh --auth              # switch it back on
#     sudo ./install.sh --uninstall         # remove services + web config, keep lab data
#
# Re-running it upgrades in place: the dashboard, and containerlab to the
# latest release (unless --clab-version / --no-clab). Users, settings and labs
# are kept. Afterwards `clab-dashboard help` lists the admin commands.
set -euo pipefail

PORT=8080
LISTEN=
PREFIX=/opt/clab-dashboard
DATA=/var/lib/clab-dashboard
TTYD_VERSION=1.7.7
CLAB_TESTED=0.79.0          # newest containerlab this release was tested against
CLAB_VERSION=               # empty = latest
DO_CLAB=1
AUTH_MODE=keep              # keep | on | off
UNINSTALL=0
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT="${2:-}"; shift 2 ;;
        --listen) LISTEN="${2:-}"; shift 2 ;;
        --prefix) PREFIX="${2:-}"; shift 2 ;;
        --clab-version) CLAB_VERSION="${2#v}"; shift 2 ;;
        --no-clab) DO_CLAB=0; shift ;;
        --auth) AUTH_MODE=on; shift ;;
        --no-auth) AUTH_MODE=off; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option $1 (see --help)" >&2; exit 2 ;;
    esac
done

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!!  %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31mxx  %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root (sudo $0)"
command -v systemctl >/dev/null || die "systemd is required"

# ---------------------------------------------------------------- uninstall
if [ "$UNINSTALL" = 1 ]; then
    say "removing services and web config (lab data in $DATA, your topologies, Docker and containerlab stay)"
    systemctl disable --now clab-dashboard clab-term 2>/dev/null || true
    rm -f /etc/systemd/system/clab-dashboard.service /etc/systemd/system/clab-term.service
    systemctl daemon-reload
    rm -f /etc/nginx/sites-enabled/clab-dashboard /etc/nginx/sites-available/clab-dashboard \
          /etc/nginx/conf.d/clab-dashboard.conf /usr/local/bin/clab-dashboard
    if command -v nginx >/dev/null && nginx -t 2>/dev/null; then systemctl reload nginx || true; fi
    # LAN exposure rules live in our own chains; drop the jumps and the chains
    for spec in "nat PREROUTING CLABD-DNAT" "nat OUTPUT CLABD-DNAT" "nat POSTROUTING CLABD-SNAT" \
                "filter DOCKER-USER CLABD-FWD" "filter INPUT CLABD-IN"; do
        set -- $spec
        while iptables -t "$1" -D "$2" -j "$3" 2>/dev/null; do :; done
        iptables -t "$1" -F "$3" 2>/dev/null || true
        iptables -t "$1" -X "$3" 2>/dev/null || true
    done
    # our aliases carry the label <iface>:cl
    for ifc in $(ip -o -4 addr show | awk '{for (i = 5; i <= NF; i++) if ($i ~ /:cl\\?$/) print $2"|"$4}'); do
        ip addr del "${ifc#*|}" dev "${ifc%%|*}" 2>/dev/null || true
    done
    say "removed. $PREFIX and $DATA were left in place - delete them by hand if you want"
    exit 0
fi

# ------------------------------------------------------------------ checks
[ -f "$SRC/app/server.py" ] && [ -d "$SRC/app/static" ] \
    || die "run this from the unpacked clab-dashboard source (the directory holding app/)"
case "$PORT" in ''|*[!0-9]*) die "--port needs a number" ;; esac
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "--port must be 1-65535"
[ "$PORT" != 8090 ] && [ "$PORT" != 8091 ] || die "ports 8090 and 8091 are used internally - pick another"
if [ -n "$LISTEN" ]; then
    case "$LISTEN" in *[!0-9a-fA-F.:]*) die "--listen needs an IP address" ;; esac
fi
arch=$(uname -m)
case "$arch" in x86_64|aarch64) ;; *) die "unsupported CPU architecture $arch (x86_64 and aarch64 only)" ;; esac
. /etc/os-release 2>/dev/null || true
OS_ID="${ID:-unknown}"

# ---------------------------------------------------------------- packages
say "installing packages"
if command -v apt-get >/dev/null; then
    PKG=apt
    export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1
    echo "wireshark-common wireshark-common/install-setuid boolean false" | debconf-set-selections 2>/dev/null || true
    apt-get update -qq
    need="curl ca-certificates sudo python3 python3-yaml python3-paramiko nginx iproute2 iptables iputils-ping util-linux git rsync sshpass tcpdump"
    want="python3-netmiko python3-ruamel.yaml python3-pip ttyd tshark qemu-utils qemu-system-x86 genisoimage make linux-modules-extra-$(uname -r)"
    apt-get install -y -qq $need >/dev/null
    for p in $want; do apt-get install -y -qq "$p" >/dev/null 2>&1 || warn "package $p not available - skipped"; done
elif command -v dnf >/dev/null || command -v yum >/dev/null; then
    PKG=dnf
    PM=$(command -v dnf || command -v yum)
    # EPEL carries sshpass, ttyd and a few python modules on RHEL clones
    case "$OS_ID" in rocky|almalinux|rhel|centos|ol)
        $PM install -y -q epel-release >/dev/null 2>&1 || warn "EPEL not available - some optional packages will be skipped" ;;
    esac
    need="curl ca-certificates sudo python3 python3-pyyaml python3-paramiko nginx iproute iptables iputils util-linux git rsync tcpdump"
    want="python3-ruamel-yaml python3-pip sshpass ttyd wireshark-cli qemu-img qemu-kvm genisoimage make"
    $PM install -y -q $need >/dev/null
    for p in $want; do $PM install -y -q "$p" >/dev/null 2>&1 || warn "package $p not available - skipped"; done
else
    die "no apt-get, dnf or yum - this installer supports Debian/Ubuntu and RHEL-family hosts"
fi

pyok() { python3 -c "import $1" 2>/dev/null; }
for mod in netmiko ruamel.yaml; do
    if ! pyok "$mod"; then
        say "python module $mod from pip"
        python3 -m pip install -q "$mod" 2>/dev/null \
            || python3 -m pip install -q --break-system-packages "$mod" 2>/dev/null \
            || warn "could not install $mod - live protocol state and device config need it"
    fi
done
pyok yaml && pyok paramiko || die "python3 yaml + paramiko are required"

# --------------------------------------------------------- docker + clab
if [ "$DO_CLAB" = 1 ]; then
    if ! command -v docker >/dev/null; then
        say "installing Docker"
        case "$OS_ID" in
            debian|ubuntu|rocky|rhel|centos|almalinux|fedora)
                # containerlab's own installer pins a Docker release known to work with it
                curl -fsSL https://containerlab.dev/setup | bash -s install-docker ;;
            *)
                curl -fsSL https://get.docker.com | sh ;;
        esac
        command -v docker >/dev/null || die "Docker installation failed"
    fi
    systemctl enable -q --now docker

    have=$( (containerlab version 2>/dev/null || true) | sed -n 's/^ *version: *v\{0,1\}\([0-9][0-9.]*\).*/\1/p' | head -1)
    if [ -n "$CLAB_VERSION" ]; then
        if [ "$have" = "$CLAB_VERSION" ]; then
            say "containerlab $have (pinned) already installed"
        else
            say "installing containerlab $CLAB_VERSION"
            bash -c "$(curl -fsSL https://get.containerlab.dev)" -- -v "$CLAB_VERSION"
        fi
    else
        latest=$(curl -fsSI https://github.com/srl-labs/containerlab/releases/latest 2>/dev/null \
                 | sed -n 's|^[Ll]ocation: .*/tag/v\{0,1\}\([0-9][0-9.]*\).*|\1|p' | tr -d '\r' | head -1)
        if [ -n "$have" ] && [ "$have" = "$latest" ]; then
            say "containerlab $have is the latest release"
        else
            say "installing containerlab ${latest:-latest}${have:+ (was $have)}"
            bash -c "$(curl -fsSL https://get.containerlab.dev)"
        fi
    fi
else
    command -v docker >/dev/null || die "--no-clab given, but Docker is not installed"
fi
command -v containerlab >/dev/null || command -v clab >/dev/null || die "containerlab is not installed"
CLAB_NOW=$( (containerlab version 2>/dev/null || true) | sed -n 's/^ *version: *v\{0,1\}\([0-9][0-9.]*\).*/\1/p' | head -1)
if [ -n "$CLAB_NOW" ] && [ "$(printf '%s\n%s\n' "$CLAB_TESTED" "$CLAB_NOW" | sort -V | tail -1)" != "$CLAB_TESTED" ]; then
    warn "containerlab $CLAB_NOW is newer than $CLAB_TESTED, the version this release was tested with."
    warn "It will most likely work; if something breaks, pin it: --clab-version $CLAB_TESTED"
fi

# ------------------------------------------------------------------- ttyd
if ! command -v ttyd >/dev/null; then
    say "ttyd $TTYD_VERSION from GitHub (not packaged here)"
    curl -fsSL -o /usr/local/bin/ttyd \
        "https://github.com/tsl0922/ttyd/releases/download/$TTYD_VERSION/ttyd.$arch" \
        && chmod 755 /usr/local/bin/ttyd || die "could not download ttyd"
fi
TTYD=$(command -v ttyd)

# MPLS in the kernel, for FRR SR-MPLS / L3VPN labs
printf 'mpls_router\nmpls_iptunnel\nmpls_gso\n' > /etc/modules-load.d/clab-mpls.conf
modprobe -a mpls_router mpls_iptunnel mpls_gso 2>/dev/null || warn "MPLS kernel modules unavailable - FRR MPLS labs will not forward labels"

# ------------------------------------------------------------------- files
say "installing the dashboard into $PREFIX"
mkdir -p "$PREFIX" "$DATA" /opt/clab-topologies
chmod 700 "$DATA"
rsync -a --delete --exclude '__pycache__' --exclude '*.bak-*' "$SRC/app"/ "$PREFIX"/
install -m 755 "$SRC/install.sh" "$PREFIX/install.sh"
chown -R root:root "$PREFIX"
chmod 755 "$PREFIX"/*.py
VERSION=$(cat "$PREFIX/VERSION" 2>/dev/null || echo dev)

# ------------------------------------------------------------------- login
FRESH_PW=$(CLABD_DATA_DIR="$DATA" python3 "$PREFIX/auth.py" init)
case "$AUTH_MODE" in
    on)  CLABD_DATA_DIR="$DATA" python3 "$PREFIX/auth.py" enable >/dev/null ;;
    off) CLABD_DATA_DIR="$DATA" python3 "$PREFIX/auth.py" disable >/dev/null ;;
esac
AUTH_ON=$(python3 -c 'import json,sys; print(1 if json.load(open(sys.argv[1])).get("enabled", True) else 0)' "$DATA/auth.json")
if [ "$AUTH_ON" = 1 ] && ! nginx -V 2>&1 | grep -q http_auth_request_module; then
    die "this nginx lacks the auth_request module, which protects the web terminal - install a full nginx build"
fi
if [ "$AUTH_ON" = 0 ] && [ "$LISTEN" != 127.0.0.1 ] && [ "$LISTEN" != ::1 ]; then
    warn "login is OFF and the dashboard listens on ${LISTEN:-all addresses}: anyone who can reach"
    warn "port $PORT can run commands as root on this host. Use --listen 127.0.0.1 or --auth."
fi

# ---------------------------------------------------------------- services
cat > /etc/systemd/system/clab-dashboard.service <<EOF
[Unit]
Description=clab-dashboard - web UI for containerlab
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=$(command -v python3) $PREFIX/server.py
Restart=always
RestartSec=3
User=root
WorkingDirectory=$PREFIX
Environment=PYTHONUNBUFFERED=1
Environment=CLABD_DATA_DIR=$DATA

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/clab-term.service <<EOF
[Unit]
Description=clab-dashboard web terminal (ttyd)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
# --url-arg lets the browser pass the node name; term.py validates it against
# the containers containerlab reports as running before it execs anything.
# nginx only lets a request through to here after the dashboard's login check.
ExecStart=$TTYD --port 8091 --interface 127.0.0.1 --base-path /term --writable \\
          --max-clients 12 --url-arg --client-option fontSize=13 $PREFIX/term.py
Restart=always
RestartSec=3
User=root

[Install]
WantedBy=multi-user.target
EOF

cat > /usr/local/bin/clab-dashboard <<EOF
#!/usr/bin/env bash
# clab-dashboard admin commands - written by $PREFIX/install.sh
set -e
export CLABD_DATA_DIR=$DATA
REPO=\${CLABD_REPO:-leoniri/clab-dashboard}
need_root() { [ "\$(id -u)" = 0 ] || { echo "run as root (sudo clab-dashboard \$*)" >&2; exit 1; }; }
case "\${1:-help}" in
    status)   systemctl --no-pager status clab-dashboard clab-term ;;
    logs)     journalctl -u clab-dashboard -u clab-term -f ;;
    restart)  need_root; systemctl restart clab-dashboard clab-term ;;
    version)  echo "clab-dashboard \$(cat $PREFIX/VERSION)"; containerlab version 2>/dev/null | sed -n 's/^ *version:/containerlab/p' ;;
    passwd)   need_root; python3 $PREFIX/auth.py passwd "\${2:-admin}" ;;
    reset-password) need_root; python3 $PREFIX/auth.py reset "\${2:-admin}" ;;
    users)    need_root; python3 $PREFIX/auth.py list ;;
    deluser)  need_root; python3 $PREFIX/auth.py deluser "\$2" ;;
    auth)     need_root; case "\$2" in on) python3 $PREFIX/auth.py enable ;; off) python3 $PREFIX/auth.py disable ;;
                                       *) echo "usage: clab-dashboard auth on|off" >&2; exit 2 ;; esac ;;
    update)   need_root; shift; curl -fsSL "https://raw.githubusercontent.com/\$REPO/main/get.sh" | bash -s -- "\$@" ;;
    uninstall) need_root; $PREFIX/install.sh --uninstall ;;
    *) cat <<'USAGE'
usage: clab-dashboard <command>
  status | logs | restart | version
  passwd [user]           set a password (creates the user if new)
  reset-password [user]   new random password, printed (default user: admin)
  users | deluser <user>
  auth on|off             switch the login on or off
  update [install.sh options]   upgrade to the latest release (and containerlab to latest)
  uninstall               remove services + web config, keep labs and data
USAGE
    ;;
esac
EOF
chmod 755 /usr/local/bin/clab-dashboard

# ------------------------------------------------------------------- nginx
say "nginx on ${LISTEN:-*}:$PORT"
# Debian-style sites-available when the distro has it (decided only now that
# nginx is installed), conf.d otherwise; never both, or two server blocks
# fight over the port
if [ -d /etc/nginx/sites-available ]; then
    CONF=/etc/nginx/sites-available/clab-dashboard
    rm -f /etc/nginx/conf.d/clab-dashboard.conf
else
    CONF=/etc/nginx/conf.d/clab-dashboard.conf
fi
if [ -n "$LISTEN" ]; then
    case "$LISTEN" in *:*) LISTEN_LINES="    listen [$LISTEN]:$PORT;" ;; *) LISTEN_LINES="    listen $LISTEN:$PORT;" ;; esac
elif [ -f /proc/net/if_inet6 ]; then
    LISTEN_LINES="    listen $PORT;
    listen [::]:$PORT;"
else
    LISTEN_LINES="    listen $PORT;"
fi
cat > "$CONF" <<EOF
# clab-dashboard - written by $PREFIX/install.sh
map \$http_upgrade \$clabd_upgrade {
    default upgrade;
    ""      close;
}

server {
$LISTEN_LINES
    server_name _;

    proxy_http_version 1.1;
    proxy_set_header Host \$http_host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-Proto \$scheme;

    # the web terminal is served by ttyd directly, so nginx asks the
    # dashboard whether the browser is signed in before letting it through
    location = /_clabd_auth {
        internal;
        proxy_pass http://127.0.0.1:8090/api/auth/check;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
    }

    location /term/ {
        auth_request /_clabd_auth;
        proxy_pass http://127.0.0.1:8091;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$clabd_upgrade;
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
        proxy_buffering off;
    }

    location ~ ^/api/(images/upload|images/vendor-upload|lab/import/upload)$ {
        proxy_pass http://127.0.0.1:8090;
        client_max_body_size 0;
        proxy_request_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    location / {
        proxy_pass http://127.0.0.1:8090;
        proxy_read_timeout 300s;
        proxy_buffering off;
    }
}
EOF
if [ "$AUTH_ON" = 0 ]; then
    # nothing to ask when the login is off (and the module may be missing)
    sed -i '/auth_request \/_clabd_auth;/d' "$CONF"
fi
if [ "$CONF" = /etc/nginx/sites-available/clab-dashboard ]; then
    mkdir -p /etc/nginx/sites-enabled
    ln -sf "$CONF" /etc/nginx/sites-enabled/clab-dashboard
fi
# a distro default site on the same port would shadow ours
if [ "$PORT" = 80 ]; then rm -f /etc/nginx/sites-enabled/default; fi
# SELinux: let nginx proxy to the local backend
if command -v setsebool >/dev/null; then setsebool -P httpd_can_network_connect 1 2>/dev/null || true; fi
nginx -t 2>&1 | tail -1

# ---------------------------------------------------------------- firewall
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
    say "opening $PORT/tcp in ufw"; ufw allow "$PORT/tcp" >/dev/null
fi
if command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
    say "opening $PORT/tcp and the SSH gateway ports in firewalld"
    firewall-cmd -q --permanent --add-port="$PORT/tcp" --add-port=2222-2272/tcp && firewall-cmd -q --reload
fi

# ----------------------------------------------------------------- start
say "starting services"
systemctl daemon-reload
systemctl enable -q nginx clab-dashboard clab-term
systemctl restart clab-dashboard clab-term
systemctl reload-or-restart nginx
for _ in $(seq 1 45); do
    curl -sf "http://127.0.0.1:8090/api/health" >/dev/null && break
    sleep 1
done
curl -sf "http://127.0.0.1:8090/api/health" >/dev/null || die "the dashboard did not come up - journalctl -u clab-dashboard"

if [ -n "$LISTEN" ]; then host=$LISTEN; case "$host" in *:*) host="[$host]" ;; esac
else host=$(ip -o -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p'); fi
GW=$(python3 -c 'import json,sys; g=json.load(open(sys.argv[1])).get("gateway",{}); print("%s / %s" % (g.get("user",""), g.get("password","")))' \
     "$DATA/settings.json" 2>/dev/null || echo "see Manage > LAN access")

echo
say "clab-dashboard $VERSION is running - http://${host:-<this host>}:$PORT/"
echo "    containerlab ${CLAB_NOW:-?}"
if [ "$AUTH_ON" = 1 ]; then
    if [ -n "$FRESH_PW" ]; then
        printf '    login: \033[1madmin / %s\033[0m\n' "$FRESH_PW"
        echo "           (also in $DATA/initial-admin-password until you change it under Account)"
    else
        echo "    login: your existing users are unchanged (forgot it? sudo clab-dashboard reset-password)"
    fi
else
    echo "    login: OFF"
fi
echo "    SSH gateway into FRR / Linux nodes exposed on the LAN: $GW"
echo "    admin commands: clab-dashboard help"
