#!/usr/bin/env bash
# Install or update the quota backend and Desktop widget transactionally.
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/hermes-home.sh
source "$REPO_ROOT/scripts/hermes-home.sh"
source "$REPO_ROOT/scripts/hermes-config.sh"

HOME_DIR="$(resolve_hermes_root)"
export HERMES_HOME="$HOME_DIR"
PLUGIN_DIR="$HOME_DIR/plugins/quota"
DESKTOP_DIR="$HOME_DIR/desktop-plugins/quota"

for required in \
  plugin.yaml __init__.py commands.py quota_cache.py quota_providers \
  desktop/plugin.js \
  scripts/hermes-home.sh scripts/hermes-config.sh; do
  [ -e "$REPO_ROOT/$required" ] || {
    echo "Missing required file: $required" >&2
    exit 1
  }
done

if ! command -v hermes >/dev/null 2>&1; then
  echo "Cannot install safely: 'hermes' is not on PATH." >&2
  exit 2
fi
hermes_config_init_python || exit $?

mkdir -p "$HOME_DIR" "$HOME_DIR/plugins" "$HOME_DIR/desktop-plugins"
STAGE_DIR="$(mktemp -d "$HOME_DIR/.quota-install.XXXXXX")"
trap 'rm -rf "$STAGE_DIR" || true' EXIT
STAGE_PLUGIN="$STAGE_DIR/plugin"
STAGE_DESKTOP="$STAGE_DIR/desktop"
BACKUP_PLUGIN="$STAGE_DIR/backup-plugin"
BACKUP_DESKTOP="$STAGE_DIR/backup-desktop"
mkdir -p "$STAGE_PLUGIN" "$STAGE_DESKTOP"

for item in \
  plugin.yaml __init__.py commands.py quota_cache.py quota_providers \
  LICENSE scripts; do
  [ -e "$REPO_ROOT/$item" ] && cp -R "$REPO_ROOT/$item" "$STAGE_PLUGIN/"
done
cp "$REPO_ROOT/desktop/plugin.js" "$STAGE_DESKTOP/plugin.js"

(
  cd "$STAGE_PLUGIN"
  "$PYTHON_BIN" -m py_compile \
    __init__.py commands.py quota_cache.py \
    quota_providers/*.py
)
rm -rf \
  "$STAGE_PLUGIN/__pycache__" \
  "$STAGE_PLUGIN/quota_providers/__pycache__"

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
  desired_enabled+=("$(printf '%s' "$enabled" | hermes_config_add_quota)")
  desired_disabled+=("$(printf '%s' "$disabled" | hermes_config_remove_quota)")
done

CONFIG_APPLIED=0
PLUGIN_BACKED_UP=0
PLUGIN_INSTALLED=0
DESKTOP_BACKED_UP=0
DESKTOP_INSTALLED=0

rollback() {
  status=$?
  trap - ERR
  set +e

  if [ "$DESKTOP_INSTALLED" -eq 1 ]; then rm -rf "$DESKTOP_DIR"; fi
  if [ "$DESKTOP_BACKED_UP" -eq 1 ]; then mv "$BACKUP_DESKTOP" "$DESKTOP_DIR" || true; fi
  if [ "$PLUGIN_INSTALLED" -eq 1 ]; then rm -rf "$PLUGIN_DIR"; fi
  if [ "$PLUGIN_BACKED_UP" -eq 1 ]; then mv "$BACKUP_PLUGIN" "$PLUGIN_DIR" || true; fi

  i=0
  while [ "$i" -lt "$CONFIG_APPLIED" ]; do
    hermes_config_restore "${profiles[$i]}" plugins.enabled "${before_enabled_present[$i]}" "${before_enabled[$i]}" || true
    hermes_config_restore "${profiles[$i]}" plugins.disabled "${before_disabled_present[$i]}" "${before_disabled[$i]}" || true
    i=$((i + 1))
  done
  echo "Quota installation failed; previous files and configuration were restored where possible." >&2
  exit "$status"
}
trap rollback ERR

for i in "${!profiles[@]}"; do
  CONFIG_APPLIED=$((i + 1))
  hermes_config_apply "${profiles[$i]}" plugins.enabled "${before_enabled_present[$i]}" "${before_enabled[$i]}" "${desired_enabled[$i]}"
  hermes_config_apply "${profiles[$i]}" plugins.disabled "${before_disabled_present[$i]}" "${before_disabled[$i]}" "${desired_disabled[$i]}"
done

if [ -e "$PLUGIN_DIR" ]; then
  mv "$PLUGIN_DIR" "$BACKUP_PLUGIN"
  PLUGIN_BACKED_UP=1
fi
mv "$STAGE_PLUGIN" "$PLUGIN_DIR"
PLUGIN_INSTALLED=1

if [ -e "$DESKTOP_DIR" ]; then
  mv "$DESKTOP_DIR" "$BACKUP_DESKTOP"
  DESKTOP_BACKED_UP=1
fi
mv "$STAGE_DESKTOP" "$DESKTOP_DIR"
DESKTOP_INSTALLED=1

# Per-profile roots. Both the Python backend scanner (plugins/) and the
# Desktop runtime-plugin loader (desktop-plugins/) resolve under the ACTIVE
# profile's hermes_home — only 'default' uses the global root — so a plugin
# installed solely at $HOME_DIR is invisible to every named profile (the
# widget shows "backend unavailable"). Symlink, don't copy: one real copy
# stays the single source; updates propagate on every re-install.
PROFILE_LINKS=0
for profile in "${profiles[@]}"; do
  [ -z "$profile" ] && continue   # "" = default profile -> already served by the global roots
  base="$HOME_DIR/profiles/$profile"

  for pair in "plugins/quota:$PLUGIN_DIR" "desktop-plugins/quota:$DESKTOP_DIR"; do
    rel="${pair%%:*}"; target="${pair#*:}"
    link="$base/$rel"
    mkdir -p "$(dirname "$link")"
    if [ -L "$link" ]; then
      ln -sfn "$target" "$link"
    elif [ -e "$link" ]; then
      echo "Warning: $link exists and is not a symlink; leaving it untouched." >&2
      continue
    else
      ln -s "$target" "$link"
    fi
    PROFILE_LINKS=$((PROFILE_LINKS + 1))
  done
done

trap - ERR
if ! rm -rf "$BACKUP_PLUGIN" "$BACKUP_DESKTOP"; then
  echo "Warning: installation succeeded, but a temporary backup could not be removed: $STAGE_DIR" >&2
fi

printf '\nQuota plugin installed in %s\n' "$HOME_DIR"
if [ "$PROFILE_LINKS" -gt 0 ]; then
  printf '%s\n' "Linked into $PROFILE_LINKS per-profile plugin roots (backend + desktop widget)."
fi
printf '%s\n' 'IMPORTANT: close every Hermes Desktop window, then reopen the app.'
printf '%s\n' 'Desktop widget reloads on save; the backend is loaded at process start.'
printf '%s\n' 'Verify with: hermes plugins doctor quota && hermes quota refresh && hermes quota status'
