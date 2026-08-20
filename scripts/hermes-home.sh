#!/usr/bin/env bash
# Shared Hermes root resolution for install.sh and uninstall.sh.

resolve_hermes_root() {
  local home_dir
  if [ -n "${HERMES_HOME:-}" ]; then
    home_dir="$HERMES_HOME"
  elif [ -n "${LOCALAPPDATA:-}" ] && command -v cygpath >/dev/null 2>&1; then
    home_dir="$(cygpath -m "$LOCALAPPDATA")/hermes"
  elif [ -n "${XDG_DATA_HOME:-}" ]; then
    home_dir="$XDG_DATA_HOME/hermes"
  else
    home_dir="$HOME/.hermes"
  fi

  if command -v cygpath >/dev/null 2>&1 && [[ "$home_dir" == *\\* ]]; then
    home_dir="$(cygpath -m "$home_dir")"
  fi
  case "$home_dir" in
    */profiles/*) home_dir="${home_dir%%/profiles/*}" ;;
  esac
  printf '%s\n' "$home_dir"
}
