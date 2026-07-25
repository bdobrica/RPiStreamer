# Contributing

RPi Streamer supports Python 3.11–3.13 on Linux. From the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
make check PYTHON=.venv/bin/python
```

Keep runtime dependencies exceptional and justified. Tests must be offline by
default, use synthetic media, and cover persisted/public-contract changes.
Never add personal media, API keys, generated state, or remote response dumps.

Open a focused change with tests and update `README.md` and `CHANGELOG.md` when
behavior changes. Add or supersede an
[architecture decision record](docs/adr/README.md) when a durable design
choice changes. Commit messages use an imperative `type: summary` form such as
`fix: preserve published site after failure`.

Before proposing a release, complete
[`docs/RELEASE_CHECKLIST.md`](docs/RELEASE_CHECKLIST.md) on both supported
architectures.
