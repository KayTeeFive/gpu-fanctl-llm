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

# Ensure script is run as root (required for /usr/local/bin and systemd)
if [[ $EUID -ne 0 ]]; then
  echo "[ERROR] Please run as root (sudo)"
  exit 1
fi

# Ensure Python script exists in current directory
if [[ ! -f "./${SCRIPT_NAME}" ]]; then
  echo "[ERROR] ${SCRIPT_NAME} not found in current directory"
  exit 1
fi

# ============================================================
# INSTALL SCRIPT
# ============================================================

echo "[INFO] Installing ${SCRIPT_NAME} → ${INSTALL_PATH}"
cp -vf "./${SCRIPT_NAME}" "${INSTALL_PATH}"
chmod +x "${INSTALL_PATH}"

# ============================================================
# COPY SYSTEMD UNIT
# ============================================================

echo "[INFO] Installing systemd unit ${SERVICE_NAME} → ${SERVICE_PATH}"

cp -vf "./${SERVICE_NAME}" "${SERVICE_PATH}"

# ============================================================
# ENABLE SERVICE
# ============================================================

echo "[INFO] Reloading systemd daemon"
systemctl daemon-reexec
systemctl daemon-reload

echo "[INFO] Enabling service"
systemctl enable "${SERVICE_NAME}"

echo "[INFO] Starting service"
systemctl restart "${SERVICE_NAME}"

echo "[OK] Installation complete"
