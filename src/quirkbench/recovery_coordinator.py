"""Fixed recovery-image execution on the existing controller lifecycle owner.

This services explicit recovery image operations only, not agent scheduling.
"""
from pathlib import Path
from .contracts import Conflict,canonical
from .operations import recovery_rootfs_arguments
from .recovery_rootfs import _json


class RecoveryImageCoordinator:
    def __init__(self, owner, services, *, signing_home, trusted_public_key, fingerprint, timeout=7200):
        if type(timeout) is not int or not 60<=timeout<=86400: raise ValueError('invalid image operation deadline')
        self.owner,self.services=owner,services
        self.signing={'signing_home':Path(signing_home),'trusted_public_key':Path(trusted_public_key),'fingerprint':fingerprint}
        self.timeout=timeout

    def tick(self):
        """Advance one admitted image; all authority remains in existing records."""
        owner=self.owner; controller=owner.controller
        if owner.closed or controller._lifecycle_owner is not owner: raise Conflict('controller ownership ended')
        with controller.transaction() as db:
            active=[dict(row) for row in db.execute("SELECT * FROM operations WHERE state='RUNNING' AND worker_unit IS NOT NULL ORDER BY created")]
            queued=[dict(row) for row in db.execute("SELECT * FROM operations WHERE state='QUEUED' AND kind='image_prepare' AND queued_epoch=? ORDER BY created", (owner.epoch,))]
        if active:
            if len(active)!=1: raise Conflict('multiple active recovery workers require reconciliation')
            claim=active[0]
            try: arguments=recovery_rootfs_arguments(_json(controller.store.get(claim['input_digest']),'image intent'))
            except ValueError: return None
            if 'recipe_sha256' not in arguments: return None
            expired=controller.clock()>=claim['deadline']
            if not expired and hasattr(self.services, 'advance'):
                self.services.advance(claim, controller.root)
            if not expired:
                try:
                    owner.collect_activity(claim)
                except Conflict:
                    if controller.clock()<claim['deadline']: raise
                    expired=True
            if not expired and not self.services.finished(claim['worker_unit'],claim['worker_boot_id']): return None
            # A collected transient unit is never re-stopped; exact proof is journaled.
            owner._stop_worker_once(claim,self.services)
            if expired:
                controller._publish_operation(claim['id'],owner.epoch,claim['worker_generation'],state='FAILED',
                    error={'code':'RECOVERY_IMAGE_DEADLINE','message':'Image worker deadline expired; inspect retained private diagnostics.','retryable':True},
                    expected_claim=claim,clear_stopped_worker=True)
                owner.housekeep()
                return {'operation_id':claim['id'],'state':'FAILED'}
            try:
                result=owner.consume_recovery_image(claim['id'],services=self.services,**self.signing)
            except Conflict:
                if controller.clock()<claim['deadline']: raise
                # The transaction distinguishes elapsed deadline from ownership loss:
                # FAILED still requires the exact claim and current live owner.
                controller._publish_operation(claim['id'],owner.epoch,claim['worker_generation'],state='FAILED',
                    error={'code':'RECOVERY_IMAGE_DEADLINE','message':'Image completion crossed its deadline; retained bytes remain unqualified.','retryable':True},
                    expected_claim=claim,clear_stopped_worker=True)
                owner.housekeep()
                return {'operation_id':claim['id'],'state':'FAILED'}
            except Exception as exc:
                controller._publish_operation(claim['id'],owner.epoch,claim['worker_generation'],state='FAILED',
                    error={'code':'RECOVERY_IMAGE_VALIDATION_FAILED','message':'Private image validation/signing failed; inspect retained stage diagnostics. '+type(exc).__name__,'retryable':True},expected_claim=claim,clear_stopped_worker=True)
                result={'operation_id':claim['id'],'state':'FAILED'}
            owner.housekeep()
            return result
        for row in queued:
            try: arguments=recovery_rootfs_arguments(_json(controller.store.get(row['input_digest']),'image intent'))
            except ValueError: continue
            if 'recipe_sha256' not in arguments: continue
            return owner.dispatch(row['id'],stage='recovery_rootfs',deadline=controller.clock()+self.timeout,services=self.services)
        return None
