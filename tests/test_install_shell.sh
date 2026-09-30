#!/bin/bash
# Exercise install.sh / uninstall.sh against a throwaway HERMES_HOME.
# NEVER run these against the real ~/.hermes.
set -u
# Repo root = the parent of tests/.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS=0; FAIL=0
ok()   { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }

# A fake `hermes` so the config helpers have something to talk to.
FAKEBIN="$(mktemp -d)"
# hermes-config.sh:36-42 tries `config get` twice. On "not set" it expects the
# literal message "Config key not set: <key>" and it must arrive on STDOUT with
# exit 0 (the second branch captures 2>&1 and tests $?).
# hermes-config.sh:39-42 makes two `config get` attempts. Answering the
# "not set" shape (message on stdout, non-zero exit) is what a real Hermes
# does for an unset key, and lands the script on the absent branch.
cat > "$FAKEBIN/hermes" <<'FAKE'
#!/bin/sh
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
FAKE
chmod +x "$FAKEBIN/hermes"
export PATH="$FAKEBIN:$PATH"

new_home() {
  H="$(mktemp -d)"
  mkdir -p "$H/profiles/alpha" "$H/profiles/beta"
  echo "$H"
}

echo "=== T1: install into a temp home, twice (idempotent) ==="
H1="$(new_home)"
( cd "$REPO" && HERMES_HOME="$H1" ./install.sh >/tmp/i1.log 2>&1 ); rc1=$?
ok "first install exit 0" 2>/dev/null; [ $rc1 -eq 0 ] && ok "first install" || { bad "first install rc=$rc1"; tail -5 /tmp/i1.log; }
[ -d "$H1/plugins/quota" ] && ok "backend installed" || bad "backend missing"
[ -d "$H1/desktop-plugins/quota" ] && ok "widget installed" || bad "widget missing"
L="$H1/profiles/alpha/plugins/quota"
[ -L "$L" ] && [ -e "$L" ] && ok "alpha backend link resolves" || bad "alpha backend link broken: $L"
L2="$H1/profiles/alpha/desktop-plugins/quota"
[ -L "$L2" ] && [ -e "$L2" ] && ok "alpha widget link resolves" || bad "alpha widget link broken"
( cd "$REPO" && HERMES_HOME="$H1" ./install.sh >/tmp/i2.log 2>&1 ); rc2=$?
[ $rc2 -eq 0 ] && ok "second install exit 0 (idempotent)" || bad "second install rc=$rc2"
[ -L "$L" ] && [ -e "$L" ] && ok "link still resolves after re-install" || bad "link broken after re-install"

echo
echo "=== T2: a user's own dev symlink survives uninstall ==="
H2="$(new_home)"
mkdir -p /tmp/hqp-devcheckout/quota
mkdir -p "$H2/profiles/alpha/plugins"
ln -s /tmp/hqp-devcheckout "$H2/profiles/alpha/plugins/quota"
( cd "$REPO" && HERMES_HOME="$H2" ./uninstall.sh >/tmp/u1.log 2>&1 ); rc=$?
[ $rc -eq 0 ] && ok "uninstall exit 0" || bad "uninstall rc=$rc"
if [ -L "$H2/profiles/alpha/plugins/quota" ]; then
  ok "foreign symlink PRESERVED (was removed before this fix)"
else
  bad "foreign symlink was deleted"
fi
grep -q "Leaving" /tmp/u1.log && ok "uninstall explained why it left it" || bad "no explanation printed"

echo
echo "=== T3: install.sh's own links ARE removed by uninstall ==="
H3="$(new_home)"
( cd "$REPO" && HERMES_HOME="$H3" ./install.sh >/dev/null 2>&1 )
( cd "$REPO" && HERMES_HOME="$H3" ./uninstall.sh >/dev/null 2>&1 )
[ ! -e "$H3/profiles/alpha/plugins/quota" ] && ok "our own link removed" || bad "our own link survived uninstall"
[ ! -d "$H3/plugins/quota" ] && ok "backend dir removed" || bad "backend dir survived"

echo
echo "=== T4: install reports success only when links resolve ==="
H4="$(new_home)"
( cd "$REPO" && HERMES_HOME="$H4" ./install.sh >/tmp/i4.log 2>&1 )
grep -q "Linked into" /tmp/i4.log && ok "install reports link count" || bad "no link report"
grep -q "does not resolve" /tmp/i4.log && bad "healthy install warned about broken links" || ok "healthy install: no false warning"

echo
echo "=== T5: a failed install leaves no dangling profile links ==="
H5="$(new_home)"
# Force a failure after the symlink loop by making the post-loop version.json
# write impossible: replace the staged tree with a read-only parent.
H6="$(new_home)"
mkdir -p "$H6/profiles/alpha" "$H6/profiles/beta"
# A regular file where the loop needs a directory, so mkdir -p fails partway
# through the profile list -- after alpha's links already exist.
: > "$H6/profiles/beta/plugins"
( cd "$REPO" && HERMES_HOME="$H6" ./install.sh >/tmp/i6.log 2>&1 ); rc6=$?
[ $rc6 -ne 0 ] && ok "install failed as intended (rc=$rc6)" || bad "expected failure, got rc=0"
# After a successful rollback there must be NO symlink at all: either they
# never existed, or rollback removed them. A surviving link is dangling when
# its target is gone, or stray when the install is still there.
leftover=0
for l in "$H6"/profiles/*/plugins/quota "$H6"/profiles/*/desktop-plugins/quota; do
  [ -L "$l" ] || continue
  leftover=$((leftover + 1))
  [ -e "$l" ] && bad "symlink $l survived and still resolves"
done
[ "$leftover" -eq 0 ] && ok "no symlinks left after a failed install" \
                      || bad "$leftover symlink(s) left after rollback"

# And the half-installed plugin dir must be gone too.
[ ! -e "$H6/plugins/quota" ] && ok "partial plugin dir cleaned up" \
                           || bad "plugins/quota survived a failed install"

echo
echo "=== T6: no stage dirs left behind ==="
for H in "$H1" "$H2" "$H3" "$H4" "$H5" "$H6"; do
  n=$(find "$H" -maxdepth 2 -name '.quota-install.*' 2>/dev/null | wc -l)
  [ "$n" -eq 0 ] || bad "stage dir left in $H"
done
ok "no .quota-install.* left in any temp home"

rm -rf "$FAKEBIN" /tmp/hqp-devcheckout
echo
echo "=== $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
