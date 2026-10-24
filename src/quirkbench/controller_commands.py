"""Candidate commands and file transfers through the connected target."""

import asyncssh
from .errors import RequestRejected
from .files import disk, identifier, contained, read_json
from .store import new_id
from .work_status import COMMAND_DONE, completion, command_finished


class CommandControl:
    async def command_run(self, run_id):
        run = await self.store.get("run", run_id)
        inv = await self.store.get("investigation", run["investigation"])
        target = self.targets.get(run["target"], {})
        if (
            not inv["authorized"]
            or run["status"] != "running"
            or target.get("mode") != "candidate"
            or target.get("run_id") != run_id
        ):
            raise ValueError("Connect the authorized candidate run before executing commands.")
        return run, target

    async def upload_file(self, run_id, file, name):
        from .preparation import capture_inputs
        from .transfer import upload

        run, target = await self.command_run(run_id)
        identifier(name)
        self.require_operation(run["target"], "sftp-upload")
        connection = self.connections[run["target"]]
        async with self.candidate_files_lock:
            captured = await disk(capture_inputs, self.root, [], [], file)
            item = captured["startup_probe"]
            _, current = await self.command_run(run_id)
            if (
                current["boot_id"] != target["boot_id"]
                or self.connections.get(run["target"]) is not connection
            ):
                raise ValueError(
                    "Candidate connection changed while capturing the file; retry after inspecting context."
                )
            await upload(connection, self.root / "inputs" / item["sha256"], name)
            current, connected = await self.command_run(run_id)
            if connected["boot_id"] != target["boot_id"]:
                raise ValueError(
                    "Candidate changed boot during transfer; inspect it before executing the file."
                )
            (
                await self.store.put(
                    "transfer",
                    new_id("transfer"),
                    run_id=run_id,
                    boot_id=target["boot_id"],
                    name=name,
                    sha256=item["sha256"],
                )
            )
        return {
            "run_id": run_id,
            "boot_id": target["boot_id"],
            "target_path": "/var/tmp/quirkbench/" + name,
            "sha256": item["sha256"],
            "next_tool": "ssh_command_start",
            "instructions": "File uploaded, not executed. Use chmod if executable permission is needed. Files survive repeat boots until deployment discard.",
        }

    async def upload_files(self, run_id, files):
        if not isinstance(files, list) or not 1 <= len(files) <= 32:
            raise ValueError("Supply 1–32 files.")
        names = [identifier(item["target_name"]) for item in files]
        if len(set(names)) != len(names):
            raise ValueError("Each target_name must be unique within the batch.")
        _, initial = await self.command_run(run_id)
        results = []
        for item in files:
            try:
                _, current = await self.command_run(run_id)
                if current["boot_id"] != initial["boot_id"]:
                    raise ValueError("Candidate boot changed during batch upload.")
                result = await self.upload_file(
                    run_id, item["controller_path"], item["target_name"]
                )
                results.append(dict(result, status="uploaded"))
            except (ValueError, OSError, asyncssh.Error) as error:
                results.append(
                    dict(target_name=item["target_name"], status="failed", error=str(error))
                )
                # Earlier files stay published; never imply a batch is atomic.
                break
        results += [
            dict(target_name=item["target_name"], status="not-attempted")
            for item in files[len(results) :]
        ]
        return dict(
            run_id=run_id, files=results, complete=all(r["status"] == "uploaded" for r in results)
        )

    async def download_file(self, run_id, file):
        from .transfer import download

        run, target = await self.command_run(run_id)
        self.require_operation(run["target"], "sftp-download")
        connection = self.connections[run["target"]]
        destination = contained(self.root / "results" / run_id / "downloads" / new_id("file"), file)

        async def check_connection():
            _, current = await self.command_run(run_id)
            if (
                current["boot_id"] != target["boot_id"]
                or self.connections.get(run["target"]) is not connection
            ):
                raise ValueError(
                    "Candidate connection changed during download; retry after inspecting context."
                )

        result = await download(connection, file, destination, check_connection)
        return dict(
            result,
            run_id=run_id,
            boot_id=target["boot_id"],
            controller_path=str(destination),
            saved_file=destination.relative_to(self.root).as_posix(),
            instructions="Local copy ready. Target evidence remains until final recovery upload is confirmed.",
        )

    async def command_start(
        self,
        run_id,
        request_id,
        argv,
        cwd,
        env,
        timeout_seconds,
        max_output_bytes,
        *,
        script=None,
        shell="/bin/sh",
        wait_seconds=0,
        stop_grace_seconds=10,
        interactive=False,
        description=None,
        login_session=None,
    ):
        if description is not None and (
            not isinstance(description, str)
            or not 1 <= len(description) <= 120
            or not description.isprintable()
        ):
            raise ValueError(
                "Command description must be 1–120 printable characters; omit secrets."
            )
        if not 0 <= wait_seconds <= 5:
            raise ValueError("Brief start wait must be between 0 and 5 seconds.")
        if (argv is None) == (script is None):
            raise ValueError("Provide exactly one of argv or script.")
        if script is not None:
            if not isinstance(script, str) or not script.strip():
                raise ValueError("Provide a nonempty script.")
            argv = [shell, "-c", script]
        elif shell != "/bin/sh":
            raise ValueError("shell applies only to script; argv executes exactly as supplied.")
        run, target = await self.command_run(run_id)
        inputs = dict(
            argv=argv,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            stop_grace_seconds=stop_grace_seconds,
            interactive=interactive,
        )
        if login_session is not None:
            self.require_operation(run["target"], "login-sessions")
            inputs["login_session"] = login_session
        async with self.store.admission():
            await self.require_active(run["investigation"])
            work, fresh = await self.store.request(
                "ssh-command", run_id, request_id, {**inputs, "description": description}
            )
            if fresh:
                (await self.update_work(work["id"], run_id=run_id, boot_id=target["boot_id"]))
        if work.get("boot_id", target["boot_id"]) != target["boot_id"]:
            raise ValueError("The command belongs to an earlier boot and will not be replayed.")
        if work.get("not_started"):
            raise RequestRejected(work["error"], not_started=True)
        try:
            result = await self.rpc(
                run["target"],
                "command-start",
                run_id=run_id,
                boot_id=target["boot_id"],
                command_id=work["id"],
                **inputs,
            )
            return (
                await self.command_wait(work["id"], wait_seconds)
                if wait_seconds
                else self.command_result(result)
            )
        except RequestRejected as error:
            if error.not_started:
                (
                    await self.update_work(
                        work["id"], status="failed", not_started=True, error=str(error)
                    )
                )
            raise

    @staticmethod
    def command_result(result):
        result = completion(result, command=True)
        if "output_bytes" in result and "next_offset" in result:
            result["more"] = result["next_offset"] < result["output_bytes"]
        result["next_tool"] = (
            "ssh_command_read"
            if result.get("more")
            else "ssh_command_wait"
            if not result["complete"]
            else None
        )
        return result

    async def command_read(self, command_id, offset=0, stop=False, status_only=False):
        return self.command_result(await self._command_read(command_id, offset, stop, status_only))

    async def _command_read(self, command_id, offset=0, stop=False, status_only=False):
        if not stop and not status_only and (command_id, offset) in self.command_pages:
            return dict(self.command_pages[command_id, offset])
        work = await self.store.get("work", command_id)
        if work["action"] != "ssh-command":
            raise ValueError("Not an SSH command ID.")
        if work.get("not_started"):
            return dict(
                command_id=command_id, status="failed", error=work["error"], not_started=True
            )
        if status_only and command_finished(work) and "exit_code" in work:
            return dict(work, command_id=command_id)
        if stop:
            (await self.update_work(command_id, stop_requested=True))
        run = await self.store.get("run", work["run_id"])
        target = self.targets.get(run["target"])
        if (
            run["target"] in self.connections
            and target
            and target.get("boot_id") == work["boot_id"]
            and target.get("mode") == "candidate"
        ):
            try:
                result = await self.rpc(
                    run["target"],
                    "command-stop" if stop else "command-status" if status_only else "command-read",
                    run_id=run["id"],
                    boot_id=work["boot_id"],
                    command_id=command_id,
                    offset=offset,
                )
            except (asyncssh.Error, OSError, TimeoutError):
                pass  # Lost transport is not proof of process completion.
            else:
                if result["status"] in COMMAND_DONE:
                    (await self.update_work(command_id, stop_requested=False))
                (
                    await self.update_work(
                        command_id,
                        **{
                            k: v
                            for k, v in result.items()
                            if k
                            in (
                                "status",
                                "exit_code",
                                "output_bytes",
                                "last_activity",
                                "saving_pending",
                                "saving_error",
                                "error",
                            )
                        },
                    )
                )
                if not status_only and command_finished(result) and not stop:
                    if len(self.command_pages) >= 128:
                        self.command_pages.pop(next(iter(self.command_pages)))
                    self.command_pages[command_id, offset] = dict(result)
                return result
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("Output offset must be nonnegative.")
        folder = self.root / "results" / run["id"] / "commands"

        def read_saved():
            saved = read_json(folder / (command_id + ".json"), {})
            path = folder / (command_id + ".txt")
            if not path.exists():
                return saved, b"", 0
            with path.open("rb") as stream:
                stream.seek(offset)
                return (
                    saved,
                    b"" if status_only else stream.read(32768),
                    __import__("os").fstat(stream.fileno()).st_size,
                )

        saved, output, output_bytes = await disk(read_saved)
        known = target or (await self.store.get("target", run["target"]))
        observed_boot = known.get("boot_id") or known.get("last_boot_id")
        new_boot = observed_boot is not None and observed_boot != work["boot_id"]
        status = (
            saved.get("status")
            if saved.get("status") in ("finished", "failed", "timed-out", "stopped", "interrupted")
            else ("interrupted" if new_boot or run.get("evidence") else "connection-lost")
        )
        if saved or new_boot:
            (
                await self.update_work(
                    command_id,
                    status=status,
                    exit_code=saved.get("exit_code"),
                    output_bytes=output_bytes,
                    saving_pending=saved.get("saving_pending", False),
                    error=saved.get("error"),
                    stop_requested=False
                    if status in COMMAND_DONE
                    else work.get("stop_requested", False),
                )
            )
        return dict(
            command_id=command_id,
            run_id=run["id"],
            boot_id=work["boot_id"],
            status=status,
            output=output.decode(errors="replace"),
            offset=offset,
            next_offset=offset + len(output),
            output_bytes=output_bytes,
            message="Connection lost; command outcome is unknown. It will not be replayed."
            if status == "connection-lost"
            else "Reading saved command evidence; a new boot ends earlier commands.",
            evidence="received" if run.get("evidence") else "pending",
            next_tool="ssh_command_wait" if status == "connection-lost" else "ssh_command_read",
            **({"exit_code": saved["exit_code"]} if "exit_code" in saved else {}),
        )

    async def command_input(self, command_id, text="", rows=None, columns=None):
        work = await self.store.get("work", command_id)
        if work["action"] != "ssh-command":
            raise ValueError("Not an SSH command ID.")
        run, target = await self.command_run(work["run_id"])
        if target["boot_id"] != work["boot_id"]:
            raise ValueError("Command belongs to an earlier boot. Input will not be replayed.")
        result = await self.rpc(
            run["target"],
            "command-input",
            run_id=run["id"],
            boot_id=work["boot_id"],
            command_id=command_id,
            text=text,
            rows=rows,
            columns=columns,
        )
        return self.command_result(result)

    async def command_wait(self, command_id, timeout_seconds=25, offset=0):
        if not 0 <= timeout_seconds <= 50:
            raise ValueError(
                "Wait timeout must be between 0 and 50 seconds; it does not stop the command."
            )
        work = await self.store.get("work", command_id)
        run = await self.store.get("run", work["run_id"])
        target = self.targets.get(run["target"], {})
        if (
            not command_finished(work)
            and target.get("boot_id") == work["boot_id"]
            and run["target"] in self.connections
        ):
            try:
                await self.rpc(
                    run["target"],
                    "command-wait",
                    run_id=run["id"],
                    boot_id=work["boot_id"],
                    command_id=command_id,
                    seconds=timeout_seconds,
                )
            except (asyncssh.Error, OSError, TimeoutError):
                pass
        return await self.command_read(command_id, offset)
