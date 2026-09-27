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

from .contracts import CapabilityReport, Experiment, Outcome, Result, canonical, digest, identifier


class BootControl(Protocol):
    """Hardware integration boundary; an implementation needs commissioning."""

    def stage_candidate(self, artifact: bytes, expected_digest: str) -> None: ...
    def reboot_to_candidate(self) -> None: ...
    def recover(self) -> None: ...


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
        conn.send(("ok", recipe(experiment)))
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
    ):
        if report.device_id != client.device_id:
            raise ValueError("target report and client device differ")
        if any(cap in report.capabilities for cap in ("candidate_boot", "kernel_boot", "boot_control")):
            raise ValueError("boot capability unavailable until boot cycle is implemented")
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
        evidence_records = []
        for sequence, (stream, raw) in enumerate(output.evidence.items()):
            identifier(stream)
            if not isinstance(raw, bytes) or len(raw) > 128 * 1024 * 1024:
                raise ValueError("evidence stream must be bytes of at most 128 MiB")
            checksum = digest(raw)
            _atomic(self.blob_dir / checksum, raw)
            evidence_records.append({
                "stream": stream, "sequence": sequence, "sha256": checksum,
                "size": len(raw), "uploaded_offset": 0, "evidence_acked": False,
            })
        result = Result(
            attempt_id=pending["attempt_id"], outcome=output.outcome, summary=output.summary,
            evidence=[record["sha256"] for record in evidence_records],
            measurements=output.measurements, limitations=output.limitations,
        )
        pending["result"] = asdict(result)
        pending["evidence"] = evidence_records
        pending["stage"] = "observed"
        self._save()

    def _uncertain(self, pending: dict) -> None:
        self._record_output(pending, RecipeOutput(
            Outcome.NEEDS_HUMAN,
            "Target restarted after execution intent; whether the recipe ran or completed is unknown.",
            limitations=["Execution was not repeated after a target restart."],
        ))

    def _drain(self, pending: dict) -> None:
        attempt_id = pending["attempt_id"]
        token = pending["token"]
        boot_id = pending["boot_id"]
        for record in pending["evidence"]:
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
            ack = self.client.evidence(attempt_id, token, record["stream"], record["sequence"], record["sha256"], record["size"])
            if not isinstance(ack, dict) or ack.get("acknowledged") is not True or ack.get("attempt_id") != attempt_id or ack.get("stream") != record["stream"] or ack.get("sequence") != record["sequence"] or ack.get("sha256") != record["sha256"]:
                raise ValueError("invalid evidence acknowledgement")
            record["evidence_acked"] = True
            self._save()
        ack = self.client.complete(Result.from_dict(pending["result"]), token, boot_id)
        if not isinstance(ack, dict) or ack.get("acknowledged") is not True or ack.get("attempt_id") != attempt_id or ack.get("state") != "COMPLETE":
            raise ValueError("invalid completion acknowledgement")
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
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return RecipeOutput(Outcome.INFRA_FAILURE, "Recipe exceeded its time limit.", limitations=["Process group was stopped after timeout."])
                if parent.poll(min(5.0, remaining)):
                    try:
                        kind, payload = parent.recv()
                    except EOFError:
                        return RecipeOutput(Outcome.INFRA_FAILURE, "Recipe worker exited without a result.")
                    if kind == "error":
                        return RecipeOutput(Outcome.INFRA_FAILURE, f"Locally installed recipe raised {payload}.")
                    if not isinstance(payload, RecipeOutput):
                        return RecipeOutput(Outcome.INFRA_FAILURE, "Recipe worker returned an invalid result.")
                    return payload
                if not process.is_alive():
                    return RecipeOutput(Outcome.INFRA_FAILURE, "Recipe worker exited without a result.")
                if time.monotonic() >= deadline:
                    return RecipeOutput(Outcome.INFRA_FAILURE, "Recipe exceeded its time limit.", limitations=["Process group was stopped after timeout."])
                ack = self.client.heartbeat(pending["attempt_id"], pending["token"], pending["boot_id"])
                if not isinstance(ack, dict) or ack.get("attempt_id") != pending["attempt_id"]:
                    raise ValueError("invalid heartbeat acknowledgement")
        finally:
            parent.close()
            if process.is_alive():
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    process.kill()
            process.join(timeout=5)

    def step(self) -> str:
        """Advance one attempt under an exclusive local journal lock."""
        with self.lock_path.open("a+b") as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._journal = self._load()
            if self._journal.get("device_id") != self.report.device_id:
                raise ValueError("journal belongs to another device")
            return self._step_locked()

    def _step_locked(self) -> str:
        """Advance one attempt. Returns idle, completed, or pending.

        Network errors propagate; the journal retains all unacknowledged work.
        The caller can retry step after restoring connectivity.
        """
        self.client.register(self.report)
        self.client.reconcile(self.report.boot_id)
        pending = self._journal["pending"]
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
                "stage": "claimed", "evidence": [], "result": None,
            }
            self._journal["claim_request_id"] = None
            self._set_pending(pending)
        experiment = Experiment.from_dict(pending["experiment"])
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
        elif any(role in experiment.artifacts for role in ("kernel", "kernel_image", "candidate_kernel")):
            output = RecipeOutput(
                Outcome.NEEDS_HUMAN,
                "Candidate kernel boot cycle is not implemented.",
                limitations=["No candidate was booted, even if a BootControl adapter was supplied."],
            )
        else:
            output = self._run_recipe(recipe, experiment, pending)
        self._record_output(pending, output)
        self._drain(pending)
        return "completed"
