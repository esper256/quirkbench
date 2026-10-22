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
SequenceTool = Literal['rpm_ostree_prepare_candidate', 'recovery_deploy_ostree',
    'recovery_boot_ostree', 'recovery_discard_ostree', 'recovery_return',
    'sftp_upload', 'sftp_download', 'ssh_command_start', 'ssh_command_wait', 'ssh_command_stop']


class SequenceStep(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$')]
    tool: SequenceTool
    arguments: dict[str, Any] = Field(description='Arguments of the selected tool; use {"from_step":"earlier-name","field":"run_id"} for a prior result. Retry IDs are supplied by the sequence. Command starts default to no brief wait.')
    expected_exit_codes: Annotated[list[int], Field(min_length=1)] = [0]


class ActionResult(BaseModel):
    model_config = ConfigDict(extra='allow')
    status: str
    complete: bool = Field(description='The action has reached a terminal state; not a claim that evidence was received.')
    outcome: Literal['pending', 'succeeded', 'failed', 'stopped', 'cancelled', 'interrupted', 'unknown']
    needs_attention: bool
    next_tool: str | None = Field(default=None, description='Optional suggested action, not a required workflow step.')


class CommandResult(ActionResult):
    command_id: str



def mcp_app(controller):
    mcp = RecordedMCP(controller,'Quirkbench', stateless_http=True, json_response=True,
                  transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                      allowed_hosts=['127.0.0.1:*', 'localhost:*'], allowed_origins=['http://127.0.0.1:*', 'http://localhost:*']))

    @mcp.tool(structured_output=True)
    async def investigation_context(investigation: str) -> dict[str, Any]:
        """Read authorized investigation and current target mode before acting."""
        return controller.context(investigation)

    @mcp.tool(structured_output=True)
    async def investigation_runs(investigation: str, after: str | None = None, limit: PageSize = 20) -> dict[str, Any]:
        """Page through run summaries, newest first. Use next_cursor as after; omit after to see new runs. deployment_id identifies retained files; run_id identifies one boot attempt."""
        return controller.investigation_runs(investigation, after, limit)

    @mcp.tool(structured_output=True)
    async def controller_mcp_activity(investigation: str | None = None, after: Offset = 0, limit: PageSize = 100) -> dict[str, Any]:
        """Review retained MCP call timings, response sizes and unchanged reads/waits. Omit investigation to include unscoped calls and validation errors. No command text or output is copied into this log."""
        return activity(controller.store, investigation, after, limit)

    @mcp.tool(structured_output=True)
    async def investigation_problem_update(investigation: str, name: str, description: str | None = None,
            status: Literal['open', 'resolved', 'not-reproduced'] | None = None, references: list[str] | None = None) -> dict[str, Any]:
        """Add or update a named problem. Status: open, resolved or not-reproduced. References are optional run, file or related problem names."""
        return controller.problem_update(investigation, name, description, status, references)

    @mcp.tool(structured_output=True)
    async def investigation_control(investigation: str, action: Literal['pause', 'force-stop', 'resume']) -> dict[str, Any]:
        """pause stops new work, finishes bounded commands and returns to recovery. force-stop also stops active commands/preparation. resume permits new work; never replays or boots anything."""
        return await controller.investigation_control(investigation, action)

    @mcp.tool(structured_output=True)
    async def investigation_timeline(investigation: str, after: Offset = 0, limit: Annotated[int, Field(ge=1, le=1000)] = 200) -> dict[str, Any]:
        """Read controller-observed progress and durations. after is the last event cursor; external builds are not timed."""
        return controller.timeline(investigation, after, limit)

    @mcp.tool(structured_output=True)
    async def controller_sequence_start(investigation: str, request_id: RequestID, steps: Annotated[list[SequenceStep], Field(min_length=1, max_length=100)]) -> ActionResult:
        """Execute ordered operations. Each step has name, tool, arguments and optional expected_exit_codes (default [0]). Reference an earlier result with {from_step: name, field: field}. Supported: rpm_ostree_prepare_candidate, recovery_deploy_ostree, recovery_boot_ostree, recovery_discard_ostree, recovery_return, sftp_upload/download, ssh_command_start/wait/stop. Start is immediate; every started command needs an explicit later wait or stop. Other steps finish before continuing. No loops, automatic retries or restart continuation."""
        return controller.sequences.start(investigation, request_id, [step.model_dump() for step in steps])

    @mcp.tool(structured_output=True)
    async def controller_sequence_resume(work_id: str) -> ActionResult:
        """Explicitly resume a paused sequence after inspecting its progress. Completed actions are not repeated; uncertain effects stop for attention."""
        return controller.sequences.resume(work_id)

    @mcp.tool(structured_output=True)
    async def controller_sequence_cancel(work_id: str) -> ActionResult:
        """Cancel ordered operations and stop only their own commands/preparation. Inspect child work for cleanup or uncertain physical outcomes."""
        return await controller.sequences.cancel(work_id)

    @mcp.tool(structured_output=True)
    async def ssh_candidate_inventory(run_id: str) -> dict[str, Any]:
        """Inspect the connected candidate OS, kernel, build information, tools and storage. Availability does not guarantee a debugging feature works."""
        return await controller.candidate_inventory(run_id)

    @mcp.tool(structured_output=True)
    async def ssh_recovery_inspect(target: str, view: Literal['hardware', 'network', 'storage', 'processes', 'services', 'boot-log', 'report', 'emergency'], max_lines: Annotated[int, Field(ge=1, le=1000)] = 80) -> dict[str, Any]:
        """Read hardware, network, storage, processes, services (failed units only), boot-log, report or emergency (kernel memory; no subprocesses). No shell access."""
        return await controller.inspect(target, view, max_lines)

    @mcp.tool(structured_output=True)
    async def saved_output_read(saved_file: str, offset: Offset = 0, limit: Annotated[int, Field(ge=1, le=32768)] = 8192) -> dict[str, Any]:
        """Read saved diagnostics by controller-relative path, with a byte cursor. Maximum 32 KiB per response."""
        return await controller.saved_output(saved_file, offset, limit)

    @mcp.tool(structured_output=True)
    async def run_results(run_id: str) -> dict[str, Any]:
        """Show run outcome, evidence receipt, command results and saved files without database access."""
        return await controller.run_results(run_id)

    @mcp.tool(structured_output=True)
    async def sftp_upload(run_id: str, controller_path: str, target_name: str) -> dict[str, Any]:
        """Upload a controller-local file into the connected candidate's /var/tmp/quirkbench. Name is a simple filename. Publish only after the transfer completes; replaces that name. Does not execute it. Recovery cannot accept agent files."""
        return await controller.upload_file(run_id, controller_path, target_name)

    @mcp.tool(structured_output=True)
    async def sftp_download(run_id: str, evidence_file: str) -> dict[str, Any]:
        """Download a completed binary or text file relative to QUIRKBENCH_EVIDENCE_DIR. Finish and rename files into place first. Returns a controller file path, byte size and digest. Does not delete target evidence."""
        return await controller.download_file(run_id, evidence_file)

    @mcp.tool(structured_output=True)
    async def ssh_command_start(run_id: str, request_id: RequestID, timeout_seconds: PositiveSeconds,
                                argv: Annotated[list[str], Field(min_length=1)] | None = None, cwd: str = '/var/tmp', env: dict[str,str] | None = None,
                                max_output_bytes: Annotated[int, Field(gt=0)] = 16777216,
                                script: str | None = None, shell: str = '/bin/sh', wait_seconds: BriefWait = 0.5) -> CommandResult:
        """Start a candidate command as root. Provide exactly one of argv (no shell expansion) or script (executed by shell -c). A script starts a new shell, not a persistent session. timeout_seconds stops execution; wait_seconds only waits briefly before returning an ID or completed output. Each command has its own cwd/env; files and services are shared. Output is saved on target. Use ssh_command_wait/read/stop. Output defaults to 16 MiB; increase max_output_bytes explicitly if needed. No interactive input yet."""
        return await controller.command_start(run_id,request_id,argv,cwd,env or {},timeout_seconds,max_output_bytes,
                                              script=script, shell=shell, wait_seconds=wait_seconds)

    @mcp.tool(structured_output=True)
    async def ssh_command_read(command_id: str, offset: Offset = 0) -> CommandResult:
        """Read up to 32 KiB of saved output; next_offset is a byte cursor. Does not repeat execution."""
        return await controller.command_read(command_id,offset)

    @mcp.tool(structured_output=True)
    async def ssh_command_wait(command_id: str, timeout_seconds: WaitSeconds = 25, offset: Offset = 0) -> CommandResult:
        """Wait quietly for completion, connection loss or a wait deadline (maximum 50 seconds). Does not stop work."""
        return await controller.command_wait(command_id,timeout_seconds,offset)

    @mcp.tool(structured_output=True)
    async def ssh_command_stop(command_id: str) -> CommandResult:
        """Stop this command and its process group without ending the candidate run."""
        return await controller.command_read(command_id,stop=True)

    @mcp.tool(structured_output=True)
    async def rpm_ostree_prepare_candidate(investigation: str, request_id: RequestID,
            base: str = 'ghcr.io/ublue-os/bazzite:stable', name: str = 'baseline',
            packages: list[str] | None = None, replacements: list[str] | None = None,
            startup_probe: str | None = None, startup_probe_timeout_seconds: Annotated[int, Field(ge=1)] = 60,
            repository_packages: list[str] | None = None, debugging_tools: bool = True) -> ActionResult:
        """Prepare Bazzite with a small optional debugging toolkit, repository_packages from its own repositories, local RPM additions/replacements and an optional executable startup probe. Preparation can run while a target retains an earlier deployment. Paths are on the controller; files are captured before building. The resulting commit is unchanged; kernel replacements must be RPMs, not raw kernel images. Probe runs before networking with an explicit timeout (default 60 seconds); output goes to the boot journal. Kernel changes require a new boot. Wait with controller_work_wait."""
        return await controller.prepare(investigation, base, name, request_id, packages, replacements, startup_probe, startup_probe_timeout_seconds, repository_packages, debugging_tools)

    @mcp.tool(structured_output=True)
    async def recovery_deploy_ostree(investigation: str, candidate: str, request_id: RequestID, kernel_args: list[str] | None = None) -> ActionResult:
        """Transfer and prepare fresh run files, with optional debugging kernel_args retained on repeat boots. Boot/recovery arguments are managed by Quirkbench. Discard retained run files first if replacing them; preparation itself does not require discard. Does not select a boot or reboot. Inspect progress with controller_work_wait, then use recovery_boot_ostree."""
        return await controller.deploy(investigation, candidate, request_id, kernel_args)

    @mcp.tool(structured_output=True)
    async def recovery_boot_ostree(run_id: str, request_id: RequestID, startup_connect_timeout_seconds: Annotated[int, Field(ge=1, le=3600)] = 300) -> ActionResult:
        """Boot a prepared or retained OSTree deployment once. Persistent edits remain; plain ostree admin unlock /usr edits do not survive reboot. Repeat boots get a new run ID and never replay commands."""
        return await controller.retained_action(run_id, request_id, 'boot', startup_connect_timeout_seconds)

    @mcp.tool(structured_output=True)
    async def recovery_discard_ostree(run_id: str, request_id: RequestID) -> ActionResult:
        """Remove retained run files after diagnostics receipt, or discard an unbooted prepared run. Keep downloaded candidates, settings and controller evidence. Recovery only."""
        return await controller.retained_action(run_id, request_id, 'discard')

    @mcp.tool(structured_output=True)
    async def recovery_cancel_ostree(work_id: str) -> ActionResult:
        """Cancel a recovery_deploy_ostree transfer. Returns promptly; controller_work_wait confirms cleanup. Disconnected targets receive cancellation on reconnect. After boot is committed use recovery_return."""
        return await controller.cancel_deployment(work_id)

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
            return JSONResponse(await controller.local(request.path_params['action'], await request.json()))
        except (ValueError, TypeError) as e:
            return JSONResponse({'error': str(e)}, status_code=400)

    @asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run():
            yield

    app = Starlette(routes=[Route('/local/{action}', local_request, methods=['POST']),
                            Mount('/', app=mcp.streamable_http_app())], lifespan=lifespan)
    token = controller.token

    async def authenticated(scope, receive, send):
        if scope['type'] == 'http':
            headers = dict(scope['headers'])
            host = headers.get(b'host', b'').decode().split(':')[0]
            origin = headers.get(b'origin')
            valid_origin = not origin or urlparse(origin.decode()).hostname in ('127.0.0.1', 'localhost')
            if host not in ('127.0.0.1', 'localhost') or not valid_origin or not hmac.compare_digest(headers.get(b'authorization', b''), ('Bearer ' + token).encode()):
                return await JSONResponse({'error': 'Local controller access denied.'}, status_code=403)(scope, receive, send)
        await app(scope, receive, send)
    return authenticated

