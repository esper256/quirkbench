# Conventional internals

Revisable design supporting the [requirements](../requirements/product.md).

Use Python for the controller, MCP interface, CLI, recovery application and shared test tooling.
This is the chosen implementation language. Continue using existing Linux tools
for building, deployment and boot.

Design the application to be integration tested from the beginning. It is fine
to shape production code for this purpose. Keep external commands and other
expensive dependencies easy to replace in tests while exercising real Quirkbench
behavior. Pass storage roots and small replaceable Linux operations at startup;
keep ordinary file access real rather than introducing a universal filesystem
proxy. Follow the [testable application design](testing.md#keep-the-application-easy-to-exercise);
do not leave tests to work around an already completed implementation.

- Share application behavior between human and agent interfaces. Preserve owner data; no new migrations or compatibility layers.
- Use SQLite for controller work records, request IDs and results metadata; the foreground controller is the only writer. CLI, MCP and HTTPS use its application functions through the existing local interface. Keep editable config in TOML and output/evidence in ordinary files. Target command records remain target-owned; a controller database does not replace them.
- Single-user software needs locks, not accounts/tenants. Ordinary data is not invalid merely because of group-readable permissions or symlinked ancestors. Create secrets privately and authenticate network peers.
- This is a personal experiment tool, not a hardened OS distribution service. Use ordinary tool-supported authentication and package checks. Do not add a release-security system, extra signing approvals or repeated verification rituals for experiments. Keep practical disk protection separate from this choice.
- Derive managed paths from current roots and existing IDs; don't persist redundant absolute or relative paths. Store necessary external locations once in configuration. Paths and permanent inode observations are not durable identity.
- Validate untrusted inputs at entry and recheck where mutation/races matter. Keep actual containment and live ownership checks; avoid repeated full-tree validation. Configuration edits are normal, not integrity violations.
- Keep submitted build output unchanged, retain matching symbols, and attribute commands and evidence to their run. Publish completed files together and make repeated requests identify existing work.
- Cache loss should cause recomputation. Show storage use and offer cleanup/export; preserve active work and unuploaded evidence. Report partial results and full-disk failures honestly.
- Use ordinary Git/files for patch and evidence export where practical. Comprehensive backup machinery is not a prerequisite to a useful loop.
- Use rpm-ostree/OSTree first, with small OS-specific preparation/deployment modules over shared command and evidence code. Keep prepared software unchanged, run files disposable and persistence explicit; see [candidate deployment](candidate-deployment.md).
- One foreground controller process serves MCP over local Streamable HTTP, HTTPS and AsyncSSH reverse-direction connections. Human commands share its application behavior. No separate persistent MCP process, SSH daemon or worker service. Use child processes for commands/builds and keep blocking work out of the connection event loop.

Changes should leave fewer concepts and fewer sources of truth. A filename changing should not make every subsystem panic.

Keep controller network registration in `controller_mcp.py` and
`controller_http.py`; application operations are grouped in `controller.py`, `controller_commands.py`,
`investigations.py` and `storage.py`.
These are modules in one process, not additional services. Share completion rules
in `work_status.py`. Repeated status observations must not rewrite unchanged records
or advance their progress timestamp. Last contact is separate from work progress.

SQLite has one owner thread. Asynchronous calls share a lock; admission holds it
until all related records commit or roll back, before target dispatch. Status
queries select relevant records. Finished command metadata avoids target reads;
recent completed output pages are cached separately without claiming unread bytes
are available. Target action records use one JSON file per request under `actions/`;
heartbeats request only unresolved action IDs. No migration reads old `work.json`.
Use fresh development target state for that format change; do not erase owner data.

Operator failures go to a bounded rotating `logs/controller.log`, separately from
MCP activity and experiment evidence. Do not log credentials, command bodies or
environments. Failed diagnostic logging must not stop networking.
