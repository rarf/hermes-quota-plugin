#!/usr/bin/env bash
# Shared Hermes config and Python helpers for install.sh/uninstall.sh.

hermes_config_init_python() {
  python_works() {
    command -v "$1" >/dev/null 2>&1 &&
      "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1
  }
  if python_works python; then
    PYTHON_BIN=python
  elif python_works python3; then
    PYTHON_BIN=python3
  else
    echo "No working Python 3.9+ interpreter was found." >&2
    return 2
  fi
  export PYTHON_BIN
}

hermes_config_normalize_list() {
  "$PYTHON_BIN" -c 'import json,sys; v=json.load(sys.stdin); v=[] if v is None else v; isinstance(v,list) or (_ for _ in ()).throw(ValueError("expected JSON list")); print(json.dumps(v))'
}

hermes_config_add_quota() {
  "$PYTHON_BIN" -c 'import json,sys; v=json.load(sys.stdin); v=[x for x in v if x != "quota"]; print(json.dumps(v + ["quota"]))'
}

hermes_config_remove_quota() {
  "$PYTHON_BIN" -c 'import json,sys; v=json.load(sys.stdin); print(json.dumps([x for x in v if x != "quota"]))'
}

hermes_config_get_state() {
  local profile="$1" key="$2" raw normalized
  local -a args=()
  [ -z "$profile" ] || args=(-p "$profile")
  if raw="$(hermes "${args[@]}" config get "$key" --json 2>/dev/null)"; then
    if ! normalized="$(printf '%s' "$raw" | hermes_config_normalize_list)"; then
      echo "Invalid $key${profile:+ for profile $profile}; expected a JSON list; no changes were made." >&2
      return 1
    fi
    printf '1\t%s\n' "$normalized"
  elif raw="$(hermes "${args[@]}" config get "$key" --json 2>&1)"; [[ "$raw" == *"Config key not set: $key"* ]]; then
    printf '0\t[]\n'
  else
    echo "Failed to read $key${profile:+ for profile $profile}; no changes were made." >&2
    return 1
  fi
}

hermes_config_set_list() {
  local profile="$1" key="$2" value="$3"
  local -a args=()
  [ -z "$profile" ] || args=(-p "$profile")
  hermes "${args[@]}" config set "$key" "$value" >/dev/null
}

hermes_config_unset() {
  local profile="$1" key="$2"
  local -a args=()
  [ -z "$profile" ] || args=(-p "$profile")
  hermes "${args[@]}" config unset "$key" >/dev/null
}

# Sets CONFIG_CHANGED=1 only when the desired value differs from the snapshot.
hermes_config_apply() {
  local profile="$1" key="$2" was_present="$3" before="$4" desired="$5"
  CONFIG_CHANGED=0
  if [ "$was_present" = 0 ] && [ "$desired" = '[]' ]; then
    return 0
  fi
  if [ "$was_present" = 1 ] && [ "$before" = "$desired" ]; then
    return 0
  fi
  hermes_config_set_list "$profile" "$key" "$desired"
  CONFIG_CHANGED=1
}

hermes_config_restore() {
  local profile="$1" key="$2" was_present="$3" value="$4"
  if [ "$was_present" = 1 ]; then
    hermes_config_set_list "$profile" "$key" "$value"
  else
    hermes_config_unset "$profile" "$key"
  fi
}
