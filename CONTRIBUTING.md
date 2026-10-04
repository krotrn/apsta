# Contributing to apsta

Thanks for helping! Bug reports with hardware details are as valuable as code.

## Reporting a bug

Please include:

- `apsta --version` and your distribution
- `apsta detect --json`
- `iw phy phy0 info` (use your card's phy)
- the failing command with `APSTA_DEBUG=1`, plus `journalctl -u apsta -u apsta-hostapd -n 100` if relevant

If `apsta detect` gets your card wrong, the `iw phy` output is enough for us to
add a regression test (`tests/fixtures/iw/`).

## Development setup

```bash
git clone https://github.com/krotrn/apsta && cd apsta
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
make check      # lint + format check + tests with coverage
```

Run your checkout without installing: `./apsta.py detect`, `sudo ./apsta.py start`.

The test suite needs **no root and no WiFi hardware**:

```bash
make test       # python -m unittest discover -s tests -t .
make coverage   # same, with a coverage report (CI requires ≥ 90 %)
make lint       # ruff check + ruff format --check
make fmt        # apply formatting
```

## Guidelines

- Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) first. Keep dependencies
  pointing down the layers: `cmd` → `services` → `net`/`hw`/`config` → `core`.
- Start processes only through `core.shell.run` with an argv list. Never
  `shell=True`, and never put secrets in argv.
- Library code raises `ApstaError` subclasses with `hints`. It doesn't print
  advice or call `sys.exit`.
- Any change to system state must be undoable: register a rollback step in the
  `Transaction`, and record what `stop` needs in `HotspotState`.
- Write all files through `core.fsutil.atomic_write`.
- Add tests. Prefer pure functions (parsers, renderers, decisions) with unit
  tests, use `FakeShell` for command sequences, and add an integration test in
  `tests/integration/` for new user-visible behaviour.
- Python ≥ 3.9 and the standard library only for the CLI.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `ci:`, `chore:`).
- Add a line to `CHANGELOG.md` under "Unreleased" for user-visible changes.

## Releasing (maintainers)

Run the **Version Bump** workflow (Actions → Version Bump → `X.Y.Z`), or locally:

```bash
python scripts/bump_version.py X.Y.Z   # pyproject, apsta_cli/__init__.py, PKGBUILD, debian/changelog
git commit -am "chore: release X.Y.Z" && git tag vX.Y.Z && git push origin main vX.Y.Z
```

The tag publishes everything:

| Workflow       | Job            | Publishes                                              | Needs                                                      |
| -------------- | -------------- | ------------------------------------------------------ | ---------------------------------------------------------- |
| `release.yml`  | publish-pypi   | PyPI                                                   | trusted publisher or `PYPI_API_TOKEN`                      |
| `packages.yml` | github-release | GitHub release with the `.deb` and `.pkg.tar.zst`      | —                                                          |
| `packages.yml` | arch-repo      | pacman repo on the `arch-repo` release                 | — (`ARCH_GPG_PRIVATE_KEY` to sign)                         |
| `packages.yml` | aur            | AUR package `apsta`                                    | `AUR_SSH_PRIVATE_KEY`                                      |
| `packages.yml` | ppa            | Launchpad PPA (`vars.PPA`, default `ppa:krotrn/apsta`) | `LAUNCHPAD_GPG_PRIVATE_KEY` (+ `LAUNCHPAD_GPG_PASSPHRASE`) |

Jobs whose secret is missing are skipped with a notice. Build packages locally
with `packaging/arch/ci-build.sh local` or `packaging/deb/ci-build.sh binary`
(on Ubuntu).
