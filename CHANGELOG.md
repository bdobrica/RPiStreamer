# Changelog

All notable changes are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - Unreleased

### Added

- Read-only MP4 discovery, SQLite reconciliation, and periodic rescans.
- Cached, rate-limited Tenrai metadata with conservative matching, sidecar
  overrides, and explicit Jikan/offline rollback modes.
- Optional bounded OpenAI title and episode inference with structured output,
  protected credentials, and SQLite caching.
- Atomic static catalogue generation with accessible single-player episode
  navigation.
- Nginx byte-range streaming configuration rendered from application settings.
- Hardened native systemd and rootless-process Compose deployments.
- Repository-root install, backup, update, validation, and uninstall targets.
- Offline end-to-end fixtures and CI across supported Python versions.
- Architecture decision records for the shipped design.
- Multi-work collections with manual, deterministic, and optional
  model-assisted file mappings; grouped single-player catalogue pages; and
  bounded per-collection inspection, validation, refresh, invalidation, and
  recomputation controls.
- Sanitized aggregate mapping counters for native and container scan logs.

### Security

- Media is mounted read-only to the indexer and containers.
- Generated provider content and local names are escaped; remote artwork is
  bounded and copied locally.
- Native and container services use least-privilege accounts and do not expose
  an application authentication boundary.

[0.1.0]: https://github.com/bdobrica/RPiStreamer/releases/tag/v0.1.0
