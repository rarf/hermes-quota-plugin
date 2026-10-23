#!/usr/bin/env bash
# Exercise install.sh / uninstall.sh only inside a guarded throwaway Hermes home.
set -Eeuo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
[ -f "$REPO/install.sh" ] && [ -f "$REPO/uninstall.sh" ] || {
  echo "Refusing to test: install.sh or uninstall.sh is missing." >&2
  exit 2
}

: "${HERMES_TEST_SCRATCH:?Set HERMES_TEST_SCRATCH to a dedicated scratch directory.}"
case "$HERMES_TEST_SCRATCH" in
  /*) ;;
  *) echo "Refusing to test: HERMES_TEST_SCRATCH must be an absolute path." >&2; exit 2 ;;
esac
[ "$HERMES_TEST_SCRATCH" != "/" ] || {
  echo "Refusing to test: HERMES_TEST_SCRATCH cannot be /." >&2
  exit 2
}
case "$HERMES_TEST_SCRATCH" in
  "$HOME/.hermes"|"$HOME/.hermes/plugins"*|"$HOME/.hermes/desktop-plugins"*|"$HOME/.hermes/profiles"*)
    echo "Refusing to test inside active Hermes plugins or profiles." >&2
    exit 2
    ;;
esac
mkdir -p "$HERMES_TEST_SCRATCH"
TEST_SCRATCH="$(cd "$HERMES_TEST_SCRATCH" && pwd -P)"
case "$TEST_SCRATCH" in
  "$HOME/.hermes"|"$HOME/.hermes/plugins"|"$HOME/.hermes/plugins/"*|\
  "$HOME/.hermes/desktop-plugins"|"$HOME/.hermes/desktop-plugins/"*|\
  "$HOME/.hermes/profiles"|"$HOME/.hermes/profiles/"*)
    echo "Refusing to test inside active Hermes plugins or profiles." >&2
    exit 2
    ;;
esac

RUN_ROOT="$(mktemp -d "$TEST_SCRATCH/quota-install-test.XXXXXX")"
trap 'rm -rf "$RUN_ROOT"' EXIT
FAKEBIN="$RUN_ROOT/bin"
mkdir -p "$FAKEBIN"
SYSTEM_PATH="${PATH:?PATH must be set}"
REAL_LN="$(command -v ln)"
REAL_READLINK="$(command -v readlink)"

cat > "$FAKEBIN/hermes" <<'FAKE_HERMES'
#!/bin/sh
if [ "${1:-}" = "--quota-shell-test-identity" ]; then
  printf '%s\n' 'quota-shell-test-fake-v1'
  exit 0
fi
key=""
prev=""
for a in "$@"; do
  if [ "$prev" = "get" ]; then key="$a"; fi
  prev="$a"
done
case "$*" in
  *"config get"*)
    echo "Config key not set: $key"
    exit 1 ;;
  *) exit 0 ;;
esac
FAKE_HERMES
chmod +x "$FAKEBIN/hermes"

cat > "$FAKEBIN/readlink" <<'FAKE_READLINK'
#!/bin/sh
if [ "${1:-}" = "-f" ]; then
  : > "$QUOTA_TEST_READLINK_F_MARKER"
  echo "simulated macOS readlink: -f is unsupported" >&2
  exit 1
fi
exec "$QUOTA_TEST_REAL_READLINK" "$@"
FAKE_READLINK
chmod +x "$FAKEBIN/readlink"

cat > "$FAKEBIN/ln" <<'FAKE_LN'
#!/bin/sh
if [ -n "${QUOTA_TEST_FAIL_LN_AT:-}" ]; then
  count=0
  if [ -f "$QUOTA_TEST_LN_COUNT_FILE" ]; then
    count="$(cat "$QUOTA_TEST_LN_COUNT_FILE")"
  fi
  count=$((count + 1))
  printf '%s\n' "$count" > "$QUOTA_TEST_LN_COUNT_FILE"
  if [ "$count" -eq "$QUOTA_TEST_FAIL_LN_AT" ]; then
    echo "simulated ln failure at call $count" >&2
    exit 73
  fi
fi
exec "$QUOTA_TEST_REAL_LN" "$@"
FAKE_LN
chmod +x "$FAKEBIN/ln"
export QUOTA_TEST_REAL_LN="$REAL_LN"
export QUOTA_TEST_REAL_READLINK="$REAL_READLINK"
export QUOTA_TEST_READLINK_F_MARKER="$RUN_ROOT/readlink-f-called"
export PATH="$FAKEBIN:$SYSTEM_PATH"

fail_closed() {
  echo "FATAL: $*" >&2
  exit 2
}

assert_fake_launcher() {
  local resolved identity
  resolved="$(command -v hermes || true)"
  [ "$resolved" = "$FAKEBIN/hermes" ] ||
    fail_closed "fake hermes is not first on PATH (found: ${resolved:-none})"
  identity="$(hermes --quota-shell-test-identity)"
  [ "$identity" = "quota-shell-test-fake-v1" ] ||
    fail_closed "fake hermes identity check failed"
}

assert_safe_home() {
  local home="$1"
  [ -n "$home" ] || fail_closed "HERMES_HOME cannot be empty"
  case "$home" in
    "$RUN_ROOT"/home.*) ;;
    *) fail_closed "HERMES_HOME is outside this run's scratch directory: $home" ;;
  esac
  [ -d "$home" ] || fail_closed "throwaway HERMES_HOME does not exist: $home"
}

new_home() {
  local home
  home="$(mktemp -d "$RUN_ROOT/home.XXXXXX")"
  mkdir -p "$home/profiles/alpha" "$home/profiles/beta"
  printf '%s\n' "$home"
}

run_shell() {
  local script="$1" home="$2" log="$3"
  assert_fake_launcher
  assert_safe_home "$home"
  (
    cd "$REPO"
    export PATH="$FAKEBIN:$SYSTEM_PATH"
    export HERMES_HOME="$home"
    "./$script"
  ) > "$log" 2>&1
}

PASS=0
FAIL=0
ok()  { printf '  PASS  %s\n' "$1"; PASS=$((PASS + 1)); }
bad() { printf '  FAIL  %s\n' "$1"; FAIL=$((FAIL + 1)); }

assert_fake_launcher
printf '%s\n' '=== T1: isolated install is repeatable ==='
H1="$(new_home)"
if run_shell install.sh "$H1" "$RUN_ROOT/install-1.log"; then
  ok "first install exit 0"
else
  bad "first install failed"
  cat "$RUN_ROOT/install-1.log"
fi
[ -d "$H1/plugins/quota" ] && ok "backend installed" || bad "backend missing"
[ -f "$H1/plugins/quota/codex_special_reset.py" ] && ok "special reset collector installed" || bad "special reset collector missing"
[ -f "$H1/plugins/quota/quota_monitor/__init__.py" ] && ok "monitor package installed" || bad "monitor package missing"
[ -d "$H1/desktop-plugins/quota" ] && ok "widget installed" || bad "widget missing"
L="$H1/profiles/alpha/plugins/quota"
[ -L "$L" ] && [ -e "$L" ] && ok "alpha backend link resolves" || bad "alpha backend link broken: $L"
L2="$H1/profiles/alpha/desktop-plugins/quota"
[ -L "$L2" ] && [ -e "$L2" ] && ok "alpha widget link resolves" || bad "alpha widget link broken"
if run_shell install.sh "$H1" "$RUN_ROOT/install-2.log"; then
  ok "second install exit 0 (idempotent)"
else
  bad "second install failed"
  cat "$RUN_ROOT/install-2.log"
fi
[ -L "$L" ] && [ -e "$L" ] && ok "link still resolves after re-install" || bad "link broken after re-install"

printf '\n%s\n' '=== T2: a foreign development symlink survives uninstall ==='
H2="$(new_home)"
FOREIGN="$RUN_ROOT/dev-checkout"
mkdir -p "$FOREIGN/quota" "$H2/profiles/alpha/plugins"
"$REAL_LN" -s "$FOREIGN" "$H2/profiles/alpha/plugins/quota"
if run_shell uninstall.sh "$H2" "$RUN_ROOT/uninstall-foreign.log"; then
  ok "uninstall exit 0"
else
  bad "uninstall failed"
  cat "$RUN_ROOT/uninstall-foreign.log"
fi
if [ -L "$H2/profiles/alpha/plugins/quota" ]; then
  ok "foreign symlink preserved"
else
  bad "foreign symlink was deleted"
fi
grep -q "Leaving" "$RUN_ROOT/uninstall-foreign.log" && ok "uninstall explained preserved link" || bad "no explanation printed"

printf '\n%s\n' '=== T3: uninstall removes links created by install ==='
H3="$(new_home)"
if run_shell install.sh "$H3" "$RUN_ROOT/install-3.log" &&
   run_shell uninstall.sh "$H3" "$RUN_ROOT/uninstall-3.log"; then
  ok "install and uninstall exit 0"
else
  bad "install or uninstall failed"
  cat "$RUN_ROOT/install-3.log" "$RUN_ROOT/uninstall-3.log"
fi
[ ! -L "$H3/profiles/alpha/plugins/quota" ] && ok "backend profile link removed" || bad "backend profile link survived uninstall"
[ ! -L "$H3/profiles/alpha/desktop-plugins/quota" ] && ok "widget profile link removed" || bad "widget profile link survived uninstall"
[ ! -e "$H3/plugins/quota" ] && ok "backend dir removed" || bad "backend dir survived"
[ ! -e "$H3/desktop-plugins/quota" ] && ok "widget dir removed" || bad "widget dir survived"

printf '\n%s\n' '=== T4: install reports resolved profile links ==='
H4="$(new_home)"
if run_shell install.sh "$H4" "$RUN_ROOT/install-4.log"; then
  ok "install exit 0"
else
  bad "install failed"
  cat "$RUN_ROOT/install-4.log"
fi
grep -q "Linked into" "$RUN_ROOT/install-4.log" && ok "install reports link count" || bad "no link report"
if grep -q "does not resolve" "$RUN_ROOT/install-4.log"; then
  bad "healthy install warned about broken links"
else
  ok "healthy install: no false warning"
fi

printf '\n%s\n' '=== T5: failed install removes links created during this run ==='
H5="$(new_home)"
printf '%s\n' 'not a directory' > "$H5/profiles/beta/plugins"
if run_shell install.sh "$H5" "$RUN_ROOT/install-fail-midway.log"; then
  bad "install unexpectedly succeeded"
else
  ok "install failed as intended"
fi
leftover=0
for link in "$H5"/profiles/*/plugins/quota "$H5"/profiles/*/desktop-plugins/quota; do
  [ -L "$link" ] || continue
  leftover=$((leftover + 1))
done
[ "$leftover" -eq 0 ] && ok "no profile links left after rollback" || bad "$leftover profile link(s) left after rollback"
[ ! -e "$H5/plugins/quota" ] && ok "partial backend dir cleaned up" || bad "backend survived failed install"

printf '\n%s\n' '=== T6: macOS readlink without -f does not trigger rollback ==='
H6="$(new_home)"
if run_shell install.sh "$H6" "$RUN_ROOT/install-macos-link.log"; then
  ok "install for macOS resolver probe exit 0"
else
  bad "install for macOS resolver probe failed"
  cat "$RUN_ROOT/install-macos-link.log"
fi
rm -f "$QUOTA_TEST_READLINK_F_MARKER"
if run_shell uninstall.sh "$H6" "$RUN_ROOT/uninstall-macos-link.log"; then
  ok "uninstall succeeds with readlink -f simulated as unsupported"
else
  bad "uninstall failed with readlink -f simulated as unsupported"
  cat "$RUN_ROOT/uninstall-macos-link.log"
fi
[ ! -e "$QUOTA_TEST_READLINK_F_MARKER" ] && ok "portable ownership check did not call readlink -f" || bad "uninstall attempted unsupported readlink -f"
if grep -q "Quota uninstall failed" "$RUN_ROOT/uninstall-macos-link.log"; then
  bad "readlink probe entered ERR rollback"
else
  ok "readlink probe did not enter ERR rollback"
fi
[ ! -e "$H6/plugins/quota" ] && ok "backend removed by uninstall" || bad "backend remained after uninstall"
[ ! -e "$H6/desktop-plugins/quota" ] && ok "widget removed by uninstall" || bad "widget remained after uninstall"

printf '\n%s\n' '=== T7: failed reinstall restores an existing profile symlink ==='
H7="$(new_home)"
OLD_TREE="$RUN_ROOT/previous-plugin-tree"
OLD_TARGET="../../../../previous-plugin-tree"
mkdir -p "$OLD_TREE" "$H7/plugins/quota" "$H7/desktop-plugins/quota" "$H7/profiles/alpha/plugins"
printf '%s\n' 'old backend' > "$H7/plugins/quota/sentinel"
printf '%s\n' 'old widget' > "$H7/desktop-plugins/quota/sentinel"
LINK="$H7/profiles/alpha/plugins/quota"
"$REAL_LN" -s "$OLD_TARGET" "$LINK"
export QUOTA_TEST_FAIL_LN_AT=2
export QUOTA_TEST_LN_COUNT_FILE="$RUN_ROOT/ln-call-count"
if run_shell install.sh "$H7" "$RUN_ROOT/install-fail-links.log"; then
  bad "install unexpectedly succeeded after injected ln failure"
else
  ok "install failed at injected ln call"
fi
unset QUOTA_TEST_FAIL_LN_AT QUOTA_TEST_LN_COUNT_FILE
if [ -L "$LINK" ] && [ -e "$LINK" ] && [ "$("$REAL_READLINK" "$LINK")" = "$OLD_TARGET" ]; then
  ok "pre-existing profile symlink target restored"
else
  bad "pre-existing profile symlink was not restored"
fi
[ "$(<"$H7/plugins/quota/sentinel")" = "old backend" ] && ok "old backend restored" || bad "old backend not restored"
[ "$(<"$H7/desktop-plugins/quota/sentinel")" = "old widget" ] && ok "old widget restored" || bad "old widget not restored"
[ ! -L "$H7/profiles/alpha/desktop-plugins/quota" ] && ok "link not created before injected failure" || bad "unexpected widget link after failure"

printf '\n%s\n' '=== T8: no staging directories remain ==='
for home in "$H1" "$H2" "$H3" "$H4" "$H5" "$H6" "$H7"; do
  for stage in "$home"/.quota-install.* "$home"/.quota-uninstall.*; do
    if [ -e "$stage" ]; then
      bad "stage directory left in $home"
    fi
  done
done
ok "no temporary install/uninstall stages left"
printf '\n=== %s passed, %s failed ===\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
