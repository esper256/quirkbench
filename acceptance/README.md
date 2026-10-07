# Expensive qualification fixtures

These are optional release/physical validation tools, not routine development tests
or product requirements. Follow [development guidance](../docs/designs/development.md).
Release-only qualification still needs an explicit final-major-release request and
`RELEASE_QUALIFICATION=1`; do not bypass the guard by invoking scripts directly.
Separately requested product builds, flashing and experiments do not authorize the
full suite. Software tests do not establish hardware coverage.

Use the [archived fixture guide](../docs/legacy/acceptance-README.md) only when a
specific authorized run needs its invocation details. Its historical product gates
and per-run approval rules are superseded by current requirements/designs.
