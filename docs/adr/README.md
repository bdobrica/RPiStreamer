# Architecture decision records

These records capture decisions that shape RPi Streamer beyond an individual
implementation change. They replace the completed project-wide milestone
plan; current behavior and operator instructions remain in the project
[README](../../README.md). The active, feature-specific
[multi-work mapping plan](../../PLAN.md) will add an ADR when its contract is
implemented.

| ADR | Decision |
|---|---|
| [0001](0001-static-nginx-architecture.md) | Use Nginx as the data plane and static HTML as the catalogue |
| [0002](0002-sqlite-and-read-only-scanning.md) | Keep catalogue state in SQLite and scan media read-only |
| [0003](0003-metadata-identity-and-tenrai-transport.md) | Use MAL identity with Tenrai as the default transport |
| [0004](0004-conservative-metadata-matching.md) | Require conservative, explainable metadata matching |
| [0005](0005-optional-model-assisted-inference.md) | Keep model-assisted inference optional, bounded, and verified |
| [0006](0006-service-configuration-and-deployment.md) | Share one configuration contract across native and container deployments |
| [0007](0007-atomicity-security-and-verification.md) | Preserve last-known-good state and test without live dependencies |

ADRs are immutable once superseded. A later decision should add a new record
and mark the earlier one superseded rather than rewriting history.
