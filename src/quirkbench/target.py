"""Target-side state machine with a crash-safe execution journal and outbox."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import signal
import tempfile
import time
from typing import Callable, Protocol
import uuid

from .contracts import CapabilityReport, Experiment, Outcome, Progress, Result, canonical, digest, identifier


from .deployment import BootControl, DeploymentBackend, DeploymentManifest


@dataclass(frozen=True)
class EvidenceChunk:
    """A sealed observation yielded by a streaming recipe before its final output."""
    stream: str
    data: bytes


@dataclass(frozen=True)
class RecipeOutput:
    outcome: str
    summary: str
    evidence: dict[str, bytes] = field(default_factory=dict)
    measurements: dict = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)


def smoke(experiment: Experiment) -> RecipeOutput:
    """A protocol demonstration, explicitly making no Linux-kernel claim."""
    observation = canonical({
        "schema_version": 1,
        "experiment_id": experiment.experiment_id,
        "recipe": "smoke",
        "observation": "The target accepted and executed the demo recipe.",
        "kernel_tested": False,
    })
    return RecipeOutput(
        outcome=Outcome.INCONCLUSIVE,
        summary="Demo smoke recipe completed; no kernel behavior was tested.",
        evidence={"demo_observation": observation},
        limitations=["This is a protocol demonstration, not a kernel experiment."],
    )


def _recipe_child(conn, recipe, experiment):
    os.setsid()
    try:
        result = recipe(experiment)
        if isinstance(result, RecipeOutput):
            conn.send(("ok", result))
        else:
            for item in result:
                if isinstance(item, EvidenceChunk):
                    conn.send(("chunk", item))
                elif isinstance(item, RecipeOutput):
                    conn.send(("ok", item))
                    return
                else:
                    raise ValueError("invalid streaming recipe item")
            raise ValueError("streaming recipe omitted terminal output")
    except BaseException as exc:
        conn.send(("error", type(exc).__name__))
    finally:
        conn.close()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class TargetAgent:
    """One step claims at most one attempt and drains its durable outbox.

    Recipe functions run only from an explicit local registry. On a restart
    after execution was marked started, the agent emits NEEDS_HUMAN rather
    than invoking a recipe a second time.
    """

    def __init__(
        self,
        client,
        state_dir: str | Path,
        report: CapabilityReport,
        *,
        recipes: dict[str, Callable[[Experiment], RecipeOutput]] | None = None,
        boot_control: BootControl | None = None,
        deployment_backend: DeploymentBackend | None = None,
        supervisor=None,
        library_store=None,
        finish_upload_s: float = 30,

    ):
        if report.device_id != client.device_id:
            raise ValueError("target report and client device differ")
        if (boot_control is None or deployment_backend is None) and any(cap in report.capabilities for cap in ("candidate_boot", "kernel_boot", "boot_control", "deployment_boot", "deployment.ostree.v1")):
            raise ValueError("boot capability unavailable until boot cycle is implemented")
        if finish_upload_s <= 0 or finish_upload_s > 120:
            raise ValueError("finish upload budget must be in (0,120]")
        self.library_store = library_store
        self.deployment_backend = deployment_backend
        self.supervisor = supervisor
        self.finish_upload_s = finish_upload_s
        self.client = client
        self.report = report
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_dir, 0o700)
        self.journal_path = self.state_dir / "journal.json"
        self.lock_path = self.state_dir / "agent.lock"
        self.blob_dir = self.state_dir / "blobs"
        self.blob_dir.mkdir(exist_ok=True, mode=0o700)
        os.chmod(self.blob_dir, 0o700)
        self.boot_control = boot_control
        self.recipes = dict(recipes or {})
        if report.mode == "simulation":
            self.recipes.setdefault("smoke", smoke)
        self._journal = self._load()
        if self._journal.get("device_id") != report.device_id:
            raise ValueError("journal belongs to another device")

    def _load(self) -> dict:
        if not self.journal_path.exists():
            value = {"schema_version": 1, "device_id": self.report.device_id, "pending": None, "claim_request_id": None}
            _atomic(self.journal_path, canonical(value))
            return value
        value = json.loads(self.journal_path.read_bytes())
        if value.get("schema_version") != 1 or "pending" not in value:
            raise ValueError("invalid target journal")
        value.setdefault("claim_request_id", None)
        return value

    def _save(self) -> None:
        _atomic(self.journal_path, canonical(self._journal))

    def _set_pending(self, pending: dict | None) -> None:
        self._journal["pending"] = pending
        self._save()

    def _record_output(self, pending: dict, output: RecipeOutput) -> None:
        if output.outcome not in [x.value for x in Outcome]:
            raise ValueError("recipe gave invalid outcome")
        for stream, raw in output.evidence.items():
            self._seal(pending, stream, raw)
        evidence_records = pending["evidence"]
        result = Result(
            attempt_id=pending["attempt_id"], outcome=output.outcome, summary=output.summary,
            evidence=[record["sha256"] for record in evidence_records],
            measurements=output.measurements, limitations=output.limitations,
        )
        pending["result"] = asdict(result)
        pending["evidence"] = evidence_records
        pending["stage"] = "observed"
        self._save()
        self._progress(pending, "experiment", "Recipe finished; observations sealed locally.",
            timeout_s=min(604800, max(1, int(pending["experiment"]["timeout_s"]))),
            completed=len(pending["evidence"]), state="COMPLETE")

    def _seal(self, pending: dict, stream: str, raw: bytes) -> None:
        identifier(stream)
        if not isinstance(raw, bytes) or len(raw) > 128 * 1024 * 1024:
            raise ValueError("evidence stream must be bytes of at most 128 MiB")
        checksum = digest(raw)
        _atomic(self.blob_dir / checksum, raw)
        pending["evidence"].append({
            "stream": stream, "sequence": len(pending["evidence"]),
            "sha256": checksum, "size": len(raw), "uploaded_offset": 0,
            "evidence_acked": False,
        })
        self._save()

    def _uncertain(self, pending: dict) -> None:
        self._record_output(pending, RecipeOutput(
            Outcome.NEEDS_HUMAN,
            "Target restarted after execution intent; whether the recipe ran or completed is unknown.",
            limitations=["Execution was not repeated after a target restart."],
        ))

    def _drain(self, pending: dict, *, finish=True, deadline=None) -> None:
        attempt_id = pending["attempt_id"]
        token = pending["token"]
        boot_id = pending["boot_id"]
        def check_budget():
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("bounded upload window elapsed")
            if self.supervisor:
                self.supervisor.pulse(waiting=True,
                    pending_bytes=sum(r["size"] for r in pending["evidence"] if not r["evidence_acked"]),
                    acknowledged_bytes=sum(r["size"] for r in pending["evidence"] if r["evidence_acked"]))
        for record in pending["evidence"]:
            check_budget()
            if not finish and record["evidence_acked"]:
                continue
            raw = (self.blob_dir / record["sha256"]).read_bytes()
            if digest(raw) != record["sha256"] or len(raw) != record["size"]:
                raise ValueError("spooled evidence corrupted")
            upload_id = f"{attempt_id}.{record['sequence']}"
            rewinds = 0
            # Probe even after a local upload ack: a controller restored from an
            # older backup may have lost both partial bytes and evidence refs.
            if record["uploaded_offset"] > 0 or len(raw) == 0:
                probe = self.client.upload(
                    attempt_id, token, boot_id, upload_id, 0, raw[:256 * 1024],
                    record["sha256"], len(raw),
                )
                offset = probe.get("offset") if isinstance(probe, dict) else None
                if type(offset) is not int or not 0 <= offset <= len(raw):
                    raise ValueError("invalid upload offset probe")
                if offset == len(raw) and probe.get("complete") is not True:
                    raise ValueError("upload completion was not acknowledged")
                record["uploaded_offset"] = offset
                self._save()
            while record["uploaded_offset"] < len(raw):
                check_budget()
                offset = record["uploaded_offset"]
                chunk = raw[offset:offset + 256 * 1024]
                answer = self.client.upload(
                    attempt_id, token, boot_id, upload_id, offset, chunk,
                    record["sha256"], len(raw),
                )
                new_offset = answer.get("offset") if isinstance(answer, dict) else None
                if type(new_offset) is not int or not 0 <= new_offset <= len(raw) or new_offset == offset:
                    raise ValueError("invalid upload acknowledgement")
                if new_offset < offset:
                    rewinds += 1
                    if rewinds > 3:
                        raise ValueError("upload offset repeatedly rewound")
                if new_offset == len(raw) and answer.get("complete") is not True:
                    raise ValueError("upload completion was not acknowledged")
                record["uploaded_offset"] = new_offset
                self._save()
            # Re-attach an already acknowledged reference too. This operation
            # is idempotent and repairs a restored controller database.
            check_budget()
            ack = self.client.evidence(attempt_id, token, record["stream"], record["sequence"], record["sha256"], record["size"])
            if not isinstance(ack, dict) or ack.get("acknowledged") is not True or ack.get("attempt_id") != attempt_id or ack.get("stream") != record["stream"] or ack.get("sequence") != record["sequence"] or ack.get("sha256") != record["sha256"]:
                raise ValueError("invalid evidence acknowledgement")
            record["evidence_acked"] = True
            self._save()
        if not finish:
            return
        check_budget()
        ack = self.client.complete(Result.from_dict(pending["result"]), token, boot_id)
        if not isinstance(ack, dict) or ack.get("acknowledged") is not True or ack.get("attempt_id") != attempt_id or ack.get("state") != "COMPLETE":
            raise ValueError("invalid completion acknowledgement")
        if pending.get("physical"):
            pending["completion_acked"] = True
            self._save()
        else:
            self._set_pending(None)

    def _run_recipe(self, recipe: Callable[[Experiment], RecipeOutput], experiment: Experiment, pending: dict) -> RecipeOutput:
        # A local recipe runs in its own process group so timeout can stop its
        # descendants. Parent heartbeats while it waits, never inside the child.
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=False)
        process = context.Process(target=_recipe_child, args=(child, recipe, experiment))
        process.start()
        child.close()
        deadline = time.monotonic() + experiment.timeout_s
        if self.supervisor:
            self.supervisor.begin("experiment", experiment.timeout_s)
        try:
            while True:
                if self.supervisor:
                    self.supervisor.pulse(waiting=True)
                self._progress(pending, "experiment", "Recipe alive; waiting for the next sealed observation.",
                    timeout_s=min(604800, max(1, int(experiment.timeout_s))), completed=len(pending["evidence"]))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return RecipeOutput(Outcome.INFRA_FAILURE, "Recipe exceeded its time limit.", limitations=["Process group was stopped after timeout."])
                if parent.poll(min(5.0, remaining)):
                    try:
                        kind, payload = parent.recv()
                    except EOFError:
                        return RecipeOutput(Outcome.INFRA_FAILURE, "Recipe worker exited without a result.")
                    if kind == "chunk":
                        if not isinstance(payload, EvidenceChunk):
                            return RecipeOutput(Outcome.INFRA_FAILURE, "Invalid evidence chunk.")
                        self._seal(pending, payload.stream, payload.data)
                        try:
                            self._drain(pending, finish=False, deadline=min(deadline, time.monotonic()+5))
                        except (ConnectionError, TimeoutError, OSError):
                            pass
                        except Exception as exc:
                            from .transport import TransportError
                            if not isinstance(exc, TransportError):
                                raise
                        continue
                    if kind == "error":
                        return RecipeOutput(Outcome.INFRA_FAILURE, f"Locally installed recipe raised {payload}.")
                    if not isinstance(payload, RecipeOutput):
                        return RecipeOutput(Outcome.INFRA_FAILURE, "Recipe worker returned an invalid result.")
                    return payload
                if not process.is_alive():
                    return RecipeOutput(Outcome.INFRA_FAILURE, "Recipe worker exited without a result.")
                if time.monotonic() >= deadline:
                    return RecipeOutput(Outcome.INFRA_FAILURE, "Recipe exceeded its time limit.", limitations=["Process group was stopped after timeout."])
                try:
                    ack = self.client.heartbeat(pending["attempt_id"], pending["token"], pending["boot_id"])
                    if not isinstance(ack, dict) or ack.get("attempt_id") != pending["attempt_id"]:
                        raise ValueError("invalid heartbeat acknowledgement")
                except (ConnectionError, TimeoutError, OSError):
                    pass
                except Exception as exc:
                    from .transport import TransportError
                    if not isinstance(exc, TransportError):
                        raise
        finally:
            parent.close()
            if process.is_alive():
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    process.kill()
            process.join(timeout=5)

    def _progress(self, pending, phase, message, *, timeout_s=1800, completed=None, state="WAITING"):
        if not pending.get("campaign_id") or not hasattr(self.client, "progress"):
            return
        if phase in pending.get("progress_finished", []):
            return
        sequence = pending.setdefault("progress_sequences", {}).get(phase, -1) + 1
        pending["progress_sequences"][phase] = sequence
        self._save()
        report = Progress("target-" + pending["attempt_id"] + "-" + phase,
            pending["campaign_id"], phase, state, message[:1000], sequence,
            completed=completed, unit="chunks", expected_update_s=15,
            stall_after_s=min(300, timeout_s), timeout_s=timeout_s)
        try:
            self.client.progress(pending["attempt_id"], pending["token"], report)
            if state in {"COMPLETE", "FAILED"}:
                pending.setdefault("progress_finished", []).append(phase)
                self._save()
        except Exception:
            # Monitoring cannot discard observations or extend experiment deadlines.
            pass

    def preparation_progress(self, phase=None, message=None):
        """Wire to the deployment adapter's bounded command progress callback."""
        pending = self._journal.get("pending")
        if self.supervisor:
            self.supervisor.pulse(waiting=True)
        if pending and pending.get("stage") == "preparing":
            self._progress(pending, "prepare", message or "Preparing authorized deployment; waiting for adapter progress.")
            self.client.heartbeat(pending["attempt_id"], pending["token"], pending["boot_id"])

    def _finish_candidate(self, pending):
        if self.supervisor:
            self.supervisor.begin("finish-upload", self.finish_upload_s + 20)
        if pending["stage"] == "observed":
            self._progress(pending, "finish", "Uploading sealed evidence before returning to recovery.", timeout_s=int(self.finish_upload_s+20))
            try:
                self._drain(pending, deadline=time.monotonic()+self.finish_upload_s)
            except Exception as exc:
                pending["upload_error"] = type(exc).__name__
                self._save()
        pending["stage"] = "returning"
        self._save()
        self.boot_control.recover()
        return "recovery_requested"

    def _physical_step(self, pending):
        experiment = Experiment.from_dict(pending["experiment"])
        if self.report.mode == "recovery":
            if pending["stage"] in {"claimed", "preparing"} and pending["boot_id"] == self.report.boot_id:
                if self.supervisor:
                    self.supervisor.begin("prepare-deployment", 1800)
                raw = self.client.artifact(experiment.artifacts["deployment"])
                if digest(raw) != experiment.artifacts["deployment"]:
                    raise ValueError("deployment artifact digest mismatch")
                manifest = DeploymentManifest.from_dict(json.loads(raw))
                pending["stage"] = "preparing"
                self._save()
                # Adapter preparation is restartable. Only recovery calls it.
                prepared = self.deployment_backend.prepare(manifest, pending["attempt_id"])
                self._progress(pending, "prepare", "Exact deployment verified and ready for one-shot boot.", state="COMPLETE")
                pending["revision"] = prepared.revision
                pending["deployment_id"] = prepared.deployment_id
                self._save()
                ack = self.client.handoff(pending["attempt_id"], pending["token"], pending["boot_id"], prepared.revision)
                if ack.get("state") != "BOOT_PENDING":
                    raise ValueError("invalid handoff acknowledgement")
                pending["stage"] = "arming"
                self._save()
                self.boot_control.arm_once(prepared, pending["attempt_id"])
                pending["stage"] = "boot_pending"
                self._save()
                self.boot_control.reboot_to_candidate()
                return "candidate_requested"
            if pending["stage"] in {"arming", "boot_pending"} and pending["boot_id"] == self.report.boot_id:
                # An uncertain arming action is not repeated. Clear USB selection.
                self.boot_control.recover()  # recovery adapter clears selection; no reboot
                self._uncertain(pending)
                self.client.recovery_returned(pending["attempt_id"], pending["token"], self.report.boot_id)
                self._drain(pending)
                self._set_pending(None)
                return "completed"
            if not pending.get("result"):
                self._uncertain(pending)
            self.client.recovery_returned(pending["attempt_id"], pending["token"], self.report.boot_id)
            self._drain(pending)
            self._set_pending(None)
            return "completed"
        if self.report.mode != "experiment":
            raise ValueError("physical deployment requires recovery or experiment mode")
        if pending["stage"] in {"arming", "boot_pending"}:
            if (self.deployment_backend.running_revision() != pending.get("revision")
                or self.report.inventory.get("deployment_id") != pending.get("deployment_id")):
                self._uncertain(pending)
                return self._finish_candidate(pending)
            ack = self.client.candidate_started(pending["attempt_id"], pending["token"], self.report.boot_id, pending["revision"])
            if ack.get("state") != "RUNNING":
                raise ValueError("invalid candidate acknowledgement")
            pending["boot_id"] = self.report.boot_id
            pending["stage"] = "started"
            self._save()
            if "library" in experiment.artifacts:
                from .library import LibrarySelection
                if self.library_store is None:
                    raise ValueError("required immutable library verifier is unavailable")
                selection = LibrarySelection.from_dict(json.loads(self.client.artifact(experiment.artifacts["library"])))
                self.library_store.require(selection, self.report.inventory.get("architecture"), self.report.capabilities)
            recipe = self.recipes.get(experiment.recipe)
            output = self._run_recipe(recipe, experiment, pending) if recipe else RecipeOutput(Outcome.NEEDS_HUMAN, "Requested recipe is not installed.")
            self._record_output(pending, output)
            return self._finish_candidate(pending)
        self._uncertain(pending)
        return self._finish_candidate(pending)

    def step(self) -> str:
        """Advance one attempt under an exclusive local journal lock."""
        with self.lock_path.open("a+b") as lock:
            os.fchmod(lock.fileno(), 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Recovery maintenance owns this same lock during healthy long
                # downloads/copies. Keep reporting without touching its journal
                # or authorizing another physical execution.
                if self.supervisor:
                    self.supervisor.begin("target-lock-wait", 120)
                    self.supervisor.pulse(waiting=True)
                return "busy"
            self._journal = self._load()
            if self._journal.get("device_id") != self.report.device_id:
                raise ValueError("journal belongs to another device")
            try:
                return self._step_locked()
            except Exception as exc:
                pending = self._journal.get("pending")
                if self.report.mode == "experiment" and self.boot_control:
                    if pending and pending.get("physical"):
                        if not pending.get("result"):
                            self._record_output(pending, RecipeOutput(Outcome.NEEDS_HUMAN,
                                "Candidate could not safely continue: " + type(exc).__name__,
                                limitations=["No automatic experiment retry was authorized."]))
                        return self._finish_candidate(pending)
                    self.boot_control.recover()
                    return "recovery_requested"
                raise

    def _step_locked(self) -> str:
        """Advance one attempt. Returns idle, completed, or pending.

        Network errors propagate; the journal retains all unacknowledged work.
        The caller can retry step after restoring connectivity.
        """
        pending = self._journal["pending"]
        # Finished candidates return even when the controller is unavailable.
        if pending and pending.get("physical") and self.report.mode == "experiment" and pending["stage"] in {"observed", "returning"}:
            return self._finish_candidate(pending)
        self.client.register(self.report)
        reconciliation = self.client.reconcile(self.report.boot_id)
        if pending and pending.get("physical") and self.report.mode == "recovery":
            # Candidate-start acknowledgement can be lost after the controller
            # adopts the boot. Reconcile identity before delivering uncertainty.
            for remote in reconciliation.get("attempts", []):
                if remote["id"] == pending["attempt_id"]:
                    pending["boot_id"] = remote["boot"]
                    self._save()
                    break
        if pending and pending.get("physical"):
            return self._physical_step(pending)
        if self.report.mode == "experiment":
            if self.boot_control:
                self.boot_control.recover()
            return "recovery_requested"
        if pending is not None:
            if pending["boot_id"] != self.report.boot_id and pending["stage"] == "claimed":
                self._uncertain(pending)
            if pending["stage"] in {"starting", "started"}:
                self._uncertain(pending)
            if pending["stage"] == "observed":
                self._drain(pending)
                return "completed"
            if pending["stage"] != "claimed":
                raise ValueError("invalid target journal stage")
        else:
            request_id = self._journal.get("claim_request_id") or uuid.uuid4().hex
            self._journal["claim_request_id"] = request_id
            self._save()
            claim = self.client.claim(self.report.boot_id, request_id)
            if claim is None:
                self._journal["claim_request_id"] = None
                self._save()
                return "idle"
            if claim.get("device_id") != self.report.device_id or claim.get("boot_id") != self.report.boot_id:
                raise ValueError("claim identity mismatch")
            Experiment.from_dict(claim["experiment"])
            pending = {
                "attempt_id": identifier(claim["attempt_id"]), "token": claim["token"],
                "boot_id": self.report.boot_id, "experiment": claim["experiment"],
                "campaign_id": claim.get("campaign_id"),
                "stage": "claimed", "evidence": [], "result": None,
            }
            self._journal["claim_request_id"] = None
            self._set_pending(pending)
        experiment = Experiment.from_dict(pending["experiment"])
        if "library" in experiment.artifacts:
            try:
                from .library import LibrarySelection
                raw = self.client.artifact(experiment.artifacts["library"])
                if digest(raw) != experiment.artifacts["library"]:
                    raise ValueError("library selection digest mismatch")
                selection = LibrarySelection.from_dict(json.loads(raw))
                if self.library_store is None:
                    raise ValueError("required immutable library verifier is unavailable")
                self.library_store.require(selection, self.report.inventory.get("architecture"), self.report.capabilities)
            except Exception as exc:
                self.client.start(pending["attempt_id"], pending["token"], pending["boot_id"])
                self._record_output(pending, RecipeOutput(Outcome.INFRA_FAILURE,
                    "Required library packs could not be verified: " + type(exc).__name__))
                self._drain(pending)
                return "completed"
        if "deployment" in experiment.artifacts and self.boot_control and self.deployment_backend:
            pending["physical"] = True
            pending["recovery_profile"] = self.report.inventory.get("recovery", {"profile": self.report.inventory.get("recovery_profile"), "watchdog": self.report.inventory.get("watchdog")})
            self._seal(pending, "recovery-profile", canonical({"schema_version": 1, "recovery": pending["recovery_profile"]}))
            self._save()
            return self._physical_step(pending)
        pending["stage"] = "starting"
        self._save()
        self.client.start(pending["attempt_id"], pending["token"], pending["boot_id"])
        pending["stage"] = "started"
        self._save()
        recipe = self.recipes.get(experiment.recipe)
        if recipe is None:
            output = RecipeOutput(
                Outcome.NEEDS_HUMAN,
                f"Recipe {experiment.recipe} is not installed on this target.",
                limitations=["Only locally registered recipes may run."],
            )
        elif "deployment" in experiment.artifacts:
            # Physical handoff is deliberately gated until M3 reconciliation exists.
            raw = self.client.artifact(experiment.artifacts["deployment"])
            if digest(raw) != experiment.artifacts["deployment"]:
                raise ValueError("deployment artifact digest mismatch")
            DeploymentManifest.from_dict(json.loads(raw))
            output = RecipeOutput(
                Outcome.NEEDS_HUMAN,
                "Deployment boot requires a commissioned physical handoff adapter.",
                limitations=["No deployment was booted or recipe executed."],
            )
        elif any(role in experiment.artifacts for role in ("kernel", "kernel_image", "candidate_kernel")):
            output = RecipeOutput(
                Outcome.NEEDS_HUMAN,
                "Legacy kernel-only deployment requests are unsupported; supply a deployment manifest.",
                limitations=["No candidate was booted, even if a BootControl adapter was supplied."],
            )
        else:
            output = self._run_recipe(recipe, experiment, pending)
        self._record_output(pending, output)
        self._drain(pending)
        return "completed"
