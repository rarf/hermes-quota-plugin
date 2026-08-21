#!/usr/bin/env bash
# Remove the quota backend, Desktop widget, and profile entries transactionally.
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/hermes-home.sh
source "$REPO_ROOT/scripts/hermes-home.sh"
source "$REPO_ROOT/scripts/hermes-config.sh"

HOME_DIR="$(resolve_hermes_root)"
export HERMES_HOME="$HOME_DIR"
QUOTA_PLUGIN="$HOME_DIR/plugins/quota"
QUOTA_DESKTOP="$HOME_DIR/desktop-plugins/quota"

if ! command -v hermes >/dev/null 2>&1; then
  echo "Cannot uninstall safely: 'hermes' is not on PATH; no changes were made." >&2
  exit 2
fi
hermes_config_init_python || exit $?

profiles=("")
if [ -d "$HOME_DIR/profiles" ]; then
  for profile_dir in "$HOME_DIR"/profiles/*; do
    [ -d "$profile_dir" ] || continue
    profiles+=("$(basename "$profile_dir")")
  done
fi

before_enabled_present=()
before_disabled_present=()
before_enabled=()
before_disabled=()

desired_enabled=()
desired_disabled=()
for profile in "${profiles[@]}"; do
  enabled_state="$(hermes_config_get_state "$profile" plugins.enabled)"
  disabled_state="$(hermes_config_get_state "$profile" plugins.disabled)"
  enabled_present="${enabled_state%%$'\t'*}"
  disabled_present="${disabled_state%%$'\t'*}"
  enabled="${enabled_state#*$'\t'}"
  disabled="${disabled_state#*$'\t'}"
  before_enabled_present+=("$enabled_present")
  before_disabled_present+=("$disabled_present")
  before_enabled+=("$enabled")
  before_disabled+=("$disabled")
  desired_enabled+=("$(printf '%s' "$enabled" | hermes_config_remove_quota)")
  desired_disabled+=("$(printf '%s' "$disabled" | hermes_config_remove_quota)")
done

mkdir -p "$HOME_DIR"
STAGE_DIR="$(mktemp -d "$HOME_DIR/.quota-uninstall.XXXXXX")"
trap 'rm -rf "$STAGE_DIR" || true' EXIT
BACKUP_PLUGIN="$STAGE_DIR/plugin"
BACKUP_DESKTOP="$STAGE_DIR/desktop"
PLUGIN_BACKED_UP=0
DESKTOP_BACKED_UP=0
CONFIG_APPLIED=0

rollback() {
  status=$?
  trap - ERR
  set +e

  i=0
  while [ "$i" -lt "$CONFIG_APPLIED" ]; do
    hermes_config_restore "${profiles[$i]}" plugins.enabled "${before_enabled_present[$i]}" "${before_enabled[$i]}" || true
    hermes_config_restore "${profiles[$i]}" plugins.disabled "${before_disabled_present[$i]}" "${before_disabled[$i]}" || true
    i=$((i + 1))
  done
  if [ "$PLUGIN_BACKED_UP" -eq 1 ]; then
    [ ! -e "$QUOTA_PLUGIN" ] || rm -rf "$QUOTA_PLUGIN"
    mv "$BACKUP_PLUGIN" "$QUOTA_PLUGIN" || true
  fi
  if [ "$DESKTOP_BACKED_UP" -eq 1 ]; then
    [ ! -e "$QUOTA_DESKTOP" ] || rm -rf "$QUOTA_DESKTOP"
    mv "$BACKUP_DESKTOP" "$QUOTA_DESKTOP" || true
  fi
  echo "Quota uninstall failed; previous files and configuration were restored where possible." >&2
  exit "$status"
}
trap rollback ERR

if [ -e "$QUOTA_PLUGIN" ]; then
  mv "$QUOTA_PLUGIN" "$BACKUP_PLUGIN"
  PLUGIN_BACKED_UP=1
fi
if [ -e "$QUOTA_DESKTOP" ]; then
  mv "$QUOTA_DESKTOP" "$BACKUP_DESKTOP"
  DESKTOP_BACKED_UP=1
fi

for i in "${!profiles[@]}"; do
  CONFIG_APPLIED=$((i + 1))
  hermes_config_apply "${profiles[$i]}" plugins.enabled "${before_enabled_present[$i]}" "${before_enabled[$i]}" "${desired_enabled[$i]}"
  hermes_config_apply "${profiles[$i]}" plugins.disabled "${before_disabled_present[$i]}" "${before_disabled[$i]}" "${desired_disabled[$i]}"
done

# Remove the per-profile symlinks install.sh creates (plugins/quota +
# desktop-plugins/quota under every named profile). Only remove symlinks —
# a real directory there was not created by us, so leave it alone.
REMOVED_LINKS=0
for profile in "${profiles[@]}"; do
  [ -z "$profile" ] && continue   # "" = default profile -> global roots handled above
  base="$HOME_DIR/profiles/$profile"
  for rel in plugins/quota desktop-plugins/quota; do
    link="$base/$rel"
    if [ -L "$link" ]; then
      rm "$link"
      REMOVED_LINKS=$((REMOVED_LINKS + 1))
    fi
  done
done

trap - ERR
if ! rm -rf "$BACKUP_PLUGIN" "$BACKUP_DESKTOP"; then
  echo "Warning: uninstall succeeded, but a temporary backup could not be removed: $STAGE_DIR" >&2
fi

printf '\nQuota plugin removed from %s\n' "$HOME_DIR"
if [ "$REMOVED_LINKS" -gt 0 ]; then
  printf '%s\n' "Removed $REMOVED_LINKS per-profile plugin links."
fi
printf '%s\n' 'Close every Hermes Desktop window, then reopen the app to unmount the Python backend.'
