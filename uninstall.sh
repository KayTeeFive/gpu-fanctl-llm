#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# CONFIG
# ============================================================

SERVICE_NAME="fan-control-multi-gpu.service"
SCRIPT_NAME="fan-control-multi-gpu.py"

INSTALL_PATH="/usr/local/bin/${SCRIPT_NAME}"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"

# ============================================================
# CHECKS
# ============================================================

if [[ $EUID -ne 0 ]]; then
  echo "[ERROR] Please run as root (sudo)"
  exit 1
fi

# ============================================================
# STOP AND DISABLE SERVICE
# ============================================================

if systemctl list-units --full -all | grep -q "${SERVICE_NAME}"; then
  echo "[INFO] Stopping service"
  systemctl stop "${SERVICE_NAME}" || true
fi

if systemctl list-unit-files | grep -q "${SERVICE_NAME}"; then
  echo "[INFO] Disabling service"
  systemctl disable "${SERVICE_NAME}" || true
fi

# ============================================================
# REMOVE FILES
# ============================================================

if [[ -f "${SERVICE_PATH}" ]]; then
  echo "[INFO] Removing systemd unit"
  rm -vf "${SERVICE_PATH}"
fi

if [[ -f "${INSTALL_PATH}" ]]; then
  echo "[INFO] Removing script"
  rm -vf "${INSTALL_PATH}"
fi

# ============================================================
# RELOAD SYSTEMD
# ============================================================

echo "[INFO] Reloading systemd daemon"
systemctl daemon-reload

echo "[OK] Uninstall complete"
