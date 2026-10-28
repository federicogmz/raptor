#!/usr/bin/env bash
# ==============================================================================
# RAPTOR v1.0.0 — Standalone One-Line Bootstrap Installer (Linux x86_64)
# Repository: https://github.com/federicogmz/raptor
#
# Usage:
#   curl -sSL https://github.com/federicogmz/raptor/releases/download/v1.0.0/raptor-install.sh | bash
#
# Custom environment variables (pass to bash on the right side of the pipe):
#   curl -sSL .../raptor-install.sh | RAPTOR_PORT=9090 RAPTOR_EXPORT_DIR=~/deliveries bash
#
# Supported Environment Variables:
#   RAPTOR_HOME        Installation directory (default: ~/.local/share/raptor)
#   RAPTOR_REF         Git branch or tag to checkout (default: main)
#   RAPTOR_EXPORT_DIR  Default GIS deliverables folder (default: ~/raptor-deliveries)
#   RAPTOR_PORT        Default Webapp HTTP port (default: 8080)
# ==============================================================================

main() {
  set -euo pipefail

  local RAPTOR_REPO="${RAPTOR_REPO:-https://github.com/federicogmz/raptor.git}"
  local RAPTOR_HOME="${RAPTOR_HOME:-$HOME/.local/share/raptor}"
  local RAPTOR_REF="${RAPTOR_REF:-main}"
  local ZENITH_RELEASE_URL="https://github.com/federicogmz/zenith/releases/download/v1.0.0/zenith-v1.0.0-linux-x86_64"
  local ZENITH_SHA256="c07ecf7f4abc736bee7b55396772bcf5ba7227eea2dc6363d58829f75c35c952"

  echo "======================================================================"
  echo "  RAPTOR // Multi-Sensor UAV Photogrammetry & Wildfire Triage Suite"
  echo "  Standalone Bootstrap Installer (Linux x86_64)"
  echo "======================================================================"

  # 1. Verify OS and Architecture
  local OS_NAME ARCH_NAME
  OS_NAME="$(uname -s)"
  ARCH_NAME="$(uname -m)"
  if [[ "$OS_NAME" != "Linux" ]]; then
    echo "❌ Error: RAPTOR requires Linux (detected: $OS_NAME)." >&2
    exit 1
  fi
  if [[ "$ARCH_NAME" != "x86_64" ]]; then
    echo "⚠  Warning: Prebuilt Zenith binary and DJI Thermal SDK target x86_64 (detected: $ARCH_NAME)." >&2
  fi

  # 2. Ensure git and curl are installed
  local cmd
  for cmd in git curl; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      echo "📦 Installing missing prerequisite: $cmd..."
      if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update -qq && sudo apt-get install -y -qq "$cmd" ca-certificates
      elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y "$cmd" ca-certificates
      elif command -v pacman >/dev/null 2>&1; then
        sudo pacman -S --needed --noconfirm "$cmd" ca-certificates
      else
        echo "❌ Error: '$cmd' is required. Please install it with your package manager." >&2
        exit 1
      fi
    fi
  done

  # 3. Install standalone Zenith v1.0.0 binary on host (x86_64 only, SHA256 verified)
  if [[ "$ARCH_NAME" == "x86_64" ]] && ! command -v zenith >/dev/null 2>&1 && [[ ! -x "$HOME/.local/bin/zenith" ]]; then
    echo "⚡ Installing standalone Zenith 2.5D engine (v1.0.0) on host..."
    local BIN_DIR="/usr/local/bin"
    if [[ ! -w "$BIN_DIR" ]]; then
      BIN_DIR="$HOME/.local/bin"
      mkdir -p "$BIN_DIR"
    fi
    local tmp_bin
    tmp_bin="$(mktemp)"
    if curl -fsSL "$ZENITH_RELEASE_URL" -o "$tmp_bin" && echo "${ZENITH_SHA256}  ${tmp_bin}" | sha256sum -c --quiet -; then
      install -m 0755 "$tmp_bin" "$BIN_DIR/zenith"
      echo "✅ Zenith v1.0.0 installed and verified at $BIN_DIR/zenith"
    else
      echo "ℹ  Note: Could not download/verify host Zenith binary (will still be available inside the RAPTOR container)."
    fi
    rm -f "$tmp_bin"
  fi

  # 4. Clone or update RAPTOR repository
  if [[ -d "$RAPTOR_HOME/.git" ]]; then
    echo "🔄 Updating existing RAPTOR installation at $RAPTOR_HOME..."
    git -C "$RAPTOR_HOME" fetch --tags origin
    git -C "$RAPTOR_HOME" checkout "$RAPTOR_REF"
    git -C "$RAPTOR_HOME" pull --ff-only origin "$RAPTOR_REF" || true
  else
    echo "📥 Cloning RAPTOR into $RAPTOR_HOME..."
    mkdir -p "$(dirname "$RAPTOR_HOME")"
    git clone --branch "$RAPTOR_REF" "$RAPTOR_REPO" "$RAPTOR_HOME"
  fi

  chmod +x "$RAPTOR_HOME/raptor"

  # 5. Delegate to ./raptor install (reattaching controlling TTY when available)
  echo "🚀 Launching './raptor install'..."
  cd "$RAPTOR_HOME"
  if [[ ! -t 0 ]] && { : < /dev/tty; } 2>/dev/null; then
    exec ./raptor install < /dev/tty
  else
    exec ./raptor install
  fi
}

main "$@"
