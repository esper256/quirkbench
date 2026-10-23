# Candidate layer reuse — 2026-10-23

Real candidate preparation on the development controller, using production code
and isolated data. No target deployment, controller restart or recovery rebuild.

## Result

| Preparation | Total | OCI export | Layers reused / total | Layer bytes written | OSTree import |
| --- | ---: | ---: | ---: | ---: | ---: |
| First candidate, empty OCI cache | 356.51 s | 165.90 s | 0 / 140 | 11.22 GiB | 41.74 s |
| Change only the startup probe | 33.56 s | 0.16 s | 137 / 140 | 12 KiB | 25.74 s |
| Add the previously absent `inotify-tools` package | 62.24 s | 1.03 s | 136 / 140 | 138.27 MiB | 28.84 s |
| Request the original candidate again | 0.47 s | None | Whole candidate reused | None | None |

OSTree reported the same layer reuse counts for both changed candidates. Its
initial import reused 128 layers from the existing bare repository. These are
warm repeated-loop measurements, not cold installation timings or guarantees.
The package layer includes RPM database changes; it is larger than the added
executable. Import still merges the filesystem and publishes the commit.

Checked the published commits directly: each contains the intended probe;
`inotifywait` exists only in the added-package candidate; `/usr/bin/sudo` retains
root ownership and its privileged mode. Requesting the original candidate returned
its exact original commit. This does not restore mutable files from a prior run.

## Why reuse now works

A small real Podman comparison showed compressed OCI export rewriting unchanged
layers: local uncompressed digests did not match the stored compressed blobs.
`podman save --format oci-dir --uncompressed` into a persistent directory skipped
existing blobs. The full candidate measurements above confirm that behavior at
Bazzite scale. The tradeoff is a larger disposable controller cache.

The ordinary OSTree import and publication path remains in place. The remaining
25–29 seconds of import is now a more useful optimization target than repeated
export of the base image. Target transfer was not measured in this check.

## Environment

- Bazzite `44.20261012.1`, pinned image digest
  `sha256:5d49529cf809ff5b9e1ba52fd4833ec4099005673af4587b15d55b69210264e3`.
- Podman 5.8.7; libostree 2026.2; rpm-ostree 2026.1.
- Existing Podman base, target RPM, tools image and OSTree repositories were warm.
  The OCI directory started empty. Later preparations used those same caches.
- An exploratory request for `tree` was excluded from the package-addition result:
  inspection showed it was already installed in the base.
- 59 focused integration tests passed in 60.23 seconds. Independent review found
  no blocking issues. No hardware boot claim is made by these checks.
