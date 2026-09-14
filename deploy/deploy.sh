#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:-all}"
DEPLOY_USER="${DEPLOY_USER:-root}"
DEPLOY_HOST="${DEPLOY_HOST:-192.168.10.115}"
REMOTE="${DEPLOY_USER}@${DEPLOY_HOST}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(dirname "${SCRIPT_DIR}")"

LOCAL_INDEX="${APP_DIR}/index.html"
LOCAL_SW="${APP_DIR}/sw.js"
LOCAL_APP="${APP_DIR}/backend/app.py"
LOCAL_REQUIREMENTS="${APP_DIR}/backend/requirements.txt"
LOCAL_BACKUP="${APP_DIR}/backend/backup.sh"
LOCAL_SERVICE="${APP_DIR}/backend/crib-score-api.service"
LOCAL_NGINX="${APP_DIR}/backend/crib-score.nginx.conf"
LOCAL_SEED="${APP_DIR}/crib_data.json"

REMOTE_WEB_DIR="/usr/share/nginx/html/crib-score"
REMOTE_APP="/opt/crib-score/backend/app.py"
REMOTE_REQUIREMENTS="/opt/crib-score/backend/requirements.txt"
REMOTE_BACKUP="/opt/crib-score/backend/backup.sh"
REMOTE_SERVICE="/etc/systemd/system/crib-score-api.service"
REMOTE_NGINX_COPY="/opt/crib-score/backend/crib-score.nginx.conf"
REMOTE_NGINX_CONF="/etc/nginx/conf.d/crib-score.conf"
REMOTE_SEED="/opt/crib-score/backend/crib_data.json"

log() { printf '\n[%s] %s\n' "$(date +'%Y-%m-%d %H:%M:%S')" "$*"; }

require_file() {
  if [[ ! -f "$1" ]]; then
    echo "Error: file not found: $1" >&2
    exit 1
  fi
}

configure_remote_nginx_port() {
  local port="$1"
  ssh "${REMOTE}" "bash -s -- ${port}" <<'EOSSH'
set -euo pipefail

port="$1"

if command -v getenforce >/dev/null 2>&1 && [[ "$(getenforce)" == "Enforcing" ]]; then
  if ! command -v semanage >/dev/null 2>&1; then
    if command -v dnf >/dev/null 2>&1; then
      dnf -y install policycoreutils-python-utils >/dev/null
    elif command -v yum >/dev/null 2>&1; then
      yum -y install policycoreutils-python-utils >/dev/null
    fi
  fi

  if ! command -v semanage >/dev/null 2>&1; then
    echo "Error: semanage not found, cannot authorize nginx to bind port ${port} under SELinux." >&2
    exit 1
  fi

  if ! semanage port -l | grep -E '^http_port_t\s+tcp\s+' | grep -Eq "(^|[ ,])${port}([ ,]|$)"; then
    semanage port -a -t http_port_t -p tcp "${port}" || semanage port -m -t http_port_t -p tcp "${port}"
  fi
fi

if command -v firewall-cmd >/dev/null 2>&1 && systemctl is-active --quiet firewalld; then
  firewall-cmd --permanent --add-port="${port}"/tcp >/dev/null || true
  firewall-cmd --reload >/dev/null || true
fi
EOSSH
}

verify_remote_port_listening() {
  local port="$1"
  ssh "${REMOTE}" "bash -s -- ${port}" <<'EOSSH'
set -euo pipefail

port="$1"
if ! ss -ltn | grep -q ":${port} "; then
  echo "Error: nginx is not listening on port ${port} after reload." >&2
  tail -n 40 /var/log/nginx/error.log || true
  exit 1
fi
EOSSH
}

deploy_frontend() {
  require_file "${LOCAL_INDEX}"
  log "Creating remote web directory"
  ssh "${REMOTE}" "mkdir -p ${REMOTE_WEB_DIR}"

  log "Copying index.html"
  scp "${LOCAL_INDEX}" "${REMOTE}:${REMOTE_WEB_DIR}/"

  if [[ -f "${LOCAL_SW}" ]]; then
    log "Copying optional sw.js"
    scp "${LOCAL_SW}" "${REMOTE}:${REMOTE_WEB_DIR}/"
  fi
}

deploy_backend() {
  require_file "${LOCAL_APP}"
  log "Creating remote backend directories"
  ssh "${REMOTE}" "mkdir -p /opt/crib-score/backend/data/backups"

  log "Copying backend app"
  scp "${LOCAL_APP}" "${REMOTE}:${REMOTE_APP}"

  log "Restarting crib-score-api"
  ssh "${REMOTE}" "systemctl restart crib-score-api"
}

deploy_install() {
  require_file "${LOCAL_APP}"
  require_file "${LOCAL_REQUIREMENTS}"
  require_file "${LOCAL_BACKUP}"
  require_file "${LOCAL_SERVICE}"
  require_file "${LOCAL_NGINX}"
  require_file "${LOCAL_INDEX}"
  require_file "${LOCAL_SEED}"

  log "Creating remote directories"
  ssh "${REMOTE}" "mkdir -p /opt/crib-score/backend/data/backups ${REMOTE_WEB_DIR}"

  log "Copying backend files"
  scp "${LOCAL_APP}" "${REMOTE}:${REMOTE_APP}"
  scp "${LOCAL_REQUIREMENTS}" "${REMOTE}:${REMOTE_REQUIREMENTS}"
  scp "${LOCAL_BACKUP}" "${REMOTE}:${REMOTE_BACKUP}"
  scp "${LOCAL_SEED}" "${REMOTE}:${REMOTE_SEED}"
  ssh "${REMOTE}" "chmod +x ${REMOTE_BACKUP}"

  log "Installing/updating Python venv"
  ssh "${REMOTE}" "
    if [[ ! -d /opt/crib-score/venv ]]; then
      python3 -m venv /opt/crib-score/venv
    fi
    /opt/crib-score/venv/bin/pip install --quiet --upgrade pip
    /opt/crib-score/venv/bin/pip install --quiet -r ${REMOTE_REQUIREMENTS}
    chown -R nginx:nginx /opt/crib-score
  "

  log "Copying frontend files"
  scp "${LOCAL_INDEX}" "${REMOTE}:${REMOTE_WEB_DIR}/"
  if [[ -f "${LOCAL_SW}" ]]; then
    scp "${LOCAL_SW}" "${REMOTE}:${REMOTE_WEB_DIR}/"
  fi
  ssh "${REMOTE}" "chown -R nginx:nginx ${REMOTE_WEB_DIR}"

  log "Installing systemd service"
  scp "${LOCAL_SERVICE}" "${REMOTE}:${REMOTE_SERVICE}"
  ssh "${REMOTE}" "systemctl daemon-reload && systemctl enable crib-score-api && systemctl restart crib-score-api"

  log "Installing nginx config"
  scp "${LOCAL_NGINX}" "${REMOTE}:${REMOTE_NGINX_COPY}"
  scp "${LOCAL_NGINX}" "${REMOTE}:${REMOTE_NGINX_CONF}"
  log "Authorizing nginx to bind TCP/82 (SELinux/firewall)"
  configure_remote_nginx_port 82
  ssh "${REMOTE}" "nginx -t && systemctl reload nginx"
  verify_remote_port_listening 82

  log "Verifying service health"
  ssh "${REMOTE}" "systemctl is-active crib-score-api && curl -s http://127.0.0.1:8002/api/health"
}

case "${TARGET}" in
  frontend) deploy_frontend ;;
  backend) deploy_backend ;;
  all) deploy_frontend; deploy_backend ;;
  install) deploy_install ;;
  *)
    echo "Usage: $0 {frontend|backend|all|install}" >&2
    exit 1
    ;;
esac

log "Deployment complete: ${TARGET}"
echo "   App: http://${DEPLOY_HOST}:82"
echo "   API: http://${DEPLOY_HOST}:82/api/health"
