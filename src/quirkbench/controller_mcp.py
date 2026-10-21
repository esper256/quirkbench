"""Local MCP and CLI HTTP registration. Application behavior lives in Controller."""
from contextlib import asynccontextmanager
import hmac
from typing import Any
from urllib.parse import urlparse
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route


def mcp_app(controller):
    mcp = FastMCP('Quirkbench', stateless_http=True, json_response=True,
                  transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                      allowed_hosts=['127.0.0.1:*', 'localhost:*'], allowed_origins=['http://127.0.0.1:*', 'http://localhost:*']))

    @mcp.tool(structured_output=True)
    async def investigation_context(investigation: str) -> dict[str, Any]:
        """Read authorized investigation and current target mode before acting."""
        return controller.context(investigation)

    @mcp.tool(structured_output=True)
    async def investigation_problem_update(investigation: str, name: str, description: str | None = None,
            status: str | None = None, references: list[str] | None = None) -> dict[str, Any]:
        """Add or update a named problem. Status: open, resolved or not-reproduced. References are optional run, file or related problem names."""
        return controller.problem_update(investigation, name, description, status, references)

    @mcp.tool(structured_output=True)
    async def investigation_control(investigation: str, action: str) -> dict[str, Any]:
        """pause stops new work, finishes bounded commands and returns to recovery. force-stop also stops active commands/preparation. resume permits new work; never replays or boots anything."""
        return await controller.investigation_control(investigation, action)

    @mcp.tool(structured_output=True)
    async def investigation_timeline(investigation: str, after: int = 0, limit: int = 200) -> dict[str, Any]:
        """Read controller-observed progress and durations. after is the last event cursor; external builds are not timed."""
        return controller.timeline(investigation, after, limit)

    @mcp.tool(structured_output=True)
    async def controller_sequence_start(investigation: str, request_id: str, steps: list[dict]) -> dict[str, Any]:
        """Execute ordered operations. Each step has name, tool, arguments and optional expected_exit_codes (default [0]). Reference an earlier result with {from_step: name, field: field}. Supported: rpm_ostree_prepare_candidate, recovery_deploy_ostree, recovery_boot_ostree, recovery_discard_ostree, recovery_return, sftp_upload/download, ssh_command_start/wait/stop. Start is immediate; every started command needs an explicit later wait or stop. Other steps finish before continuing. No loops, automatic retries or restart continuation."""
        return controller.sequences.start(investigation, request_id, steps)

    @mcp.tool(structured_output=True)
    async def controller_sequence_resume(work_id: str) -> dict[str, Any]:
        """Explicitly resume a paused sequence after inspecting its progress. Completed actions are not repeated; uncertain effects stop for attention."""
        return controller.sequences.resume(work_id)

    @mcp.tool(structured_output=True)
    async def controller_sequence_cancel(work_id: str) -> dict[str, Any]:
        """Cancel ordered operations and stop only their own commands/preparation. Inspect child work for cleanup or uncertain physical outcomes."""
        return await controller.sequences.cancel(work_id)

    @mcp.tool(structured_output=True)
    async def ssh_candidate_inventory(run_id: str) -> dict[str, Any]:
        """Inspect the connected candidate OS, kernel, build information, tools and storage. Availability does not guarantee a debugging feature works."""
        return await controller.candidate_inventory(run_id)

    @mcp.tool(structured_output=True)
    async def ssh_recovery_inspect(target: str, view: str, max_lines: int = 80) -> dict[str, Any]:
        """Read hardware, network, storage, processes, services (failed units only), boot-log, report or emergency (kernel memory; no subprocesses). No shell access."""
        return await controller.inspect(target, view, max_lines)

    @mcp.tool(structured_output=True)
    async def saved_output_read(file: str, offset: int = 0, limit: int = 8192) -> dict[str, Any]:
        """Read saved diagnostics by controller-relative path, with a byte cursor. Maximum 32 KiB per response."""
        return await controller.saved_output(file, offset, limit)

    @mcp.tool(structured_output=True)
    async def run_results(run_id: str) -> dict[str, Any]:
        """Show run outcome, evidence receipt, command results and saved files without database access."""
        return await controller.run_results(run_id)

    @mcp.tool(structured_output=True)
    async def sftp_upload(run_id: str, file: str, name: str) -> dict[str, Any]:
        """Upload a controller-local file into the connected candidate's /var/tmp/quirkbench. Name is a simple filename. Publish only after the transfer completes; replaces that name. Does not execute it. Recovery cannot accept agent files."""
        return await controller.upload_file(run_id, file, name)

    @mcp.tool(structured_output=True)
    async def sftp_download(run_id: str, file: str) -> dict[str, Any]:
        """Download a completed binary or text file relative to QUIRKBENCH_EVIDENCE_DIR. Finish and rename files into place first. Returns a controller file path, byte size and digest. Does not delete target evidence."""
        return await controller.download_file(run_id, file)

    @mcp.tool(structured_output=True)
    async def ssh_command_start(run_id: str, request_id: str, argv: list[str],
                                timeout_seconds: int, cwd: str = '/var/tmp', env: dict[str,str] | None = None,
                                max_output_bytes: int = 16777216) -> dict[str, Any]:
        """Start a candidate command as root. Explicit argv: no implicit shell. Each command has its own cwd/env; files and services are shared. Output is saved on target. Use ssh_command_wait/read/stop. Output defaults to 16 MiB; increase max_output_bytes explicitly if needed. No interactive input yet."""
        return await controller.command_start(run_id,request_id,argv,cwd,env or {},timeout_seconds,max_output_bytes)

    @mcp.tool(structured_output=True)
    async def ssh_command_read(command_id: str, offset: int = 0) -> dict[str, Any]:
        """Read up to 32 KiB of saved output; next_offset is a byte cursor. Does not repeat execution."""
        return await controller.command_read(command_id,offset)

    @mcp.tool(structured_output=True)
    async def ssh_command_wait(command_id: str, timeout_seconds: int = 25, offset: int = 0) -> dict[str, Any]:
        """Wait quietly for completion, connection loss or a wait deadline (maximum 50 seconds). Does not stop work."""
        return await controller.command_wait(command_id,timeout_seconds,offset)

    @mcp.tool(structured_output=True)
    async def ssh_command_stop(command_id: str) -> dict[str, Any]:
        """Stop this command and its process group without ending the candidate run."""
        return await controller.command_read(command_id,stop=True)

    @mcp.tool(structured_output=True)
    async def rpm_ostree_prepare_candidate(investigation: str, request_id: str,
            base: str = 'ghcr.io/ublue-os/bazzite:stable', name: str = 'baseline',
            packages: list[str] | None = None, replacements: list[str] | None = None,
            startup_probe: str | None = None, startup_probe_timeout_seconds: int = 60,
            repository_packages: list[str] | None = None, debugging_tools: bool = True) -> dict[str, Any]:
        """Prepare Bazzite with a small optional debugging toolkit, repository_packages from its own repositories, local RPM additions/replacements and an optional executable startup probe. Preparation can run while a target retains an earlier deployment. Paths are on the controller; files are captured before building. The resulting commit is unchanged; kernel replacements must be RPMs, not raw kernel images. Probe runs before networking with an explicit timeout (default 60 seconds); output goes to the boot journal. Kernel changes require a new boot. Wait with controller_work_wait."""
        return await controller.prepare(investigation, base, name, request_id, packages, replacements, startup_probe, startup_probe_timeout_seconds, repository_packages, debugging_tools)

    @mcp.tool(structured_output=True)
    async def recovery_deploy_ostree(investigation: str, candidate: str, request_id: str, kernel_args: list[str] | None = None) -> dict[str, Any]:
        """Transfer and prepare fresh run files, with optional debugging kernel_args retained on repeat boots. Boot/recovery arguments are managed by Quirkbench. Discard retained run files first if replacing them; preparation itself does not require discard. Does not select a boot or reboot. Inspect progress with controller_work_wait, then use recovery_boot_ostree."""
        return await controller.deploy(investigation, candidate, request_id, kernel_args)

    @mcp.tool(structured_output=True)
    async def recovery_boot_ostree(run_id: str, request_id: str, startup_connect_timeout_seconds: int = 300) -> dict[str, Any]:
        """Boot a prepared or retained OSTree deployment once. Persistent edits remain; plain ostree admin unlock /usr edits do not survive reboot. Repeat boots get a new run ID and never replay commands."""
        return await controller.retained_action(run_id, request_id, 'boot', startup_connect_timeout_seconds)

    @mcp.tool(structured_output=True)
    async def recovery_discard_ostree(run_id: str, request_id: str) -> dict[str, Any]:
        """Remove retained run files after diagnostics receipt, or discard an unbooted prepared run. Keep downloaded candidates, settings and controller evidence. Recovery only."""
        return await controller.retained_action(run_id, request_id, 'discard')

    @mcp.tool(structured_output=True)
    async def recovery_cancel_ostree(work_id: str) -> dict[str, Any]:
        """Cancel a recovery_deploy_ostree transfer. Returns promptly; controller_work_wait confirms cleanup. Disconnected targets receive cancellation on reconnect. After boot is committed use recovery_return."""
        return await controller.cancel_deployment(work_id)

    @mcp.tool(structured_output=True)
    async def recovery_return(run_id: str, request_id: str) -> dict[str, Any]:
        """Save boot diagnostics and return the connected candidate to recovery."""
        return await controller.return_recovery(run_id, request_id)

    @mcp.tool(structured_output=True)
    async def controller_work_wait(work_id: str, timeout_seconds: int = 25) -> dict[str, Any]:
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

