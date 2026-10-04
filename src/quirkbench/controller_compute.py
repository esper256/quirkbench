"""Compute eligibility on the existing foreground owner, separate from HTTPS."""
import time
import sqlite3

from .contracts import Conflict
from .process_identity import WorkerServiceError


class ComputeGate:
    """Retry exact reconciliation without treating an engine outage as a stop."""
    def __init__(self, owner, coordinators, *, monotonic=time.monotonic):
        self.owner = owner
        self.coordinators = coordinators
        self.monotonic = monotonic
        self.retry_at = 0
        self.reconciled = False

    def tick(self):
        if self.monotonic() < self.retry_at:
            return []
        try:
            if not self.reconciled:
                for coordinator in self.coordinators:
                    self.owner.reconcile_units(coordinator.services)
                self.reconciled = True
            return [result for coordinator in self.coordinators
                    if (result := coordinator.tick()) is not None]
        except WorkerServiceError:
            # Claims/containers and their exact stop proofs remain authoritative.
            # A failed engine request must never end authenticated availability.
            self.retry_at = self.monotonic() + 30
            return []


def readiness(root):
    """Read current compute capability; never reconcile or create a worker."""
    from .controller_service import configuration
    from .worker_service import ContainerWorkerServices
    from .state_reader import StateReader
    try:
        with StateReader(root).connection() as db:
            epoch = db.execute('SELECT epoch FROM controller_lifecycle WHERE id=1').fetchone()[0]
            unresolved = db.execute("SELECT 1 FROM operations WHERE worker_unit IS NOT NULL AND (state!='RUNNING' OR worker_epoch!=?) LIMIT 1", (epoch,)).fetchone()
        if unresolved:
            return {'compute_ready': False, 'worker_reconciliation': 'required',
                    'compute_instructions': ['Restore the recorded worker backend and reconcile exact stop before starting another worker.']}
        config = configuration(root)
        services = ContainerWorkerServices(engine=config.get('worker_engine', 'podman'),
            worker_image=config.get('worker_image') or config.get('builder_config_digest'))
        services.preflight(root, time.time() + 60)
        return {'compute_ready': True, 'worker_reconciliation': 'clear', 'compute_instructions': []}
    except (OSError, ValueError, WorkerServiceError, sqlite3.Error) as exc:
        return {'compute_ready': False, 'worker_reconciliation': 'unavailable',
                'compute_instructions': ['Configure a supported local container engine and exact worker image; prepare the signed first builder with setup --builder-archive. ' + type(exc).__name__]}
