# Development

```bash
bash -n install.sh uninstall.sh
python -m py_compile __init__.py commands.py quota_cache.py quota_providers/*.py
python -m unittest discover -s tests -p "test_*.py"
node --check desktop/plugin.js
python scripts/scan_plugin.py
hermes plugins doctor quota
hermes quota refresh
```

`scan_plugin.py` runs Hermes' own plugin scanner against this tree — the same
code that runs when someone installs or updates the plugin. **A critical finding
there stops people installing the plugin**, and the `plugin-scanner` CI job
checks for it on every PR. The scanner is pinned to a Hermes release instead of
`main`, because a scanner-side demotion must not turn the gate green while stable
installs still refuse the tree; the gate fails on any `critical`, or on `high` in
`credential_exposure`, and `--self-test` proves it can still see that class. It
needs Python 3.11+, while the plugin itself supports 3.9. See
[adding a provider](add-provider.md#run-the-plugin-scanner).

Widget changes can be checked without launching the app — the harness renders the
real `desktop/plugin.js` against a real payload and asserts the pane's contract
(provider names, brand mark, footer cadence):

```bash
npm i --no-save react react-dom @babel/core @babel/preset-react @babel/preset-env
hermes quota status --json --cached > .widget-fixture.json
node scripts/render-widget.cjs                        # pane
AREA=statusbar.right node scripts/render-widget.cjs   # status bar chip
```

The offline widget render tests require Node.js (no npm dependencies). They
execute the actual widget with SDK hooks stubbed and a fixed locale/timezone.
For optional real Chromium geometry tests, see [widget layout tests](widget-balance-layout.md).

To add a provider, write a fetcher in `quota_providers/` that returns a
`QuotaResult` and register it. The cache and the widget need no
provider-specific changes. [docs/add-provider.md](add-provider.md) is the
full walkthrough, including the merge checklist — run the scanner before you
push, not after someone reports the install is blocked.
