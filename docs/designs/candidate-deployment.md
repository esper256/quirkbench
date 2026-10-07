# Deploy candidates efficiently

Quirkbench 1.0 uses rpm-ostree on the controller computer for candidate assembly
and OSTree for storage, transfer and target deployment. Details are revisable;
keep this one implementation working rather than building interchangeable systems.

The controller obtains official RPMs and the agent's custom RPMs. Recovery obtains
the assembled candidate from the controller, not packages from Fedora. Run the
assembly tools in the supported build environment; the controller computer need
not run Fedora or an OSTree desktop.

Use the normal paired connection and stock package checks. Custom experiment RPMs
and OSTree candidates need no separate publisher approval or mandatory signing
process. Configure tool credentials where needed; keep them out of the agent's
experiment loop. This is test software, not a hardened release-distribution system.

1. Resolve the experiment's package selection and explicit replacements.
2. Assemble with rpm-ostree using persistent package caches and record the candidate
   in OSTree. Reuse identical prepared results.
3. Recovery requests work through its authenticated controller connection and gets
   the candidate identifier, test instructions and time limit.
4. Recovery fetches missing OSTree content into the shared data partition and
   prepares the candidate. Interrupted preparation must not select an incomplete
   candidate for boot.
5. Select the completed candidate for one boot. Recovery remains the default.

Use OSTree's supported boot layout. Keep candidate software read-only where its
normal deployment expects it; writable configuration and application data start
fresh for each run unless the experiment explicitly needs persistence. Evidence
uses a separate writable directory on the same filesystem, outside OSTree-managed
files. Recovery settings and run temporary directories also live on shared data.
Package changes never update the recovery system or its settings.

Measure assembly time, bytes transferred and USB preparation time for repeat
experiments. rpm-ostree caching does not guarantee that assembly touches only
changed files. Avoid fresh caches, needless full copies and broad repository
checks on every submission. Defer binary-difference generation and other extra
optimizations until measurements justify them.

Use existing OSTree cleanup tools after releasing obsolete candidate references.
Protect active work and explicitly retained candidates; limit disposable history
and caches by a storage budget. Remove finished temporary assembly directories.
Do not delete evidence, recovery settings, source records or debugging symbols
as cache cleanup. Recovery deletes uploaded USB evidence only after the controller
confirms durable receipt; see [recovery](recovery.md).
Keep useful agent build directories until explicitly cleaned. Report space use
and offer manual cleanup; missing retained build inputs must be reported, not
silently replaced with newer packages.

Keep software selection, assembly and deployment separate in the code. Replacing
rpm-ostree later should not require replacing the investigation interface; replacing
OSTree would also change target boot and storage integration.
