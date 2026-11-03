"""Antigravity (Google) quota fetcher -- CLI-backed.

Quota comes only from Antigravity's own CLI:

    agy -p /usage --output-format json

The CLI owns its login, credential store and network access.  This module never
reads Antigravity credential files or OS secret stores, never refreshes an
OAuth token, never calls Google's quota API, and never impersonates the
Antigravity client.  That direct path was retired for catalog rule 11 and
because the accepted ``antigravity-subscription-directsdk`` plugin already uses
exactly this CLI command.

Version 1.1.11 is the minimum: it is where print-mode slash commands started
emitting a structured payload under ``--output-format json`` instead of falling
through as literal prompt text.  The version gate runs *before* ``/usage`` and
is strict: an absent, unparseable, prerelease or older version is reported as
unavailable, never assumed to work.

The observed 1.4.12 payload carries the quota under
``command.data.groups``::

    {"status": "SUCCESS",
     "command": {"name": "usage",
                 "data": {"groups": [
                     {"name": "Gemini Models",
                      "buckets": [{"id": "gemini-weekly",
                                   "remaining_fraction": 1,
                                   "reset_time": "..."}]}]}}}

``remaining_fraction`` is 0..1 and *remaining*, so ``used = (1 - f) * 100``.
``reset_time`` is RFC3339 (``Z`` or a numeric offset).  The fifth bucket,
``gemini-argon-wtu-weekly``, is a separate Gemini 4 Argon weekly pool.  Argon is
conditional: it is shown only when the CLI actually returns that bucket, and it
is never derived or duplicated from the main Gemini pool.  An account without
it simply reports four windows.  A bucket with a reset but no fraction is shown
with an unknown percentage -- never as exhausted.  The CLI publishes no plan
name, so ``plan`` is always None rather than guessed.

This JSON shape is undocumented and unstable.  Only the structured command
payload is accepted; the human-readable ``response`` text is deliberately not
scraped.  Current releases put ``"SUCCESS"`` in ``status``; some older releases
use ``"OK"``.  Both spellings are matched exactly, and any other non-empty
status is a refusal.  A missing or non-string status is a malformed envelope,
never inferred as success.  If the shape changes, the provider reports
``agy-bad-json`` / ``no-data`` instead of inventing numbers.

Execution is bounded and noninteractive: no shell, no permission-skipping
flag, stdin is ``/dev/null``, stdout is capped, and one wall-clock deadline
covers the child, its process group and the output reader.  Each probe gets
``--log-file`` inside the call's private temporary directory, so per-call CLI
logs are removed with it instead of accumulating in the vendor's default log
directory.  The child runs in its own session/process group (so a timeout can
kill descendants that inherited stdout) and from a private empty directory (so
the CLI cannot discover project instruction files).  The environment is a
small allow-list plus validated standard proxy variables and
``AGY_CLI_DISABLE_AUTO_UPDATE=true`` so quota polling never mutates the CLI.
Proxy URLs with embedded userinfo are refused rather than forwarding a proxy
password; values with whitespace, control/format characters or malformed
percent escapes are refused too, and IPv6 proxy hosts must be bracketed.  A
proxy variable that is present but fails validation makes the provider
unavailable (``agy-unsafe-proxy``) instead of running agy without it: dropping
a configured proxy could send the request direct.  ``NO_PROXY`` is the
exception when no proxy URL is configured: spaces and tabs around its entries
are trimmed, and an otherwise unusable exclusion list is dropped because it
cannot make a request direct when there is no proxy.  Credential-bearing
variables (``GEMINI_API_KEY``, ``GOOGLE_APPLICATION_CREDENTIALS``,
``AGY_ADC_AUTH``, ...) are not forwarded, so agy keeps using the login it
already stored.

The reader is cancellable and closes its own pipe.  A descendant that escapes
the child's process group and keeps stdout open can therefore delay the call
only until the bounded grace period: the reader sees the cancellation event,
exits and closes the descriptor instead of leaking a thread and an fd per
refresh.  That escaped process cannot be killed portably; the plugin only
guarantees the child's own group.

The CLI is a vendor application and may write its own local state even for a
read-only ``/usage`` probe (for example ``conversation_summaries.db``,
``installation_id`` and bundled skill files under its config directory).  The
plugin controls only the per-call log file and does not read or alter that
state.  Windows process handling uses ``CREATE_NO_WINDOW`` plus ``taskkill
/T`` before a parent kill, but ``taskkill /T`` can enumerate descendants only
while the parent is alive: if agy exits normally after leaving a descendant
holding stdout, that descendant is already detached from the exited parent and
is not guaranteed to be cleaned up (the reader still stops after a bounded
grace and closes the plugin's pipe).  The Windows path is not covered by the
verified test matrix; Linux is verified.

Unavailable reasons: no ``agy`` -> ``agy-missing``; found but unusable
(relative, writable, untrusted owner, non-executable, unsupported extension) ->
``agy-unsafe-path``; a present but unusable proxy variable (including an
unusable ``NO_PROXY`` while a proxy is configured) -> ``agy-unsafe-proxy``;
unparseable or prerelease version ->
``agy-version-unknown``; older than 1.1.11 -> ``agy-too-old``; version or
usage exit/spawn failure -> ``agy-error``; missing/non-string ``status`` ->
``agy-bad-json``, an explicit non-``SUCCESS``/``OK`` status ->
``agy-refused``; malformed JSON/shape ->
``agy-invalid-json``/``agy-bad-json``; timeout -> ``timeout``; oversized
stdout -> ``agy-output-too-large``; no known buckets -> ``no-data``.

This provider is disabled by default; enable it explicitly with
``plugins.entries.quota.settings.antigravityEnabled``.
"""

from __future__ import annotations

import datetime
import ipaddress
import json
import os
import re
import select
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata
from typing import Any, Mapping, NamedTuple, Optional
from urllib.parse import unquote, urlsplit

from .base import (
    Deadline,
    QuotaResult,
    QuotaWindow,
    build_unavailable,
    opt_in_flag,
)
from .registry import register as _register

_PROVIDER_ID = "antigravity"

_MIN_AGY_VERSION = (1, 1, 11)
_VERSION_TIMEOUT_S = 5.0
_USAGE_TIMEOUT_S = 15.0
# Version + usage are serial. They share one deadline so the provider stays
# inside quota_cache.REFRESH_BUDGET_S (20s) even if both are slow.
_FETCH_BUDGET_S = 18.0
_MAX_OUTPUT_BYTES = 1024 * 1024
_MAX_VERSION_BYTES = 256
# One wall-clock allowance for killing, reaping and draining after a timeout or
# an exited child that left a descendant holding stdout.  It is included in the
# provider budget checks so a bounded call cannot turn into a second timeout.
_KILL_GRACE_S = 0.5
_DRAIN_GRACE_S = 0.2
# Final allowance for a cancelled reader to observe the event and close its fd.
# It only matters when a descendant escaped the child's process group.
_READER_EXIT_S = 0.25
_MAX_RESET_CHARS = 64

_USAGE_ARGS = ("-p", "/usage", "--output-format", "json")

# Current agy builds report ``"status": "SUCCESS"``; older releases used
# ``"OK"`` for the same successful command outcome.  Match those spellings
# exactly: case folding or prefix matching would infer success from a value the
# CLI did not actually confirm.
_SUCCESS_STATUSES = frozenset({"SUCCESS", "OK"})

# bucketId -> display label, in the order the CLI reports the groups.  The
# Argon bucket is weekly-only and is absent for accounts without that pool.
_BUCKETS = (
    ("gemini-weekly", "Gemini · Weekly"),
    ("gemini-5h", "Gemini · 5h"),
    ("3p-weekly", "Claude/GPT · Weekly"),
    ("3p-5h", "Claude/GPT · 5h"),
    ("gemini-argon-wtu-weekly", "Gemini 4 Argon · Weekly"),
)

_VERSION_RE = re.compile(
    # Final releases only: a prerelease such as ``1.1.11-rc1`` is not proof
    # that print-mode slash commands emit the structured payload.  Build
    # metadata (``+...``) does not change the version and is accepted.
    r"\A\s*v?(\d+)\.(\d+)\.(\d+)(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\s*\Z"
)

# RFC3339 ``Z`` (the documented shape) or a numeric offset.  The value is
# untrusted CLI output that is displayed, so it is also length-capped and
# calendar-validated; anything else is omitted rather than shown verbatim.
_RESET_RE = re.compile(
    r"\A(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})"
    r"(?:\.\d{1,9})?(Z|[+-]\d{2}:\d{2})\Z"
)

# Subprocess environment allow-list.  Everything here locates the user's home,
# runtime/keyring session or terminal; no credential-bearing variable is
# inherited.  In particular GEMINI_API_KEY, GOOGLE_APPLICATION_CREDENTIALS,
# AGY_ADC_AUTH and JETSKI_OAUTH_TOKEN are intentionally absent: agy must use
# the login it already owns, not an ambient secret from the plugin host.
_ENV_ALLOW = (
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "PATH",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "TZ",
    "TERM",
    "NO_COLOR",
    "COLORTERM",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    # Windows process/runtime locations.
    "USERPROFILE",
    "USERNAME",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "HOMEDRIVE",
    "HOMEPATH",
)

# Standard proxy variables only.  These carry no vendor credential, but a
# proxy URL can embed a user:password pair; such values are refused so a proxy
# secret is not silently placed in the child environment.  Upper and lower
# case forms are both honoured because Go-based CLIs and CGI contexts differ.
_PROXY_ENV = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)
_PROXY_SCHEMES = frozenset(
    {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}
)
_MAX_PROXY_CHARS = 4096
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_PORT_RE = re.compile(r"\A[0-9]{1,5}\Z")
_DOMAIN_LABEL_RE = re.compile(
    r"\A[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?\Z"
)
_ZONE_RE = re.compile(r"\A[A-Za-z0-9_.-]{1,64}\Z")


class _CliRun(NamedTuple):
    """One bounded CLI invocation.  ``stdout`` is capped by ``max_bytes``."""

    stdout: bytes
    returncode: Optional[int]
    timed_out: bool = False
    output_too_large: bool = False
    launch_error: bool = False


class _BadPayload(Exception):
    """The CLI answered, but not with the documented quota shape."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _unsafe_proxy_text(value: str) -> bool:
    """True for text the CLI would ignore or reinterpret.

    Whitespace and control/format characters anywhere in a value can make a
    proxy setting parse differently in the child (or be ignored, sending the
    request direct).  A ``%`` must introduce exactly two hex digits; an
    invalid escape is likewise not a value we can predict the child will use.
    """
    for ch in value:
        if ch.isspace() or unicodedata.category(ch).startswith("C"):
            return True
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        if (
            index + 2 >= len(value)
            or value[index + 1] not in _HEX_DIGITS
            or value[index + 2] not in _HEX_DIGITS
        ):
            return True
        index += 3
    return False


def _valid_port_text(value: str) -> bool:
    if not _PORT_RE.match(value):
        return False
    return 1 <= int(value) <= 65535


def _valid_ipv4(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return True


def _valid_ipv6(value: str, *, allow_zone: bool = True) -> bool:
    address, separator, zone = value.partition("%")
    if separator and (not allow_zone or not _ZONE_RE.match(zone)):
        return False
    try:
        ipaddress.IPv6Address(address)
    except ValueError:
        return False
    return True


def _valid_domain(value: str) -> bool:
    name = value[:-1] if value.endswith(".") else value
    if not name or len(name) > 253 or "%" in name:
        return False
    return all(_DOMAIN_LABEL_RE.match(label) for label in name.split("."))


def _decoded_host(value: str) -> Optional[str]:
    """Percent-decode a host, rejecting bytes that are not valid UTF-8."""
    try:
        return unquote(value, encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        return None


def _valid_no_proxy_entry(entry: str) -> bool:
    """One NO_PROXY token: wildcard, host[:port], IP, or CIDR."""
    if entry.startswith("["):
        end = entry.find("]")
        if end < 0:
            return False
        host, rest = entry[1:end], entry[end + 1:]
        if "%" in host or not _valid_ipv6(host, allow_zone=False):
            return False
        if rest == "":
            return True
        return rest.startswith(":") and _valid_port_text(rest[1:])
    if "/" in entry:
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            return False
        return True
    if entry.count(":") >= 2:
        # A bare IPv6 literal.  A port on one must be bracketed, as in URLs.
        return _valid_ipv6(entry, allow_zone=False)
    host, separator, port = entry.partition(":")
    if separator and not _valid_port_text(port):
        return False
    if not host:
        return False
    if host.startswith("*."):
        host = host[2:]
    elif host.startswith("."):
        host = host[1:]
    return _valid_ipv4(host) or _valid_domain(host)


def _valid_no_proxy(value: str) -> Optional[str]:
    """Normalized ``NO_PROXY`` list, or None when it is unsafe to forward.

    Commas separate entries and spaces/tabs around an entry are conventional
    padding: ``localhost, 127.0.0.1`` is the same list as
    ``localhost,127.0.0.1``.  Trimming only those two characters keeps every
    other whitespace (newlines, vertical tabs, non-breaking spaces) visible to
    the entry validation and control-character check.  The trimmed list is
    returned so the child receives exactly what was validated.
    """
    entries = []
    for raw in value.split(","):
        entry = raw.strip(" \t")
        if any(unicodedata.category(ch).startswith("C") for ch in entry):
            return None
        if entry in ("", "*"):
            entries.append(entry)
            continue
        if not _valid_no_proxy_entry(entry):
            return None
        entries.append(entry)
    return ",".join(entries)


def _proxy_url_configured(environ: Mapping[str, str]) -> bool:
    """True when a proxy destination (not ``NO_PROXY``) is present and non-empty.

    ``NO_PROXY`` only lists destinations that bypass a proxy; it cannot select
    a proxy or make a request direct.  With no proxy URL in the forwarded
    environment an invalid ``NO_PROXY`` is therefore irrelevant, while with one
    it changes routing and must fail closed.
    """
    for key in _PROXY_ENV:
        if key.lower() == "no_proxy":
            continue
        value = environ.get(key)
        if isinstance(value, str) and value:
            return True
    return False


def _proxy_value(name: str, value: str) -> Optional[str]:
    """A syntactically safe proxy setting, or None to withhold it.

    ``NO_PROXY`` is a host-pattern list, not a URL, and spaces or tabs around
    its comma-separated entries are accepted and trimmed.  The other variables
    must be http(s)/socks URLs with a bracketed IPv6 literal or a DNS name/IPv4
    host and no embedded userinfo: forwarding a ``user:password@proxy`` URL
    would hand a proxy credential to the CLI, which is outside the provider's
    "no ambient secrets" rule.  Whitespace (other than that ``NO_PROXY``
    padding), control/format characters and invalid percent escapes are
    refused because the CLI may ignore or reinterpret them; that could turn a
    configured proxy into a direct connection.
    """
    if not isinstance(value, str) or not value or len(value) > _MAX_PROXY_CHARS:
        return None
    if name.lower() == "no_proxy":
        # A host-pattern list, not a URL: spaces/tabs around entries are
        # allowed and normalized by _valid_no_proxy.
        return _valid_no_proxy(value)
    if _unsafe_proxy_text(value):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme.lower() not in _PROXY_SCHEMES:
        return None
    # A proxy URL is scheme://host[:port]; query strings, fragments, userinfo
    # and paths beyond a single slash are not proxy settings.  Reject the raw
    # delimiters too so an empty "?" or "#" cannot hide anything.
    if "?" in value or "#" in value or "@" in parts.netloc:
        return None
    netloc = parts.netloc
    if netloc.startswith("["):
        end = netloc.find("]")
        if end < 0:
            return None
        host_text, rest = netloc[1:end], netloc[end + 1:]
        if rest and (not rest.startswith(":") or not _valid_port_text(rest[1:])):
            return None
        host = _decoded_host(host_text)
        if host is None or not _valid_ipv6(host):
            return None
    else:
        host_text, separator, port_text = netloc.rpartition(":")
        if not separator:
            host_text, port_text = netloc, None
        elif ":" in host_text:
            return None
        if port_text is not None and not _valid_port_text(port_text):
            return None
        # A DNS/IPv4 host must not use percent escapes: unlike an IPv6 zone,
        # there is no valid escaped form here, and the child may not decode it.
        if "%" in host_text:
            return None
        host = _decoded_host(host_text)
        if host is None or not (_valid_ipv4(host) or _valid_domain(host)):
            return None
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return None
    if parts.hostname is None:
        return None
    return value


def _proxy_problem(environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Name of a present proxy variable that cannot be forwarded safely.

    The provider must fail closed when a configured proxy is rejected: running
    agy with that variable dropped could send traffic directly instead of
    through the proxy.  An unset or empty variable is not a problem.

    ``NO_PROXY`` is different only when no proxy URL is configured: it cannot
    select a proxy or make a request direct, so an unusable exclusion list is
    withheld without disabling the provider.  Once any proxy URL is present,
    an unusable ``NO_PROXY`` changes routing and fails closed like the rest.
    """
    source = os.environ if environ is None else environ
    for key in _PROXY_ENV:
        if key not in source:
            continue
        value = source[key]
        if value is None or value == "":
            continue
        if not isinstance(value, str) or _proxy_value(key, value) is None:
            if key.lower() == "no_proxy" and not _proxy_url_configured(source):
                continue
            return key
    return None


def _cli_env() -> dict[str, str]:
    """Environment for agy: runtime locations, proxies, auto-update off.

    ``_ENV_ALLOW`` locates the user's profile and session (so agy can use its
    own stored login); standard proxy variables are added only after
    ``_proxy_value`` accepts them.  No credential-bearing variable is copied.

    Dropping an unusable proxy here is safe only because the provider rejects
    such an environment with ``_proxy_problem`` before it executes agy.
    """
    env = {key: os.environ[key] for key in _ENV_ALLOW if key in os.environ}
    for key in _PROXY_ENV:
        if key not in os.environ:
            continue
        value = _proxy_value(key, os.environ[key])
        if value is not None:
            env[key] = value
    # Only the literal "true" disables the updater in current agy builds.
    env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
    env["NO_COLOR"] = "1"
    return env


def _trusted_owner(st: os.stat_result) -> bool:
    """Root or the invoking user; Windows has no POSIX ownership model."""
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        return True
    return st.st_uid in (0, os.geteuid())


def _safe_agy_path(found: str) -> Optional[str]:
    """Resolve ``found`` only when neither it nor its parents are hijackable.

    ``shutil.which`` can return a relative hit when ``PATH`` contains ``.`` or
    an empty entry, and an executable in a group/world-writable directory (or a
    writable executable itself) can be replaced between lookup and spawn.  The
    provider fails closed instead of executing that path.

    The ownership and mode checks are POSIX-only.  Windows has no POSIX
    uid/mode model, so there the check is limited to an absolute, existing
    regular file with a ``.exe``/``.com`` extension.  This is a best-effort
    path check, not a signature check and not a defense against a same-user
    race.
    """
    if not found or not os.path.isabs(found):
        return None
    try:
        path = os.path.realpath(found)
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    if not stat.S_ISREG(st.st_mode) or not os.access(path, os.X_OK):
        return None
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        # shutil.which honours PATHEXT; shell=False cannot safely run a script.
        return path if os.path.splitext(path)[1].lower() in {".exe", ".com"} else None
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID):
        return None
    if not _trusted_owner(st):
        return None
    parent = os.path.dirname(path)
    while True:
        try:
            dst = os.stat(parent)
        except (OSError, ValueError):
            return None
        if not stat.S_ISDIR(dst.st_mode):
            return None
        if dst.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            # A sticky directory such as /tmp protects existing entries from
            # other users; the executable and every deeper directory are still
            # checked separately.
            if not dst.st_mode & stat.S_ISVTX:
                return None
        if not _trusted_owner(dst):
            return None
        if parent == os.path.dirname(parent):
            break
        parent = os.path.dirname(parent)
    return path


def _agy_lookup() -> tuple[Optional[str], Optional[str]]:
    """``(path, unavailable_reason)`` for the installed ``agy``.

    "not installed" and "installed but unsafe to execute" are different
    facts: the first calls for installing/signing in, the second for fixing a
    PATH or permission problem.  They must not share ``agy-missing``.
    """
    found = shutil.which("agy")
    if not found:
        return None, "agy-missing"
    path = _safe_agy_path(found)
    if path is None:
        return None, "agy-unsafe-path"
    return path, None


def _agy_path() -> Optional[str]:
    """Absolute, safely-located executable ``agy``, or None when unusable."""
    return _agy_lookup()[0]


def _spawn_kwargs() -> dict[str, Any]:
    """Own session/process group, and no console window on Windows."""
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        # A console child would flash a window during every background sweep.
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        return {"creationflags": flags}
    return {"start_new_session": True}


def _kill_tree(proc: "subprocess.Popen[Any]") -> None:
    """Best-effort kill of the child and every descendant in its tree.

    ``Popen.kill`` reaches only the direct child.  A grandchild that inherited
    stdout would keep the pipe open, so the reader would never see EOF.  The
    child is spawned in a new session (POSIX) or process group (Windows), which
    makes the group kill cover those descendants.

    Windows needs ``taskkill /T`` *before* the parent is killed: once the
    parent exits, taskkill can no longer enumerate its children.  The parent
    kill is therefore a fallback, not the first step.
    """
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        taskkill = os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"),
            "System32",
            "taskkill.exe",
        )
        try:
            subprocess.run(
                [taskkill, "/PID", str(proc.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                timeout=1.0,
                check=False,
                env=_cli_env(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            )
        except (OSError, subprocess.SubprocessError):
            pass
        # Fallback if taskkill is unavailable or did not reach the parent.
        try:
            proc.kill()
        except OSError:
            pass
        return

    # ``start_new_session`` makes the process group ID equal to the child PID.
    # Never signal our own group if that invariant was somehow lost.
    if proc.pid != os.getpgrp():
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            pass
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _wait_until(proc: "subprocess.Popen[Any]", deadline: float) -> Optional[int]:
    """Wait without ever exceeding ``deadline``; None if it passed unsignalled."""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return proc.poll()
        try:
            return proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            continue


def _command(
    path: str,
    args: tuple[str, ...],
    workdir: str,
    log_name: Optional[str],
) -> list[str]:
    """argv for one probe, with its log confined to the private workdir."""
    if log_name is None:
        return [path, *args]
    return [path, "--log-file", os.path.join(workdir, log_name), *args]


def _windows_read(fd: int) -> Optional[bytes]:
    """One non-blocking read from a Windows pipe.

    Returns ``b""`` at EOF/broken pipe and ``None`` when no bytes are ready.
    ``PeekNamedPipe`` is the portable way to avoid blocking a reader thread on
    a pipe that an escaped descendant may hold open forever.
    """
    import ctypes
    import msvcrt
    from ctypes import wintypes

    handle = ctypes.c_void_p(getattr(msvcrt, "get_osfhandle")(fd))
    available = wintypes.DWORD(0)
    ok = getattr(ctypes, "windll").kernel32.PeekNamedPipe(
        handle, None, 0, None, ctypes.byref(available), None)
    if not ok:
        return b""
    if available.value == 0:
        return None
    try:
        return os.read(fd, min(int(available.value), 65536))
    except OSError:
        return b""


def _read_chunk(stream: Any, cancel: threading.Event) -> Optional[bytes]:
    """Read one chunk with a bounded wait; ``None`` means cancelled.

    The pipe is read raw (``os.read``) so closing the buffered stream from the
    reader cannot contend on a lock held by a blocked ``read``.  POSIX uses
    ``select`` with a short timeout; Windows peeks the named pipe.  Either way
    the loop re-checks ``cancel`` so a descendant that escaped the child's
    group cannot pin a reader thread forever.
    """
    fd = stream.fileno()
    while not cancel.is_set():
        if os.name == "nt":  # pragma: no cover - exercised on Windows only
            chunk = _windows_read(fd)
            if chunk is None:
                time.sleep(0.02)
                continue
            return chunk
        try:
            ready, _, _ = select.select([fd], [], [], 0.05)
        except InterruptedError:
            continue
        except (OSError, ValueError):
            return b""
        if not ready:
            continue
        try:
            return os.read(fd, 65536)
        except InterruptedError:
            continue
        except OSError:
            return b""
    return None


def _run_cli(
    path: str,
    args: tuple[str, ...],
    *,
    timeout: float,
    max_bytes: int = _MAX_OUTPUT_BYTES,
    log_name: Optional[str] = None,
) -> _CliRun:
    """Run agy without a shell, bounded in wall time and stdout bytes.

    stderr is discarded: it can carry vendor diagnostics and is never shown or
    cached.  A reader thread keeps only the first ``max_bytes`` of stdout and
    kills the process group as soon as it exceeds the cap, so a runaway CLI
    cannot grow memory without bound.  A zero/negative timeout returns
    immediately.

    The reader owns ``proc.stdout`` and closes it when it reaches EOF or is
    cancelled.  The waiting thread never closes that pipe while the reader may
    be reading (closing a buffered stream from another thread waits on the
    reader's lock and can deadlock).  If a descendant inherited stdout and
    escaped the child's group, the reader is cancelled after the drain grace
    and closes its own descriptor, so neither a thread nor an fd leaks per
    refresh.  ``log_name`` puts the CLI's per-call log inside ``workdir``.
    """
    if timeout <= 0:
        return _CliRun(b"", None, timed_out=True)

    try:
        workdir = tempfile.mkdtemp(prefix="hermes-quota-agy-")
    except OSError:
        return _CliRun(b"", None, launch_error=True)

    try:
        try:
            proc = subprocess.Popen(
                _command(path, args, workdir, log_name),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                shell=False,
                close_fds=True,
                cwd=workdir,
                env=_cli_env(),
                **_spawn_kwargs(),
            )
        except OSError:
            return _CliRun(b"", None, launch_error=True)

        captured = bytearray()
        overflow = [False]
        capture_lock = threading.Lock()
        cancel = threading.Event()

        def _drain() -> None:
            stream = proc.stdout
            if stream is None:  # pragma: no cover - PIPE guarantees this
                return
            try:
                while True:
                    chunk = _read_chunk(stream, cancel)
                    if chunk is None:
                        # Cancelled: an escaped descendant may still hold the
                        # pipe.  Release our end instead of waiting forever.
                        return
                    if not chunk:
                        return
                    kill_now = False
                    with capture_lock:
                        room = max_bytes - len(captured)
                        if room > 0:
                            captured.extend(chunk[:room])
                        if len(chunk) > room and not overflow[0]:
                            overflow[0] = True
                            kill_now = True
                    if kill_now:
                        # Kill the whole group: the child may have spawned a
                        # helper that also writes to this pipe.  Do not hold
                        # the capture lock while a Windows taskkill runs.
                        _kill_tree(proc)
                    # Keep draining so a killed child cannot block on a full
                    # pipe while the main thread waits for it.
            except (OSError, ValueError):
                return
            finally:
                # Closing here (never from the waiter) cannot deadlock on the
                # buffered reader's own lock.
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

        reader = threading.Thread(target=_drain, daemon=True, name="agy-stdout")
        reader.start()

        deadline = time.monotonic() + timeout + _KILL_GRACE_S
        timed_out = False
        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            # The output is discarded on timeout; stop the reader before the
            # kill so it cannot block on a pipe held by a descendant.
            cancel.set()
            _kill_tree(proc)
            returncode = _wait_until(proc, deadline)
        else:
            # The direct child is done.  It must not leave a helper behind:
            # kill anything still in the group it created for this call.
            _kill_tree(proc)

        if reader.is_alive():
            reader.join(timeout=max(
                0.0, min(_DRAIN_GRACE_S, deadline - time.monotonic())))
        if reader.is_alive():
            # The direct child is gone or killed but something still holds the
            # stdout pipe.  It may have escaped the group, so no group signal
            # can reach it; cancel the reader and let it close its own fd.
            cancel.set()
            _kill_tree(proc)
            reader.join(timeout=max(
                0.0, min(_KILL_GRACE_S, deadline - time.monotonic())))
        if reader.is_alive():
            # Final cleanup allowance, not tied to the child deadline: the
            # reader only needs one poll interval to observe cancellation.
            reader.join(timeout=_READER_EXIT_S)

        with capture_lock:
            stdout = bytes(captured[:max_bytes])
            too_large = overflow[0]
        return _CliRun(stdout, returncode, timed_out, too_large, False)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _parse_version(stdout: bytes) -> Optional[tuple[int, int, int]]:
    """Strict semver tuple from ``agy --version``, or None.

    The real output is a bare ``1.4.12`` line.  Prose or a partially numeric
    string is not accepted: the gate must be able to prove the minimum.
    """
    try:
        text = stdout.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    match = _VERSION_RE.match(text)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _version_reason(
    run: _CliRun,
) -> tuple[Optional[tuple[int, int, int]], Optional[str]]:
    """(version, unavailable_reason) for the version probe."""
    if run.launch_error:
        # The executable was found and passed the path check, but the OS
        # refused to start it.  That is an execution error, not "not installed".
        return None, "agy-error"
    if run.output_too_large:
        return None, "agy-version-unknown"
    if run.timed_out:
        return None, "timeout"
    if run.returncode != 0:
        return None, "agy-error"
    version = _parse_version(run.stdout)
    if version is None:
        return None, "agy-version-unknown"
    return version, None


def _buckets(groups: Any) -> dict[str, dict[str, Any]]:
    """First bucket per id across all groups; non-object entries are skipped."""
    found: dict[str, dict[str, Any]] = {}
    if not isinstance(groups, list):
        return found
    for group in groups:
        if not isinstance(group, dict):
            continue
        entries = group.get("buckets")
        if not isinstance(entries, list):
            continue
        for bucket in entries:
            if not isinstance(bucket, dict):
                continue
            bucket_id = bucket.get("id")
            if not isinstance(bucket_id, str) or not bucket_id:
                bucket_id = bucket.get("bucketId")
            if isinstance(bucket_id, str) and bucket_id and bucket_id not in found:
                found[bucket_id] = bucket
    return found


def _reset_time(value: Any) -> Optional[str]:
    """A display-safe RFC3339 timestamp, or None.

    ``reset_time`` is untrusted CLI output rendered in the widget.  It must be
    a real calendar instant, no longer than ``_MAX_RESET_CHARS``, and either
    ``Z`` or a valid numeric offset.  Malformed values are omitted; they are
    never passed through to the display.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > _MAX_RESET_CHARS:
        return None
    match = _RESET_RE.match(text)
    if match is None:
        return None
    try:
        datetime.datetime(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            int(match.group(4)),
            int(match.group(5)),
            int(match.group(6)),
        )
    except ValueError:
        return None
    zone = match.group(7)
    if zone != "Z":
        hours, minutes = int(zone[1:3]), int(zone[4:6])
        if hours > 23 or minutes > 59:
            return None
    return text


def _window(bucket: dict[str, Any], label: str) -> Optional[QuotaWindow]:
    """One window, with an honest unknown when the fraction is absent.

    A fraction outside 0..1 is not rescaled.  If the bucket still carries a
    valid reset time it is shown with ``used_percent=None``; with nothing to
    report at all the bucket is dropped.
    """
    reset = _reset_time(bucket.get("reset_time"))
    if reset is None:
        reset = _reset_time(bucket.get("resetTime"))

    fraction = bucket.get("remaining_fraction")
    if fraction is None:
        fraction = bucket.get("remainingFraction")
    if not isinstance(fraction, (int, float)) or isinstance(fraction, bool):
        if reset is None:
            return None
        return QuotaWindow(label=label, used_percent=None, reset_at=reset)
    if not 0.0 <= fraction <= 1.0:
        # A different unit, not a percentage.  Do not rescale and do not turn
        # it into an unknown meter: this bucket is unusable.
        return None
    return QuotaWindow(
        label=label,
        used_percent=round((1.0 - float(fraction)) * 100.0, 2),
        reset_at=reset,
    )


def _usage_groups(stdout: bytes) -> list[dict[str, Any]]:
    """Validate the CLI's structured envelope and return its groups.

    Only ``command.data.groups`` is read.  The top-level ``response`` field is
    human-readable TSV for terminals and is deliberately ignored: scraping it
    would turn a formatting change into silently wrong quota.
    """
    try:
        decoded = stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _BadPayload("agy-invalid-json") from exc
    try:
        payload = json.loads(decoded)
    except ValueError as exc:
        raise _BadPayload("agy-invalid-json") from exc
    if not isinstance(payload, dict):
        raise _BadPayload("agy-bad-json")
    status = payload.get("status")
    if not isinstance(status, str) or not status:
        # A missing/null/non-string status is a malformed envelope, not a
        # refusal: the CLI did not say which command outcome this is.
        raise _BadPayload("agy-bad-json")
    if status not in _SUCCESS_STATUSES:
        raise _BadPayload("agy-refused")
    command = payload.get("command")
    if not isinstance(command, dict) or command.get("name") != "usage":
        raise _BadPayload("agy-bad-json")
    data = command.get("data")
    if not isinstance(data, dict):
        raise _BadPayload("agy-bad-json")
    groups = data.get("groups")
    if not isinstance(groups, list):
        raise _BadPayload("agy-bad-json")
    return groups


def _windows(groups: list[dict[str, Any]]) -> list[QuotaWindow]:
    """Known windows in display order, with unknown fractions preserved."""
    found = _buckets(groups)
    windows: list[QuotaWindow] = []
    for bucket_id, label in _BUCKETS:
        bucket = found.get(bucket_id)
        if bucket is None:
            continue
        window = _window(bucket, label)
        if window is not None:
            windows.append(window)
    return windows


def fetch_antigravity_quota() -> QuotaResult:
    """Read quota through agy, or return a truthful unavailable record."""
    path, path_reason = _agy_lookup()
    if path is None:
        reason = path_reason or "agy-missing"
        if reason == "agy-missing":
            details = [
                "Install Antigravity CLI 1.1.11 or newer and sign in with `agy`."
            ]
        else:
            details = [
                "`agy` was found but is not a trusted executable: use an "
                "absolute .exe/.com file on Windows, or an absolute POSIX "
                "executable with trusted ownership and no writable parent "
                "directory.",
            ]
        return build_unavailable(_PROVIDER_ID, reason, details)

    proxy_var = _proxy_problem()
    if proxy_var is not None:
        return build_unavailable(
            _PROVIDER_ID,
            "agy-unsafe-proxy",
            [
                f"{proxy_var} is set but cannot be forwarded safely; "
                "refusing to run agy without the configured proxy. Unset it "
                "or replace it with a valid value (proxy URLs must not embed "
                "userinfo and must not contain whitespace, control characters "
                "or malformed percent escapes; NO_PROXY entries may be padded "
                "with spaces or tabs)."
            ],
        )

    deadline = Deadline(_FETCH_BUDGET_S)
    version_run = _run_cli(
        path, ("--version",),
        timeout=deadline.slice(_VERSION_TIMEOUT_S),
        max_bytes=_MAX_VERSION_BYTES,
        log_name="agy-version.log",
    )
    version, reason = _version_reason(version_run)
    if reason is not None:
        return build_unavailable(_PROVIDER_ID, reason)

    assert version is not None
    if version < _MIN_AGY_VERSION:
        shown = ".".join(str(part) for part in version)
        return build_unavailable(
            _PROVIDER_ID, "agy-too-old",
            [f"agy {shown} is older than 1.1.11; run `agy update`."],
        )

    if deadline.expired():
        return build_unavailable(_PROVIDER_ID, "timeout")

    usage_run = _run_cli(
        path, _USAGE_ARGS, timeout=deadline.slice(_USAGE_TIMEOUT_S),
        log_name="agy-usage.log",
    )
    if usage_run.launch_error:
        return build_unavailable(_PROVIDER_ID, "agy-error")
    if usage_run.output_too_large:
        return build_unavailable(_PROVIDER_ID, "agy-output-too-large")
    if usage_run.timed_out:
        return build_unavailable(_PROVIDER_ID, "timeout")
    if usage_run.returncode != 0:
        return build_unavailable(_PROVIDER_ID, "agy-error")

    try:
        groups = _usage_groups(usage_run.stdout)
    except _BadPayload as exc:
        return build_unavailable(_PROVIDER_ID, exc.reason)

    windows = _windows(groups)
    if not windows:
        return build_unavailable(_PROVIDER_ID, "no-data")
    return QuotaResult(label=_PROVIDER_ID, windows=windows, plan=None)


# Antigravity is opt-in: it runs another application's CLI and can trigger that
# application's own network/auth work.  Enable with:
#   hermes config set plugins.entries.quota.settings.antigravityEnabled true


def _antigravity_enabled() -> bool:
    value = os.environ.get("HERMES_QUOTA_ANTIGRAVITY_ENABLED", "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    try:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly() or {}
        plugins = config.get("plugins") if isinstance(config, dict) else None
        entries = plugins.get("entries") if isinstance(plugins, dict) else None
        entry = entries.get("quota") if isinstance(entries, dict) else None
        if isinstance(entry, dict):
            settings = entry.get("settings")
            if isinstance(settings, dict) and "antigravityEnabled" in settings:
                return opt_in_flag(settings.get("antigravityEnabled"))
    except Exception:
        pass
    return False


def _fetch_antigravity_optin() -> QuotaResult:
    if not _antigravity_enabled():
        return build_unavailable(_PROVIDER_ID, "opt-in-disabled")
    return fetch_antigravity_quota()


_register(_PROVIDER_ID)(_fetch_antigravity_optin)
