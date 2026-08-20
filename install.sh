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
  dashboard/plugin_api.py dashboard/manifest.json desktop/plugin.js \
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
  dashboard LICENSE SPEC.md scripts; do
  [ -e "$REPO_ROOT/$item" ] && cp -R "$REPO_ROOT/$item" "$STAGE_PLUGIN/"
done
cp "$REPO_ROOT/desktop/plugin.js" "$STAGE_DESKTOP/plugin.js"

(
  cd "$STAGE_PLUGIN"
  "$PYTHON_BIN" -m py_compile \
    __init__.py commands.py quota_cache.py dashboard/plugin_api.py \
    quota_providers/*.py
)
rm -rf \
  "$STAGE_PLUGIN/__pycache__" \
  "$STAGE_PLUGIN/dashboard/__pycache__" \
  "$STAGE_PLUGIN/quota_providers/__pycache__"

normalize_list() {
  "$PYTHON_BIN" -c 'import json,sys; v=json.load(sys.stdin); v=[] if v is None else v; isinstance(v,list) or (_ for _ in ()).throw(ValueError("expected JSON list")); print(json.dumps(v))'
}

mutate_list() {
  local mode="$1"
  "$PYTHON_BIN" -c 'import json,sys; mode=sys.argv[1]; v=json.load(sys.stdin); v=[x for x in v if x != "quota"]; print(json.dumps(v + (["quota"] if mode == "add" else [])))' "$mode"
}

config_get_state() {
  local profile="$1" key="$2" raw normalized
  local -a args=()
  [ -z "$profile" ] || args=(-p "$profile")
  if raw="$(hermes "${args[@]}" config get "$key" --json 2>&1)"; then
    if ! normalized="$(printf '%s' "$raw" | normalize_list)"; then
      echo "Invalid $key${profile:+ for profile $profile}; expected a JSON list; no changes were made." >&2
      return 1
    fi
    printf '1\t%s\n' "$normalized"
  elif [[ "$raw" == "Config key not set: $key"* ]]; then
    printf '0\t[]\n'
  else
    echo "Failed to read $key${profile:+ for profile $profile}; no changes were made." >&2
    return 1
  fi
}

config_set_list() {
  local profile="$1" key="$2" value="$3"
  local -a args=()
  [ -z "$profile" ] || args=(-p "$profile")
  hermes "${args[@]}" config set "$key" "$value" >/dev/null
}

config_unset() {
  local profile="$1" key="$2"
  local -a args=()
  [ -z "$profile" ] || args=(-p "$profile")
  hermes "${args[@]}" config unset "$key" >/dev/null
}

config_apply() {
  local profile="$1" key="$2" was_present="$3" desired="$4"
  if [ "$was_present" = 0 ] && [ "$desired" = '[]' ]; then
    return 0
  fi
  config_set_list "$profile" "$key" "$desired"
}

config_restore() {
  local profile="$1" key="$2" was_present="$3" value="$4"
  if [ "$was_present" = 1 ]; then
    config_set_list "$profile" "$key" "$value"
  else
    config_unset "$profile" "$key"
  fi
}

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

trap - ERR
if ! rm -rf "$BACKUP_PLUGIN" "$BACKUP_DESKTOP"; then
  echo "Warning: installation succeeded, but a temporary backup could not be removed: $STAGE_DIR" >&2
fi

printf '\nQuota plugin installed in %s\n' "$HOME_DIR"
printf '%s\n' 'IMPORTANT: close every Hermes Desktop window, then reopen the app.'
printf '%s\n' 'Reload desktop plugins refreshes JavaScript only; it does not remount dashboard/plugin_api.py.'
printf '%s\n' 'Verify with: hermes plugins doctor quota && hermes quota refresh && hermes quota status'
