# Changelog

All notable changes are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - Unreleased

### Added

- Read-only MP4 discovery, SQLite reconciliation, and periodic rescans.
- Cached, rate-limited Jikan metadata with conservative matching and sidecar
  overrides.
- Atomic static catalogue generation with accessible single-player episode
  navigation.
- Nginx byte-range streaming configuration rendered from application settings.
- Hardened native systemd and rootless-process Compose deployments.
- Repository-root install, backup, update, validation, and uninstall targets.
- Offline end-to-end fixtures and CI across supported Python versions.

### Security

- Media is mounted read-only to the indexer and containers.
- Generated provider content and local names are escaped; remote artwork is
  bounded and copied locally.
- Native and container services use least-privilege accounts and do not expose
  an application authentication boundary.

[0.1.0]: https://github.com/bdobrica/RPiStreamer/releases/tag/v0.1.0
