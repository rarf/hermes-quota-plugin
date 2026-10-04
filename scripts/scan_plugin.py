#!/usr/bin/env python3
"""Run Hermes' own plugin scanner against this repository.

The scanner is not vendored here: it lives in NousResearch/hermes-agent and is
the exact code that runs when someone installs or updates this plugin. That is
the point -- a critical finding here is not a style opinion, it stops the plugin
installing at all.

Two rules make the gate faithful rather than decorative:

* **Pinned ref, never ``main``.** The scanner's own severity table moves: on
  2026-10-04 ``main`` demoted the Google installed-app client literals to
  ``high`` (``is_google_installed_app_secret``) while every stable build still
  called them ``critical``. A gate that follows ``main`` therefore goes green
  on a tree that stable Hermes refuses to install. ``SCANNER_REF`` below is the
  release tag this plugin supports; bump it deliberately, in its own commit.
* **Block the class, not just the verdict.** Install is refused on a
  ``dangerous`` verdict, which any ``critical`` produces -- but the same class
  can arrive as ``high`` once upstream demotes a pattern. ``credential_exposure``
  at ``critical`` or ``high`` fails this gate either way, so a scanner-side
  demotion cannot silently disarm it.

``--self-test`` proves the gate is still sensitive: it plants a secret-shaped
literal in a temp tree and requires the scanner to report it as blocking. CI
runs it before every scan, so a scanner bump that stops seeing the class fails
loudly instead of passing quietly.

By default this fetches the pinned scanner into a cache directory with a sparse
checkout of ``tools/`` only. Pass --scanner to reuse a checkout you already have.

Exit codes:
    0  nothing blocking
    1  a blocking finding (community install/update would be refused), or the
       self-test failed because the scanner no longer reports the known case
    2  the scanner could not be run or fetched

Requires Python 3.11+ -- that is hermes-agent's floor, not this plugin's.
This plugin still supports 3.9; only the scanner needs the newer interpreter.

    python3 scripts/scan_plugin.py
    python3 scripts/scan_plugin.py --self-test
    python3 scripts/scan_plugin.py --json report.json
    python3 scripts/scan_plugin.py --scanner /path/to/hermes-agent
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERMES_REPO = "https://github.com/NousResearch/hermes-agent.git"
REPO_ROOT = Path(__file__).resolve().parent.parent
# The Hermes release this plugin is built against. Pinned on purpose: the
# scanner's severity table changes on main without a version bump, so tracking
# main would let a demotion upstream turn this gate green while stable installs
# still refuse the plugin. Bump in its own commit when a release is targeted.
SCANNER_REF = "v2026.9.24"
# Findings in this category block a community install, whatever severity the
# scanner build reports for them (see the module docstring).
BLOCKING_CATEGORY = "credential_exposure"


def default_cache() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base) / "hermes-plugin-scanner"


def fetch_scanner(dest: Path, ref: str = SCANNER_REF) -> Path:
    """Sparse-clone just tools/ from hermes-agent at *ref*. plugin_guard,
    skills_guard and plugin_guard_context all live there, and none of them
    reach outside it."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--quiet", "--depth", "1", "--filter=blob:none", "--sparse",
         "--branch", ref, HERMES_REPO, str(dest)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    subprocess.run(["git", "sparse-checkout", "set", "tools/"], cwd=dest,
                   check=True, stdout=subprocess.DEVNULL)
    return dest


def load_scan(scanner_dir: Path):
    sys.path.insert(0, str(scanner_dir))
    try:
        from tools.plugin_guard import PLUGIN_SCANNER_VERSION, scan_plugin
    except ImportError as exc:  # a moved module or a half-fetched tree
        raise SystemExit(f"could not import the scanner from {scanner_dir}: {exc}")
    return PLUGIN_SCANNER_VERSION, scan_plugin


def blocking(findings) -> list:
    """Findings a community install would be refused for."""
    return [f for f in findings
            if f.severity == "critical"
            or (f.severity == "high" and f.category == BLOCKING_CATEGORY)]


def _redact(match) -> str:
    """Print the shape of a match, never the value.

    A finding's file and line are already enough to find it in the source, and
    some matches are credential-shaped. Copying them into CI logs and uploaded
    artifacts would spread values that are better left in the file they came
    from.
    """
    text = " ".join((match or "").split())
    if not text:
        return ""
    out, run = [], 0
    for ch in text:
        if ch.isalnum():
            run += 1
        else:
            out.append("x" * run)
            out.append(ch)
            run = 0
    out.append("x" * run)
    return "".join(out)[:96]


def self_test(scan_plugin) -> int:
    """Require the scanner to flag a planted secret-shaped literal.

    Guards the gate itself: a scanner build that stops reporting this class
    (upstream demotion, a changed pattern, a broken sparse fetch) would let a
    blocking tree pass. The literal is synthetic -- never a real credential --
    and it is assembled at runtime on purpose: spelled out in this file it is
    itself a `hardcoded_secret` finding, so the gate would block the repository
    it protects.
    """
    planted_name = "_CLIENT_" + "SECRET"
    planted_value = "-".join(("GOCSPX", "SELFTEST", "SYNTHETIC", "LITERAL", "0001"))
    with tempfile.TemporaryDirectory(prefix="plugin-scanner-selftest-") as tmp:
        tree = Path(tmp) / "planted"
        tree.mkdir()
        (tree / "provider.py").write_text(
            '"""Synthetic self-test fixture."""\n'
            f'{planted_name} = "{planted_value}"\n',
            encoding="utf-8",
        )
        result = scan_plugin(tree, source="quota-plugin-self-test")
    found = blocking(result.findings)
    print(f"self-test: planted literal -> verdict {result.verdict}, "
          f"{len(found)} blocking finding(s)")
    for f in found:
        print(f"  [{f.severity:8}] {f.pattern_id:22} {f.category}")
    if not found:
        print("self-test FAILED: the scanner no longer reports a planted "
              "credential_exposure literal as blocking; the gate cannot see the "
              "class it exists for.", file=sys.stderr)
        return 1
    print("self-test: ok")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scanner", type=Path,
                    help="path to an existing hermes-agent checkout (skip the fetch)")
    ap.add_argument("--scanner-ref", default=SCANNER_REF,
                    help=f"hermes-agent ref to fetch the scanner from (default: {SCANNER_REF})")
    ap.add_argument("--json", type=Path, dest="json_out",
                    help="also write the full findings as JSON here")
    ap.add_argument("--path", type=Path, default=REPO_ROOT,
                    help="directory to scan (default: this repository)")
    ap.add_argument("--self-test", action="store_true",
                    help="only check that the scanner still reports the known blocking class")
    args = ap.parse_args()

    if sys.version_info < (3, 11):
        print("warning: hermes-agent needs Python 3.11+ to run the scanner.",
              file=sys.stderr)

    scanner_dir = args.scanner or fetch_scanner(default_cache(), args.scanner_ref)
    version, scan_plugin = load_scan(Path(scanner_dir).resolve())

    if args.self_test:
        return self_test(scan_plugin)

    result = scan_plugin(args.path.resolve(), source="rarf/hermes-quota-plugin/community")

    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings = sorted(result.findings,
                      key=lambda f: (order.get(f.severity, 9), f.file, f.line))
    counts: dict = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1

    print(f"plugin scanner {version} @ {args.scanner or args.scanner_ref}"
          f"  --  verdict: {result.verdict}")
    print(f"{args.path.name}: {result.summary}")
    print("  " + (", ".join(f"{n} {sev}" for sev, n in
                            sorted(counts.items(), key=lambda kv: order.get(kv[0], 9)))
                  or "no findings"))
    print()
    for f in findings:
        print(f"  [{f.severity:8}] {f.pattern_id:22} {f.file}:{f.line}")
        snippet = _redact(f.match)
        if snippet:
            print(f"{'':14}  {snippet}")

    if args.json_out:
        args.json_out.write_text(json.dumps({
            "scanner_version": version,
            "scanner_ref": args.scanner_ref,
            "verdict": result.verdict,
            "summary": result.summary,
            "counts": counts,
            "findings": [
                {"severity": f.severity, "pattern_id": f.pattern_id,
                 "category": f.category, "file": f.file, "line": f.line,
                 "match": _redact(f.match)}
                for f in findings
            ],
        }, indent=2))
        print(f"\nwrote {args.json_out}")

    blockers = blocking(findings)
    if blockers:
        print(f"\n{len(blockers)} blocking finding(s).", file=sys.stderr)
        print("Hermes refuses a community install or update for these, so this "
              "would stop users upgrading.", file=sys.stderr)
        for f in blockers:
            print(f"  {f.severity:8} {f.category}/{f.pattern_id}  {f.file}:{f.line}",
                  file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
