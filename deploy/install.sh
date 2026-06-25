#!/usr/bin/env bash
# Proov — install/refresh the always-on systemd service. Convenience only; the README
# ops section ("Operations — running Proov 24/7") is the source of truth.
#
# Idempotent: safe to re-run to deploy a new version. Does NOT touch secrets — it prints
# the one manual step (write /etc/proov/proov.env) instead of prompting for keys.
#
# Usage (run from the repo checkout, as root):
#   sudo ./deploy/install.sh
#
# Override paths via env if you changed them in deploy/proov.service:
#   PROOV_DIR=/opt/proov PROOV_USER=proov sudo -E ./deploy/install.sh
set -euo pipefail

PROOV_DIR="${PROOV_DIR:-/opt/proov}"
PROOV_USER="${PROOV_USER:-proov}"
SECRETS_DIR="${SECRETS_DIR:-/etc/proov}"
SECRETS_FILE="${SECRETS_FILE:-${SECRETS_DIR}/proov.env}"
UNIT_DST="/etc/systemd/system/proov.service"

# Resolve the repo root (this script lives in <repo>/deploy/).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "error: run as root (sudo ./deploy/install.sh)" >&2
  exit 1
fi

echo "==> Ensuring service user '${PROOV_USER}'"
if ! id -u "${PROOV_USER}" >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin "${PROOV_USER}"
fi

echo "==> Placing code in ${PROOV_DIR}"
install -d -o "${PROOV_USER}" -g "${PROOV_USER}" "${PROOV_DIR}"
# Copy the project (excluding VCS/venv/local state) into the install dir.
cp -a "${REPO_DIR}/." "${PROOV_DIR}/"
rm -rf "${PROOV_DIR}/.git" "${PROOV_DIR}/.venv"

echo "==> Creating virtualenv and installing Proov"
python3 -m venv "${PROOV_DIR}/.venv"
"${PROOV_DIR}/.venv/bin/pip" install --upgrade pip
"${PROOV_DIR}/.venv/bin/pip" install "${PROOV_DIR}"
chown -R "${PROOV_USER}:${PROOV_USER}" "${PROOV_DIR}"

echo "==> Installing systemd unit -> ${UNIT_DST}"
install -m 644 "${SCRIPT_DIR}/proov.service" "${UNIT_DST}"
install -d -m 700 "${SECRETS_DIR}"
systemctl daemon-reload

if [[ ! -f "${SECRETS_FILE}" ]]; then
  cat <<EOF

NEXT STEP (manual — secrets are never written by this script):
  sudo cp ${SCRIPT_DIR}/proov.env.example ${SECRETS_FILE}
  sudo chown root:root ${SECRETS_FILE}
  sudo chmod 600 ${SECRETS_FILE}
  sudo editor ${SECRETS_FILE}        # fill in CROO_API_KEY etc.

Then enable + start:
  sudo systemctl enable --now proov
  systemctl status proov
  journalctl -u proov -f             # watch it connect + heartbeat
EOF
else
  echo "==> ${SECRETS_FILE} already exists — restarting service for the new version"
  systemctl enable proov
  systemctl restart proov
  echo "Done. Check: systemctl status proov ; journalctl -u proov -f"
fi
