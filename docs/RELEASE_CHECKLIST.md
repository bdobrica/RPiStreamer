# Release checklist

## Automated

- [ ] `make check` passes from a clean checkout.
- [ ] The wheel builds and installs into a clean Python 3.11+ environment.
- [ ] Nginx syntax/range tests pass with the host Nginx package.
- [ ] Both container images build and `docker compose config --quiet` passes.
- [ ] Dependency and base-image updates have been reviewed.

## Host acceptance

Repeat this section on one Raspberry Pi OS Bookworm arm64 host and one Debian
12/Ubuntu 24.04 amd64 host. Record host model, OS, Python, Nginx, install mode,
collection size, elapsed scan time, and peak RSS in the release notes.

- [ ] Install from the release wheel with the documented native procedure.
- [ ] `make validate` succeeds.
- [ ] Initial scan produces a browsable catalogue.
- [ ] A browser plays an MP4 and seeks forward/backward.
- [ ] A `Range: bytes=100-199` request returns 206 and exactly 100 bytes.
- [ ] Adding and removing a fixture is reflected after `SIGHUP`.
- [ ] Restarting during scan/generation retains a valid database and last
      published catalogue.
- [ ] Backup/restore exercise below succeeds.
- [ ] Representative scan stays within the recorded performance budget.

## Disaster recovery exercise

1. Run `make backup BACKUP_DIR=/var/backups/rpi-streamer` and record the archive.
2. Stop `rpi-streamer`; copy the current database and site aside as evidence.
3. Move `/var/lib/rpi-streamer` to a temporary, host-local quarantine path.
4. Extract the archive from `/` and verify ownership remains appropriate.
5. Run `make validate`, start the service, and load the catalogue plus one
   byte-range request.
6. Compare restored catalogue/database presence, then delete the quarantine
   copy only after successful verification.

## Publish

- [ ] Version agrees in `pyproject.toml`, `rpi_streamer.__version__`, and the
      changelog.
- [ ] Step 11 host evidence is recorded and no acceptance item is outstanding.
- [ ] Create signed tag `v0.1.0` and publish the wheel/checksum.
- [ ] Confirm installation from the published artifact.
