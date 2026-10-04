#!/usr/bin/env python3
"""Run Hermes' own plugin scanner against this repository.

The scanner is not vendored here: it lives in NousResearch/hermes-agent and is
the exact code that runs when someone installs or updates this plugin. That is
the point -- a critical finding here is not a style opinion, it stops the plugin
installing at all.

By default this fetches the scanner into a cache directory with a sparse
checkout of ``tools/`` only. Pass --scanner to reuse a checkout you already have.

Exit codes:
    0  no critical findings
    1  at least one critical finding (this blocks community install/update)
    2  the scanner could not be run or fetched

Requires Python 3.11+ -- that is hermes-agent's floor, not this plugin's.
This plugin still supports 3.9; only the scanner needs the newer interpreter.

    python3 scripts/scan_plugin.py
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


def default_cache() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base) / "hermes-plugin-scanner"


def fetch_scanner(dest: Path) -> Path:
    """Sparse-clone just tools/ from hermes-agent. plugin_guard, skills_guard
    and plugin_guard_context all live there, and none of them reach outside it."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--quiet", "--depth", "1", "--filter=blob:none", "--sparse",
         HERMES_REPO, str(dest)],
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scanner", type=Path,
                    help="path to an existing hermes-agent checkout (skip the fetch)")
    ap.add_argument("--json", type=Path, dest="json_out",
                    help="also write the full findings as JSON here")
    ap.add_argument("--path", type=Path, default=REPO_ROOT,
                    help="directory to scan (default: this repository)")
    args = ap.parse_args()

    if sys.version_info < (3, 11):
        print("warning: hermes-agent needs Python 3.11+ to run the scanner.",
              file=sys.stderr)

    scanner_dir = args.scanner or fetch_scanner(default_cache())
    version, scan_plugin = load_scan(Path(scanner_dir).resolve())

    result = scan_plugin(args.path.resolve(), source="rarf/hermes-quota-plugin/community")

    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings = sorted(result.findings,
                      key=lambda f: (order.get(f.severity, 9), f.file, f.line))
    counts: dict = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1

    print(f"plugin scanner {version}  --  verdict: {result.verdict}")
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

    criticals = [f for f in findings if f.severity == "critical"]
    if criticals:
        print(f"\n{len(criticals)} critical finding(s).", file=sys.stderr)
        print("Hermes blocks community install and update while a critical "
              "finding is present, so this would stop users upgrading.", file=sys.stderr)
        for f in criticals:
            print(f"  {f.file}:{f.line}  {f.pattern_id}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())