# Build a controller release

The normal installation is one native RPM, with per-user files in `~/.quirkbench`
and a foreground controller. Fedora is the first packaging target. Other host
distributions need their own dependency checks; the target OS is a separate choice.

For a development packaging check:

```sh
./dev package --development
```

This creates and installs an RPM in a disposable Fedora container, checks its
installed commands as an ordinary user, and saves the RPM under `dist/release`.
It installs no host RPMs, starts no host controller and builds no recovery image.
Ordinary edits need only `./dev quirkbench ...`, not another packaging check.

For a release, first choose an existing recovery image whose checks you intend to
publish. Building or testing that image is a separate operation.

```sh
./dev package --recovery-image /path/to/completed/image
```

The output contains the controller RPM, recovery archive, checksums in
`recovery-release.json`, and RPM build sources. The RPM embeds that selection;
`recovery flash` and `recovery download` use its exact URL and checksum. Interrupted
downloads are retried, not selected as completed images. No global “latest” URL
changes an installed release's selection.

For a controller-only release, use `--recovery-selection /path/to/recovery-release.json`
to keep the already published image. The existing download URL remains unchanged.
Local `recovery build` results remain separate; `recovery flash --built` selects
the last successful build or cache reuse.

Publishing is explicit: review the release version in `pyproject.toml` and
`src/quirkbench/__init__.py`, then attach the RPM, recovery archive and selection
to GitHub Release `v<version>`. The helper does not tag, upload or publish anything.
There is currently no hosted DNF repository or published recovery selection in
the source checkout. Use the development path until release assets are published.

The project currently declares `LicenseRef-Quirkbench`; no redistribution license
is supplied. The owner must settle that before public distribution. Do not invent
a license or signing identity while preparing packages.

After downloading a release RPM, Fedora users install it with
`sudo dnf install ./quirkbench-<version>-<release>.noarch.rpm`. Bazzite users who
choose native installation use its RPM layering mechanism instead. The `./dev`
path requires neither host installation nor layering. Package managers handle
application upgrades; stop the foreground controller before replacing its files,
then run it again. Updating software does not update flashed recovery media.
