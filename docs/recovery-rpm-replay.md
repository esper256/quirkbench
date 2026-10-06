# Replay recovery inputs from a retained RPM repository

Use this when moving Fedora metadata no longer supplies a selected exact package
(for example, `glibc-common` matching the selected `glibc`). The inputs are the
selected acquisition JSON, its reviewed public signing key, and a **complete copy
of the RPMs from that acquisition**, including dependencies and the four exact
kernel packages. This route serves those bytes locally through the existing DNF5
adapter. It does not upgrade packages, build an image or establish signature/boot
acceptance.

Keep the retained RPM copy in an ordinary user directory outside Git and managed
controller generations, for example `/absolute/retained-stock`. Copy before a
successful `lock` disposes its acquisition generation. Do not point a repository
at transient generation paths or import hundreds of RPMs individually with
`artifact put`: counted input history can retire earlier objects. Preserve a
verified lock/snapshot with its referenced CAS objects through supported retention
or backup when available; ordinary RPM filenames can differ from CAS hashes.

The native controller needs Quirkbench, `rpm`, DNF5 with its download command and
`createrepo_c`. Keep the source copy; creating repository metadata adds `repodata`
and does not install RPMs on the host. Use absolute paths below.

## Check the selected inputs

```sh
quirkbench dev recovery inputs replay-check --spec /absolute/selected-candidate.json \
  --directory /absolute/retained-stock > /absolute/replay-inventory.json
```

Exit0 means all selected userspace hashes/NEVRAs and requested exact kernel NEVRAs
are present. Exit4 means blocked: `missing` names each expected package, NEVRA and
SHA256; `mismatched` gives the observed identity/hash/path. A kernel not hashed by
the selected specification has `sha256:null`; its eventual verified lock records
actual bytes. Exit2 is an unreadable/invalid input or inspection failure. These
are inventory results only. They do not prove complete transitive dependencies,
signatures, a usable lock or image readiness. DNF5 must resolve the closure next.

For a missing RPM, restore the exact retained bytes or obtain the Fedora-signed
historical package from its archived build, then compare the selected SHA256.
Fedora Koji's signed paths use
`packages/NAME/VERSION/RELEASE/data/signed/KEY_ID/ARCH/NAME-VERSION-RELEASE.ARCH.rpm`;
the build's unsigned sibling can have the same NEVRA and a different hash.
Do not invent hashes, edit the candidate to whatever version is currently available
or substitute an unsigned package. If an exact dependency cannot be recovered,
preparation remains blocked until a separately reviewed candidate is available.
If a directory contains conflicting versions/representations of selected names,
prepare a separate replay directory containing the intended closure; preserve the
original source copy.

## Publish local repository metadata and bind a new specification

```sh
createrepo_c /absolute/retained-stock
```

Create `/absolute/retained-stock.repo` with these bytes, substituting actual absolute
paths (URL-encode spaces or other URI characters):

```ini
[retained-stock]
name=Retained exact stock recovery inputs
baseurl=file:///absolute/retained-stock
enabled=1
gpgcheck=1
gpgkey=file:///absolute/reviewed-fedora-public.asc
```

For a first pairing image, the supported reviewed selection needs no package-list
editing:

```sh
quirkbench dev recovery inputs candidate-spec --candidate fedora44-pairing-v1 \
  --repository /absolute/retained-stock.repo --repository-id retained-stock \
  > /absolute/replay-candidate.json
```

Inventory it before acquisition; the retained repository must also contain its
exact signed OpenSSL input. This deliberately selects the new pairing snapshot;
it does not change an existing acquisition owner's identity.

For replay of an **existing** selected snapshot instead, create a new specification
by changing only the repository binding of that selected JSON. Package hashes, Fedora/kernel release and full key fingerprint
remain identical:

```sh
PYTHONPATH=/absolute/selected-runtime/lib python3 -B - /absolute/selected-candidate.json /absolute/retained-stock.repo \
  /absolute/replay-candidate.json <<'PY'
import sys
from pathlib import Path
from quirkbench.contracts import canonical, digest
from quirkbench.recovery_acquisition import load_spec
selected = load_spec(Path(sys.argv[1]).read_bytes())
content = Path(sys.argv[2]).read_bytes().decode('utf-8')
selected['repositories'] = [{'name': 'retained-stock.repo', 'content': content,
                             'sha256': digest(content.encode()), 'ids': ['retained-stock']}]
with Path(sys.argv[3]).open('xb') as output:
    output.write(canonical(load_spec(canonical(selected))))
PY
```

For an archive installation, use its recorded `runtime_root` in place of
`/absolute/selected-runtime`: the launcher's library bootstrap is local to that
launcher, so the specification-editing Python snippet explicitly selects the same installed
`lib` directory with `PYTHONPATH`. The acquisition wrapper independently carries its own library bootstrap. Use a supported Python3.11+ interpreter. `-B` and the generated wrapper prevent
bytecode caches from changing the immutable installed library. For a pip installation, use
its interpreter and omit this prefix if Quirkbench is already importable. Do not
point it at a different runtime than the controller used for planning/locking.

The new canonical
specification has a new digest because the repository changed. Existing owners
keep their original binding; do not edit their staged repositories/specification.
Repo metadata may be regenerated, but selected RPM identities remain pinned and
all acquired RPMs must pass the independent public-key verification at `lock`.

## Acquire into a fresh managed generation and verify

On an existing or freshly set up controller, use its selected state outside Git:

```sh
quirkbench recovery-inputs acquire-plan \
  /absolute/controller-state/inputs/replay-1 --spec /absolute/replay-candidate.json \
  > /absolute/replay-plan.json
```

The plan downloads nothing. Execute its returned **wrapper `argv`** unchanged;
it includes the planning installation's Python executable and library bootstrap.
The following uses only Python's standard library to run that array without a shell:

```sh
python3 - /absolute/replay-plan.json <<'PY'
import json, subprocess, sys
plan = json.load(open(sys.argv[1]))
argv = plan['argv']
subprocess.run(argv, check=True)
PY
```

Use the wrapper rather than its displayed `dnf_argv`, so acquisition completion,
process ownership, failure diagnostics and retention remain registered. Only the
explicit `retained-stock` repository is enabled. DNF5 still resolves all dependencies;
a missing dependency blocks the operation rather than selecting host repositories.
Read the failed generation's `download-failure.log` for the exact unsatisfied
requirement. Restore its matching signed RPM to the retained copy, regenerate
metadata, and retry in a new managed generation after recording the failure.
Do not run `lock` on a failed or manually filled acquisition generation.

After acquisition completes, separately verify and retain its RPMs:

```sh
quirkbench dev recovery inputs lock \
  /absolute/controller-state/inputs/replay-1/rpms \
  --spec /absolute/replay-candidate.json --public-key /absolute/reviewed-fedora-public.asc \
  --builder-image-digest sha256:ACTUAL_BUILDER_DIGEST \
  --diagnostics /absolute/controller-state/inputs/replay-verification-1
```

Use the configured builder's actual identity. The retained specification, exact
public fingerprint, all package bytes and full RPM signatures are verified before
a usable rootfs lock is published. Keep the acquisition/spec/inventory and failed
logs as evidence; a completed download and a completed signature lock are separate
outcomes. Continue with [recipe/image preparation](recovery-operations.md#recovery-inputs-and-preparation)
only after a successful lock. Native image, pairing and hardware acceptance remain
separate operator operations.
