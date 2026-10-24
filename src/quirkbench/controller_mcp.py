"""Local MCP and CLI HTTP registration. Application behavior lives in Controller."""

from contextlib import asynccontextmanager
import hmac
from typing import Any, Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field
from .mcp_activity import RecordedMCP, activity
from urllib.parse import urlparse
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route


RequestID = Annotated[str, Field(min_length=1, max_length=200)]
Offset = Annotated[int, Field(ge=0)]
WaitSeconds = Annotated[float, Field(ge=0, le=50, allow_inf_nan=False)]
BriefWait = Annotated[float, Field(ge=0, le=5, allow_inf_nan=False)]
PositiveSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]
PageSize = Annotated[int, Field(ge=1, le=100)]


class UploadFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    controller_path: str
    target_name: str


class ActionResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    status: str
    complete: bool = Field(
        description="The action has reached a terminal state; not a claim that evidence was received."
    )
    outcome: Literal[
        "pending", "succeeded", "failed", "stopped", "cancelled", "interrupted", "unknown"
    ]
    needs_attention: bool
    next_tool: str | None = Field(
        default=None, description="Optional suggested action, not a required workflow step."
    )


class CommandResult(ActionResult):
    command_id: str


def mcp_app(controller):
    mcp = RecordedMCP(
        controller,
        "Quirkbench",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*"],
        ),
    )

    @mcp.tool(structured_output=True)
    async def investigation_context(investigation: str) -> dict[str, Any]:
        """Read authorized investigation and current target mode before acting."""
        return await controller.context(investigation)

    @mcp.tool(structured_output=True)
    async def investigation_runs(
        investigation: str, after: str | None = None, limit: PageSize = 20
    ) -> dict[str, Any]:
        """Page through run summaries, newest first. Use next_cursor as after; omit after to see new runs. deployment_id identifies retained files; run_id identifies one boot attempt."""
        return await controller.investigation_runs(investigation, after, limit)

    @mcp.tool(structured_output=True)
    async def controller_mcp_activity(
        investigation: str | None = None, after: Offset = 0, limit: PageSize = 100
    ) -> dict[str, Any]:
        """Review retained MCP call timings, response sizes and unchanged reads/waits. Omit investigation to include unscoped calls and validation errors. No command text or output is copied into this log."""
        return await activity(controller.store, investigation, after, limit)

    @mcp.tool(structured_output=True)
    async def investigation_problem_update(
        investigation: str,
        name: str,
        description: str | None = None,
        status: Literal["open", "resolved", "not-reproduced"] | None = None,
        references: list[str] | None = None,
    ) -> dict[str, Any]:
        """Add or update a named problem. Status: open, resolved or not-reproduced. References are optional run, file or related problem names."""
        return await controller.problem_update(investigation, name, description, status, references)

    @mcp.tool(structured_output=True)
    async def investigation_control(
        investigation: str, action: Literal["pause", "force-stop", "resume"]
    ) -> dict[str, Any]:
        """pause stops new work, finishes bounded commands and returns to recovery. force-stop also stops active commands/preparation. resume permits new work; never replays or boots anything."""
        return await controller.investigation_control(investigation, action)

    @mcp.tool(structured_output=True)
    async def investigation_timeline(
        investigation: str, after: Offset = 0, limit: Annotated[int, Field(ge=1, le=1000)] = 200
    ) -> dict[str, Any]:
        """Read controller-observed progress and durations. after is the last event cursor; external builds are not timed."""
        return await controller.timeline(investigation, after, limit)

    @mcp.tool(structured_output=True)
    async def controller_candidate_cancel(work_id: str) -> ActionResult:
        """Cancel remaining preparation/deployment follow-up phases. A committed boot cannot be undone; inspect phase work for cleanup and outcome."""
        return await controller.followups.cancel(work_id)

    @mcp.tool(structured_output=True)
    async def ssh_incident_configure(
        run_id: str,
        argv: list[str],
        timeout_seconds: PositiveSeconds = 60,
        max_bytes: Annotated[int, Field(ge=1, le=1099511627776)] = 268435456,
    ) -> dict[str, Any]:
        """Configure an investigation-owned collector. It writes to its supplied QUIRKBENCH_EVIDENCE_DIR and closes writers before exit. Trigger separately; no diagnosis or automatic intervention."""
        from .incidents import configure

        return await configure(controller, run_id, argv, timeout_seconds, max_bytes)

    @mcp.tool(structured_output=True)
    async def ssh_incident_capture(
        run_id: str,
        request_id: RequestID,
        classification: Literal["observed", "injected", "unknown"] = "unknown",
        trigger: str = "agent",
    ) -> dict[str, Any]:
        """Run the configured bounded collector once and preserve an initial snapshot. Identical retries observe it, never replay uncertain collection. Human CLI uses the same operation."""
        from .incidents import capture

        return await capture(controller, run_id, request_id, classification, trigger)

    @mcp.tool(structured_output=True)
    async def ssh_incident_status(incident_id: str) -> dict[str, Any]:
        """Observe collection and retrieve completed snapshot/manifest. Receipt is separate from collection success and never deletes target files."""
        from .incidents import status

        return await status(controller, incident_id)

    @mcp.tool(structured_output=True)
    async def ssh_keep_awake(
        run_id: str,
        request_id: RequestID,
        action: Literal["start", "status", "release"],
        duration_seconds: PositiveSeconds | None = None,
    ) -> dict[str, Any]:
        """Opt-in bounded candidate sleep inhibitor. Blocks idle AND deliberate logind sleep; release before suspend tests. Allows screen blanking. No renewal after disconnect/restart; release only this command's inhibitor."""
        from .keep_awake import control

        return await control(controller, run_id, request_id, action, duration_seconds)

    @mcp.tool(structured_output=True)
    async def ssh_human_request(
        investigation: str,
        request_id: RequestID,
        action: Literal["prepare", "ready", "status", "cancel"],
        message: str | None = None,
        chime: bool = False,
        login_session: str | None = None,
        redeliver: bool = False,
    ) -> dict[str, Any]:
        """Coordinate one human action. Agent verifies collector readiness before ready; prepare does not display. The human responds in target TUI or controller CLI. Status reports response and whether it arrived late. Cancel before closing the collector; rearm with a new ID. Does not switch desktops or infer test success."""
        from .human_requests import request

        return await request(
            controller, investigation, request_id, action, message, chime, login_session, redeliver
        )

    @mcp.tool(structured_output=True)
    async def ssh_target_notify(
        investigation: str,
        message: Annotated[str, Field(min_length=1, max_length=500)],
        chime: bool = False,
    ) -> dict[str, Any]:
        """Show one human request on the target console. Optional chime plays once for half a second if an audio device is available. Never changes volume or mixers. No response means delivery is uncertain; do not repeat sound automatically."""
        inv = await controller.require_active(investigation)
        return await controller.rpc(inv["target"], "notify", message=message, chime=chime)

    @mcp.tool(structured_output=True)
    async def ssh_login_sessions(run_id: str) -> dict[str, Any]:
        """Inspect existing login sessions and sleep inhibitors. Select an active session ID for ssh_command_start(login_session=...). Does not switch VT, create users, inhibit sleep or start a desktop."""
        run, target = await controller.command_run(run_id)
        return await controller.rpc(
            run["target"], "login-sessions", run_id=run_id, boot_id=target["boot_id"]
        )

    @mcp.tool(structured_output=True)
    async def ssh_candidate_inventory(run_id: str) -> dict[str, Any]:
        """Inspect the connected candidate OS, kernel, build information, tools and storage. Availability does not guarantee a debugging feature works."""
        return await controller.candidate_inventory(run_id)

    @mcp.tool(structured_output=True)
    async def ssh_recovery_inspect(
        target: str,
        view: Literal[
            "hardware",
            "network",
            "storage",
            "processes",
            "services",
            "boot-log",
            "report",
            "emergency",
            "console",
        ],
        max_lines: Annotated[int, Field(ge=1, le=1000)] = 80,
    ) -> dict[str, Any]:
        """Read hardware, network, storage, processes, services (failed units only), boot-log, report, console (last rendered TUI, not external programs) or emergency (kernel memory; no subprocesses). No shell access."""
        return await controller.inspect(target, view, max_lines)

    @mcp.tool(structured_output=True)
    async def saved_output_read(
        saved_file: str,
        offset: Offset = 0,
        limit: Annotated[int, Field(ge=1, le=32768)] = 8192,
        tail: bool = False,
    ) -> dict[str, Any]:
        """Read saved diagnostics by controller-relative path, with a byte cursor. Maximum 32 KiB per response."""
        return await controller.saved_output(saved_file, offset, limit, tail)

    @mcp.tool(structured_output=True)
    async def run_results(
        run_id: str,
        view: Literal["summary", "files", "commands", "transfers", "collections"] = "summary",
        offset: Offset = 0,
        limit: PageSize = 50,
    ) -> dict[str, Any]:
        """Read a compact outcome and receipt summary. Select a view for paginated details; next_offset continues that view. Files can grow until final receipt. Read files with saved_output_read."""
        return await controller.run_results(run_id, view, offset, limit)

    @mcp.tool(structured_output=True)
    async def sftp_upload_batch(
        run_id: str, files: Annotated[list[UploadFile], Field(min_length=1, max_length=32)]
    ) -> dict[str, Any]:
        """Upload files in order to one candidate boot. Stop on failure and report each file as uploaded, failed or not-attempted. Earlier files remain; a batch is not atomic. Nothing is executed."""
        return await controller.upload_files(run_id, [f.model_dump() for f in files])

    @mcp.tool(structured_output=True)
    async def sftp_upload(run_id: str, controller_path: str, target_name: str) -> dict[str, Any]:
        """Upload a controller-local file into the connected candidate's /var/tmp/quirkbench. Name is a simple filename. Publish only after the transfer completes; replaces that name. Does not execute it. Recovery cannot accept agent files."""
        return await controller.upload_file(run_id, controller_path, target_name)

    @mcp.tool(structured_output=True)
    async def sftp_download(run_id: str, evidence_file: str) -> dict[str, Any]:
        """Download a completed binary or text file relative to QUIRKBENCH_EVIDENCE_DIR. Finish and rename files into place first. Returns a controller file path, byte size and digest. Does not delete target evidence."""
        return await controller.download_file(run_id, evidence_file)

    @mcp.tool(structured_output=True)
    async def ssh_command_start(
        run_id: str,
        request_id: RequestID,
        timeout_seconds: PositiveSeconds,
        argv: Annotated[list[str], Field(min_length=1)] | None = None,
        cwd: str = "/var/tmp",
        env: dict[str, str] | None = None,
        max_output_bytes: Annotated[int, Field(gt=0)] = 16777216,
        script: str | None = None,
        shell: str = "/bin/sh",
        wait_seconds: BriefWait = 0.5,
        stop_grace_seconds: Annotated[float, Field(ge=0, le=300)] = 10,
        interactive: bool = False,
        description: Annotated[str, Field(min_length=1, max_length=120)] | None = None,
        login_session: str | None = None,
    ) -> CommandResult:
        """Start a candidate command as root, or as the explicitly selected login_session user. Session execution uses its user-manager environment and separate user-owned evidence/work directories; no desktop is started. Optional description is a short human-readable intent shown in the controller; omit secrets. Provide exactly one of argv (no shell expansion) or script (executed by shell -c). A script starts a new shell, not a persistent session. timeout_seconds stops execution; wait_seconds only waits briefly before returning an ID or completed output. Each command has its own cwd/env; files and services are shared. Output is saved on target. Use ssh_command_wait/read/stop. Output defaults to 16 MiB; increase max_output_bytes explicitly if needed. interactive allocates a terminal; send input and resize with ssh_terminal_input. Each terminal is independent. Stop allows stop_grace_seconds for cleanup before SIGKILL."""
        return await controller.command_start(
            run_id,
            request_id,
            argv,
            cwd,
            env or {},
            timeout_seconds,
            max_output_bytes,
            script=script,
            shell=shell,
            wait_seconds=wait_seconds,
            stop_grace_seconds=stop_grace_seconds,
            interactive=interactive,
            description=description,
            login_session=login_session,
        )

    @mcp.tool(structured_output=True)
    async def ssh_terminal_input(
        command_id: str,
        text: Annotated[str, Field(max_length=8192)] = "",
        rows: Annotated[int, Field(ge=1, le=1000)] | None = None,
        columns: Annotated[int, Field(ge=1, le=1000)] | None = None,
    ) -> CommandResult:
        """Send terminal text (include newline to enter a line), control characters or resize. Input is not retried or replayed; a lost reply means delivery is uncertain. Use command read for output. Ctrl-D sends EOF when supported by the program."""
        return await controller.command_input(command_id, text, rows, columns)

    @mcp.tool(structured_output=True)
    async def ssh_command_read(command_id: str, offset: Offset = 0) -> CommandResult:
        """Read up to 32 KiB of saved output; next_offset is a byte cursor. Does not repeat execution."""
        return await controller.command_read(command_id, offset)

    @mcp.tool(structured_output=True)
    async def ssh_command_wait(
        command_id: str,
        timeout_seconds: WaitSeconds = 25,
        offset: Offset = 0,
        reconnect: bool = False,
    ) -> CommandResult:
        """Wait quietly for completion or a wait deadline (maximum 50 seconds). By default return on connection loss; reconnect=true waits for observation to resume within the same deadline. Invalid observations need attention; no command is replayed or stopped."""
        return await controller.command_wait(command_id, timeout_seconds, offset, reconnect)

    @mcp.tool(structured_output=True)
    async def ssh_command_stop(command_id: str) -> CommandResult:
        """Stop this command and its process group without ending the candidate run."""
        return await controller.command_read(command_id, stop=True)

    @mcp.tool(structured_output=True)
    async def rpm_ostree_prepare_candidate(
        investigation: str,
        request_id: RequestID,
        base: str = "ghcr.io/ublue-os/bazzite:stable",
        name: str = "baseline",
        packages: list[str] | None = None,
        replacements: list[str] | None = None,
        startup_probe: str | None = None,
        startup_probe_timeout_seconds: Annotated[int, Field(ge=1)] = 60,
        repository_packages: list[str] | None = None,
        debugging_tools: bool = True,
        removals: list[str] | None = None,
        after_prepare: Literal["stop", "deploy", "boot"] = "stop",
        kernel_args: list[str] | None = None,
        selinux: Literal["disabled", "permissive", "enforcing"] = "disabled",
        startup_connect_timeout_seconds: Annotated[int, Field(ge=1, le=3600)] = 300,
    ) -> ActionResult:
        """Prepare Bazzite with a small optional debugging toolkit, repository_packages from its own repositories, local RPM additions/replacements, explicit base-package removals and an optional executable startup probe. Removals and replacements are solved together; dependencies are never bypassed. Preparation can run while a target retains an earlier deployment. Paths are on the controller; files are captured before building. The resulting commit is unchanged; kernel replacements must be RPMs, not raw kernel images. Probe runs before networking with an explicit timeout (default 60 seconds); output goes to the boot journal. Kernel changes require a new boot. Wait with controller_work_wait."""
        return await controller.prepare(
            investigation,
            base,
            name,
            request_id,
            packages,
            replacements,
            startup_probe,
            startup_probe_timeout_seconds,
            repository_packages,
            debugging_tools,
            removals,
            after_prepare,
            kernel_args,
            selinux,
            startup_connect_timeout_seconds,
        )

    @mcp.tool(structured_output=True)
    async def recovery_deploy_ostree(
        investigation: str,
        candidate: str,
        request_id: RequestID,
        kernel_args: list[str] | None = None,
        selinux: Literal["disabled", "permissive", "enforcing"] = "disabled",
        boot: bool = False,
        startup_connect_timeout_seconds: Annotated[int, Field(ge=1, le=3600)] = 300,
    ) -> ActionResult:
        """Transfer and prepare fresh run files, with debugging kernel_args and explicit SELinux policy retained on repeat boots. Default disabled differs from a normal Bazzite install. Permissive/enforcing require a correctly labelled candidate and its SELinux policy; boot/connectivity must be checked. Boot/recovery arguments are managed by Quirkbench. Discard retained run files first if replacing them; preparation itself does not require discard. Stops before boot by default. Set boot=true for one boot after successful deployment; failures stop progression. Inspect each phase with controller_work_wait. Standalone recovery_boot_ostree remains available."""
        return await controller.deploy(
            investigation,
            candidate,
            request_id,
            kernel_args,
            selinux,
            boot,
            startup_connect_timeout_seconds,
        )

    @mcp.tool(structured_output=True)
    async def recovery_boot_ostree(
        run_id: str,
        request_id: RequestID,
        startup_connect_timeout_seconds: Annotated[int, Field(ge=1, le=3600)] = 300,
    ) -> ActionResult:
        """Boot a prepared or retained OSTree deployment once. Persistent edits remain; plain ostree admin unlock /usr edits do not survive reboot. Repeat boots get a new run ID and never replay commands."""
        return await controller.retained_action(
            run_id, request_id, "boot", startup_connect_timeout_seconds
        )

    @mcp.tool(structured_output=True)
    async def recovery_discard_ostree(run_id: str, request_id: RequestID) -> ActionResult:
        """Remove retained run files after diagnostics receipt, or discard an unbooted prepared run. Keep downloaded candidates, settings and controller evidence. Recovery only."""
        return await controller.retained_action(run_id, request_id, "discard")

    @mcp.tool(structured_output=True)
    async def recovery_cancel_ostree(work_id: str) -> ActionResult:
        """Cancel a recovery_deploy_ostree transfer. Returns promptly; controller_work_wait confirms cleanup. Disconnected targets receive cancellation on reconnect. After boot is committed use recovery_return."""
        return await controller.cancel_deployment(work_id)

    @mcp.tool(structured_output=True)
    async def recovery_prune_ostree_cache(
        investigation: str, request_id: RequestID
    ) -> ActionResult:
        """Explicitly remove downloaded candidate cache in idle recovery after retained deployment discard. Keeps settings and evidence. Downloads will be needed again. Follow with controller_work_wait."""
        return await controller.prune_target_cache(investigation, request_id)

    @mcp.tool(structured_output=True)
    async def recovery_return(run_id: str, request_id: RequestID) -> ActionResult:
        """Save boot diagnostics and return the connected candidate to recovery."""
        return await controller.return_recovery(run_id, request_id)

    @mcp.tool(structured_output=True)
    async def controller_work_wait(work_id: str, timeout_seconds: WaitSeconds = 25) -> ActionResult:
        """Wait for existing work, without starting or cancelling it. A timeout returns current progress."""
        return await controller.wait(work_id, timeout_seconds)

    async def local_request(request):
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object.")
            return JSONResponse(await controller.local(request.path_params["action"], data))
        except (ValueError, TypeError, KeyError) as e:
            controller.log.warning("Invalid local request: %s", type(e).__name__)
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as error:
            controller.log.error("Local request failed: %s", type(error).__name__)
            return JSONResponse(
                {"error": "Controller request failed; inspect controller.log."}, status_code=500
            )

    @asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run():
            yield

    app = Starlette(
        routes=[
            Route("/local/{action}", local_request, methods=["POST"]),
            Mount("/", app=mcp.streamable_http_app()),
        ],
        lifespan=lifespan,
    )
    token = controller.token

    async def authenticated(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            host = headers.get(b"host", b"").decode().split(":")[0]
            origin = headers.get(b"origin")
            valid_origin = not origin or urlparse(origin.decode()).hostname in (
                "127.0.0.1",
                "localhost",
            )
            if (
                host not in ("127.0.0.1", "localhost")
                or not valid_origin
                or not hmac.compare_digest(
                    headers.get(b"authorization", b""), ("Bearer " + token).encode()
                )
            ):
                return await JSONResponse(
                    {"error": "Local controller access denied."}, status_code=403
                )(scope, receive, send)
        await app(scope, receive, send)

    return authenticated
