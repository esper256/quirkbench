"""Full fixed image worker and fenced signing, with only synthetic privileged adapters."""
from contextlib import contextmanager
from pathlib import Path
import json
import pytest
from quirkbench.contracts import canonical,digest,Conflict
from quirkbench.controller import Controller
from quirkbench.recovery_image_worker import build_stock_image
from quirkbench.recovery_worker import run_rootfs_worker
from quirkbench.image import _builder_identity,_input_identity
from test_recovery_stock import stock_fixture
from test_stock_recovery_flow import Runner,installer,assembled_stock
from test_recovery_podman import IMAGE,builder_archive
from test_recovery_distribution import fake_gpg,fake_public_gpg,FINGERPRINT,trusted_key


class Stopped:
    def stop_and_verify(self,*args): return 'stopped'


@contextmanager
def completed_image(tmp_path,monkeypatch,*,version=2):
    import quirkbench.recovery_distribution as distribution
    from test_recovery_release import assembled
    _,_,_,_,_,template=assembled(tmp_path/'template',monkeypatch,version=version)
    recipe,lock,_,store=stock_fixture(tmp_path/'inputs')
    if version==3:recipe={**recipe,'schema_version':3,'layout':{'root_mib':2048,'factory_size_mib':4096,'library_payload_bytes':0}}
    lock={**lock,'builder_image_digest':IMAGE}
    recipe={**recipe,'builder_image_digest':IMAGE,'rootfs_lock_sha256':store.put(canonical(lock)).sha256}
    recipe_sha=store.put(canonical(recipe)).sha256
    controller=Controller(tmp_path/'controller',reserve_bytes=0)
    for path in store.objects.iterdir(): controller.store.put_file(path)
    archive=controller.store.put(builder_archive()).sha256
    image_hash=digest(b'synthetic image fixture')
    import quirkbench.recovery_stock_release as stock_release
    monkeypatch.setattr(stock_release,'_image_identity',lambda _:(image_hash,4096*1024**2))
    monkeypatch.setattr(distribution,'_image_identity',lambda _:(image_hash,4096*1024**2))
    def install(catalog,lock,store,root):
        installer(catalog,lock,store,root)
        path=root/'usr/lib/quirkbench/recovery-rootfs-lock.json'
        path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(canonical(lock)+b'\n')
        return root
    def assemble(inputs):
        inputs.output.write_bytes(b'synthetic image fixture')
        manifest={**template,'image_sha256':image_hash,'input_identity':_input_identity(inputs),'builder_identity':_builder_identity()}
        Path(str(inputs.output)+'.json').write_bytes(canonical(manifest))
        Path(str(inputs.output)+'.sha256').write_text(image_hash+'  '+inputs.output.name+'\n')
    def execute(argv,log,*,verify,deadline):
        verify()
        assert 'quirkbench.recovery_image_worker' in argv and '--network=none' in argv
        assert '--env=QUIRKBENCH_BUILDER_CONFIG_DIGEST='+lock['builder_image_digest'] in argv
        assert not any('signing' in item for item in argv)
        stage=log.parent.parent
        from quirkbench.recovery_rootfs import CASReader
        build_stock_image(recipe_sha,CASReader(stage/'inputs/cas'),stage/'output',
            runner=Runner(),rootfs_installer=install,image_builder=assemble)
        log.write_text('synthetic full image completed\n')
        return {'exit_code':0,'output_bytes':31,'retained_bytes':31,'log_truncated':False}
    with controller.lifecycle() as owner:
        operation=controller.admit_recovery_image('image',recipe_sha,archive)
        claim=owner.claim(operation['id'],stage='recovery_rootfs',deadline=controller.clock()+300)
        record=run_rootfs_worker(controller.root,claim['id'],claim['worker_epoch'],claim['worker_generation'],Path(claim['stage_dir']),
            cgroup_reader=lambda:f"0::/user.slice/{claim['worker_unit']}\n",executor=execute)
        assert record['state']=='COMPLETED' and record['operation_complete'] is False
        closure=store.get(lock['target_rpm_lock_sha256']).decode()
        home=tmp_path/'signing'; home.mkdir()
        yield controller,owner,claim,closure,home


def test_fixed_worker_to_current_owner_signed_publication(tmp_path,monkeypatch):
    with completed_image(tmp_path,monkeypatch) as (controller,owner,claim,closure,home):
        result=owner.consume_recovery_image(claim['id'],services=Stopped(),query=lambda *args:closure,
            signing_home=home,trusted_public_key=trusted_key(tmp_path),fingerprint=FINGERPRINT,
            signing_run=fake_gpg,verification_run=fake_public_gpg)
        assert result['operation']['state']=='SUCCEEDED' and result['qualification_status']=='unqualified'
        assert len(result['operation']['references']['output'])==7
        assert controller.store.get(result['image_sha256'])==b'synthetic image fixture'


def test_corrupt_completed_image_cannot_be_signed_or_published(tmp_path,monkeypatch):
    with completed_image(tmp_path,monkeypatch) as (controller,owner,claim,closure,home):
        path=Path(claim['stage_dir'])/'output/image-result.json'
        result=json.loads(path.read_bytes()); result['candidate']['kernel_release']='changed'
        path.write_bytes(canonical(result))
        def signing(*args,**kwargs): raise AssertionError('invalid result must not reach signing')
        from quirkbench.build import BuildError
        with pytest.raises(BuildError,match='independent coordinator'):
            owner.consume_recovery_image(claim['id'],services=Stopped(),query=lambda *args:closure,
                signing_home=home,trusted_public_key=trusted_key(tmp_path),fingerprint=FINGERPRINT,
                signing_run=signing,verification_run=fake_public_gpg)
        assert controller.operation_status(claim['id'])['data']['references']['output']==[]


class CollectOnStop:
    def __init__(self): self.calls=0
    def finished(self,*args): return True
    def stop_and_verify(self,*args):
        self.calls+=1
        if self.calls>1: raise AssertionError('transient unit was already collected')
        return 'stopped'


def test_coordinator_stops_collected_unit_once_through_publication(tmp_path,monkeypatch):
    from quirkbench.recovery_coordinator import RecoveryImageCoordinator
    with completed_image(tmp_path,monkeypatch) as (controller,owner,claim,closure,home):
        services=CollectOnStop()
        original=owner.consume_recovery_image
        def consume(*args,**kwargs):
            return original(*args,**kwargs,query=lambda *a:closure,signing_run=fake_gpg,verification_run=fake_public_gpg)
        monkeypatch.setattr(owner,'consume_recovery_image',consume)
        coordinator=RecoveryImageCoordinator(owner,services,signing_home=home,
            trusted_public_key=trusted_key(tmp_path),fingerprint=FINGERPRINT)
        result=coordinator.tick()
        assert result['operation']['state']=='SUCCEEDED' and services.calls==1
        assert result['operation']['worker_unit'] is None
        assert coordinator.tick() is None
        assert owner.reconcile_units(services)==[] and services.calls==1


def test_expired_completed_image_fails_durably_without_signing(tmp_path,monkeypatch):
    from quirkbench.recovery_coordinator import RecoveryImageCoordinator
    with completed_image(tmp_path,monkeypatch) as (controller,owner,claim,closure,home):
        services=CollectOnStop()
        controller.clock=lambda:claim['deadline']+1
        coordinator=RecoveryImageCoordinator(owner,services,signing_home=home,
            trusted_public_key=trusted_key(tmp_path),fingerprint=FINGERPRINT)
        assert coordinator.tick()['state']=='FAILED'
        status=controller.operation_status(claim['id'])['data']
        assert status['state']=='FAILED' and status['worker_unit'] is None
        assert controller.operation_failure(claim['id'])['code']=='RECOVERY_IMAGE_DEADLINE'
        assert coordinator.tick() is None and services.calls==1


def test_signature_changed_during_cas_import_cannot_publish_success(tmp_path,monkeypatch):
    from quirkbench.contracts import ContractError
    with completed_image(tmp_path,monkeypatch) as (controller,owner,claim,closure,home):
        original=controller.store.put_file
        def changed(path,*args,**kwargs):
            if str(path).endswith('.checksums.json.sig'): Path(path).write_bytes(b'changed signature')
            return original(path,*args,**kwargs)
        monkeypatch.setattr(controller.store,'put_file',changed)
        with pytest.raises(ContractError,match='signed image bytes changed'):
            owner.consume_recovery_image(claim['id'],services=CollectOnStop(),query=lambda *args:closure,
                signing_home=home,trusted_public_key=trusted_key(tmp_path),fingerprint=FINGERPRINT,
                signing_run=fake_gpg,verification_run=fake_public_gpg)
        assert controller.operation_status(claim['id'])['data']['references']['output']==[]


def test_deadline_crossed_during_signing_fails_without_stopping_controller(tmp_path,monkeypatch):
    from quirkbench.recovery_coordinator import RecoveryImageCoordinator
    with completed_image(tmp_path,monkeypatch) as (controller,owner,claim,closure,home):
        services=CollectOnStop()
        original=owner.consume_recovery_image
        def signing(*args,**kwargs):
            result=fake_gpg(*args,**kwargs)
            controller.clock=lambda:claim['deadline']+1
            return result
        def consume(*args,**kwargs):
            return original(*args,**kwargs,query=lambda *a:closure,signing_run=signing,verification_run=fake_public_gpg)
        monkeypatch.setattr(owner,'consume_recovery_image',consume)
        coordinator=RecoveryImageCoordinator(owner,services,signing_home=home,
            trusted_public_key=trusted_key(tmp_path),fingerprint=FINGERPRINT)
        assert coordinator.tick()['state']=='FAILED'
        status=controller.operation_status(claim['id'])['data']
        assert status['state']=='FAILED' and status['worker_unit'] is None and status['references']['output']==[]
        assert coordinator.tick() is None and services.calls==1


def test_image_coordinator_leaves_rootfs_only_active_work_untouched(tmp_path):
    from quirkbench.recovery_coordinator import RecoveryImageCoordinator
    controller=Controller(tmp_path/'controller',reserve_bytes=0)
    recipe,lock,_,store=stock_fixture(tmp_path/'inputs')
    for path in store.objects.iterdir(): controller.store.put_file(path)
    archive=controller.store.put(builder_archive()).sha256
    lock_ref=controller.store.put(canonical(lock)).sha256
    with controller.lifecycle() as owner:
        operation=controller.admit_operation('rootfs','image_prepare',{
            'schema_version':2,'builder_config_digest':lock['builder_image_digest'],
            'builder_archive_sha256':archive,'rootfs_lock_sha256':lock_ref},input_refs=[archive,lock_ref])
        claim=owner.claim(operation['id'],stage='recovery_rootfs',deadline=controller.clock()+60)
        class Untouched:
            def finished(self,*args): raise AssertionError('rootfs-only work is not this executor')
            def stop_and_verify(self,*args): raise AssertionError('must not cancel rootfs-only work')
        coordinator=RecoveryImageCoordinator(owner,Untouched(),signing_home=tmp_path,
            trusted_public_key=tmp_path/'key',fingerprint=FINGERPRINT)
        assert coordinator.tick() is None
        assert controller.operation_status(claim['id'])['data']['state']=='RUNNING'


def test_coordinator_leaves_prior_owner_queue_visible_without_crashing(tmp_path):
    from quirkbench.recovery_coordinator import RecoveryImageCoordinator
    controller=Controller(tmp_path/'controller',reserve_bytes=0)
    recipe,lock,_,store=stock_fixture(tmp_path/'inputs')
    lock={**lock,'builder_image_digest':IMAGE}
    recipe={**recipe,'builder_image_digest':IMAGE,'rootfs_lock_sha256':store.put(canonical(lock)).sha256}
    for path in store.objects.iterdir(): controller.store.put_file(path)
    recipe_sha=controller.store.put(canonical(recipe)).sha256
    archive=controller.store.put(builder_archive()).sha256
    queued=controller.admit_recovery_image('before-owner',recipe_sha,archive)
    with controller.lifecycle() as owner:
        coordinator=RecoveryImageCoordinator(owner,object(),signing_home=tmp_path,
            trusted_public_key=tmp_path/'public',fingerprint=FINGERPRINT)
        assert coordinator.tick() is None
        assert controller.operation_status(queued['id'])['data']['state']=='QUEUED'
        assert not (controller.root/'workers').exists()


def test_v3_uses_actual_durable_worker_admission_validation_and_publication(tmp_path,monkeypatch):
    with completed_image(tmp_path,monkeypatch,version=3) as (controller,owner,claim,closure,home):
        result=owner.consume_recovery_image(claim['id'],services=Stopped(),query=lambda *args:closure,
            signing_home=home,trusted_public_key=trusted_key(tmp_path),fingerprint=FINGERPRINT,
            signing_run=fake_gpg,verification_run=fake_public_gpg)
        assert result['operation']['state']=='SUCCEEDED'
        # The retained worker candidate remains explicitly unqualified.
        candidates=list(controller.store.objects.iterdir())
        matching=[json.loads(p.read_bytes()) for p in candidates if p.stat().st_size<65536 and p.read_bytes().startswith(b'{')]
        candidate=next(v for v in matching if v.get('record_type')=='recovery-release-candidate')
        assert candidate['schema_version']==3 and candidate['layout']['library_payload_bytes']==0
        assert candidate['qualified_capabilities']==[]
