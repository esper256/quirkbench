> **Legacy reference — not current requirements or agent instructions.**
> This document was archived on 2026-10-07. Consult [current documentation](../README.md).
> Commands and implementation claims below may be obsolete.

# Public investigation results export

Export an immutable captured source and its attributed report from existing controller
state. The installed command writes a **new uncompressed tar file** atomically. It
never starts a controller/service, prunes state, changes source, publishes patches,
authorizes execution or creates a resumable backup.

```sh
quirkbench investigation results retain input-device-investigation --note 'Preserve comparison inputs' --request-id retain-export-01 --json
quirkbench investigation results export input-device-investigation --output /absolute/public/input-investigation.tar --author 'Actual Author <actual@example.org>' --json
quirkbench investigation results export input-device-investigation --capture CAPTURE_OPERATION --comparison comparison.json --output /absolute/public/earlier-investigation.tar --author 'Actual Author <actual@example.org>' --timeout 300 --json
```

The output parent must exist and be outside private controller state. Ancestor
aliases are resolved at admission; checkout-local exports are allowed. Existing
output is never overwritten. The default capture is the
investigation's latest completed handed-off capture; `--capture` selects a completed,
stopped capture from that same investigation/workspace. The source writer must remain
quiesced to read the recorded Git base. Live edits are never substituted for captured
bytes. Missing base/bytes or an absent author produce an explicitly inconclusive,
unvalidated package with limitations; unavailable scoped retention rejects the export.

`--author` explicitly names the author of a new **export representation commit** for
dirty/uncommitted captured bytes. It does not assign historical authorship, copy old
commits as new patches, invent a signoff or infer an author from Git configuration.
Existing base commit metadata is preserved in `reproduce/base.bundle`. Review captured
source and acknowledged evidence for suitability before sharing: exported source and
logs are public inputs, not a content redaction service.

## Package and reconstruction

The fixed layout is `README.md`, `report.md`, `manifest.json`, `patches/`, `reproduce/`,
`experiments/` and `evidence/`. The [versioned manifest](../../schemas/investigation-export.v1.schema.json)
records SHA256/size for every public file except itself; the CLI receipt identifies
both manifest and complete archive by SHA256. Check these hashes against a trusted
receipt before extracting into a new private directory. The manifest is an integrity
inventory, not a publisher signature or independent scientific validation.

`patches/0001-captured-changes.patch` is binary, full-index `git format-patch` output
against the actual recorded base. `reproduce/` includes capture receipt, source tar,
canonical tree manifest, standalone checker and the real base bundle when available.
Follow the generated README using Git and Python 3.11+. Its shallow boundary preserves
the actual base without claiming unavailable ancestor history. Apply with `git am
--keep-cr`, then run `check_tree.py ... --restore-modes`: Git preserves executable
bits and symlinks; the tree manifest restores additional POSIX modes. An unchanged
capture has an empty patch and explicitly skips `git am`. Export verifies these same
steps using the **delivered base bundle** in a separate fresh repository, comparing
all file bytes, modes, deletions, allowed new files and symlink targets. Original
working-tree, HEAD and index remain untouched. No kernel build or boot is performed.

Distribution provenance remains separate in `reproduce/distribution.json` when
available. Its package/spec/patch identities and distribution-prepared actual base
are retained; unknown upstream Git ancestry remains unknown. The source package and
pinned build/RPM closure are identified by existing records rather than silently
bundled as a second build system.

## Attribution and bounds

`experiments/report.json` contains the existing report's complete selected pages under
one read-only database snapshot. Raw private tokens, controller database/configuration,
private Git configuration and opaque measurement dictionaries are excluded. A report's
`source_bytes_verified: false` remains its metadata-only fact; the export's distinct
`source.reconstruction_verified` records its byte/application proof.

`validation_status: tested-source-match` means the exported capture matches a verified
attributed terminal attempt with exact candidate adoption. It does **not** mean problem
reproduction, equivalent peripheral/environment conditions, exposure to a stimulus,
a causal fix or native qualification. The conclusion always remains inconclusive.
Later cleanup with different captured bytes is unvalidated until another exact-source
approved attempt exists. Missing evidence/symbols remain explicit even when a source
match exists; a PASS result cannot fill gaps. Evidence bytes require acknowledgement
and the **original attempt's retained owner**; incidental objects held by another
owner cannot resurrect evidence. Symbols require the validated experiment closure.

Existing owner/dependency references remain unchanged. Export holds the existing
shared maintenance barrier; it creates no permanent pins. Use `report-retain` before
export, inspect its missing/retired-owner receipt, and explicitly pin an older capture
operation with `quirkbench admin storage pin CAPTURE_OPERATION --note 'Keep export source'`
when needed. Pins cannot restore collected objects. See [report retention](investigation-reports.md)
and [storage maintenance](local-state-maintenance.md).

Limits: 100 experiments, 1,000 attempts, 8MiB report/manifest metadata, 8GiB per copied
object, 16GiB copied-object/serialized payload, 32MiB Git output/index input and a
128GiB verification-work budget. Repeated copies/hash/serialization/seal reads are
charged; source extraction/verification and native Git growth reserve conservative
allowances before launch, including the actual base tree even when the capture deletes
large files. All staging observes the configured free-space reserve (default 20GiB).
`--timeout` is 1–600 seconds, default 300; cooperative checks cover chunks and native
Git supervision, not a hard interrupt for stalled kernel I/O. Oversized or pressured
exports stop without a complete output file. Interruption or tampering before publish
cannot expose partial output as complete. Export is bounded software preparation;
physical/native qualification and publication require separate authorization.

Public evidence/missing/file lists are each limited to 16,384 entries. Creation and
reconstruction hashes stay pinned through the final inventory and serialization;
later mutations cannot become new trusted hashes merely because inventory runs later.
