# Installation and updates

## Source installation

```bash
git clone https://github.com/rarf/hermes-quota-plugin.git
cd hermes-quota-plugin
./install.sh
```

**Scope:** the bundled installer installs the backend and local Desktop widget and enables quota for every existing profile; it may create profile symlinks. Do not use it for a default-only update when other profiles must remain untouched. Review the script and preserve backups first. The current multi-account candidate may not yet be published on the default branch or catalog pin; use only the exact approved build available to you.

Verify through the installed CLI:

```bash
hermes plugins doctor quota --ci
hermes quota refresh
hermes quota status
```

Caches are per profile. Run collection in each profile you intentionally enabled.

## Remote Desktop

The Python backend belongs on the gateway; the widget belongs on the computer running Hermes Desktop. They can be different machines. The client loader uses its own app-local Desktop plugin root, not a path copied on the remote gateway. Keep both halves on the same approved build. [Diagnose old labels/icons](troubleshooting.md#remote-desktop-old-names-or-missing-icons).

Use **Reload desktop plugins** for client JavaScript changes. A long-lived backend process does not reload Python merely because files changed; coordinate any necessary restart without interrupting active turns. Fresh `hermes quota` CLI calls load the installed code in a new process.

## Updating

Source installation: fetch the approved revision and re-run the installer only if its all-profile scope is acceptable. A catalog-managed installation follows its pinned revision; it does not automatically select an unreleased local candidate. An immutable `--ref` must be the full commit SHA and must be available from the requested source. Verify installed identity after any update rather than treating acceptance as success.

```bash
hermes quota status --version-json
```

The source installer stamps `version.json`. A manually retained stamp can become stale; reconcile it with the actual installed bytes. On a remote setup, updating the gateway alone cannot replace the client widget.

## Uninstall

`./uninstall.sh` removes the source-installed plugin symmetrically, including installer-created profile links. Review its profile scope first and preserve unique runtime state. Do not remove another plugin or unrelated profile configuration.
