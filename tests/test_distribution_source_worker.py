"""Source package code runs only in the existing fixed rootless worker plan."""
import io
import json
from pathlib import Path
import tarfile
import pytest

from quirkbench import distribution_source_worker as worker, builder_setup
from quirkbench.contracts import Conflict, ContractError, canonical
from quirkbench.build import BuildError
from quirkbench.recovery_builder_archive import inspect_builder_archive
from quirkbench.recovery_podman import _copy_cas_object
from quirkbench.store import ArtifactStore, atomic_write
from test_recovery_source_stage import FakeRunner, LIMITS, ENTRY
from test_recovery_podman import builder_archive, IMAGE


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    root = tmp_path/'state'; root.mkdir(mode=0o700)
    stage = root/'workers/operation/1'; stage.mkdir(mode=0o700,parents=True)
    for path in (root/'workers',root/'workers/operation'):path.chmod(0o700)
    (stage/'diagnostics').mkdir(mode=0o700)
    store = ArtifactStore(root/'artifacts',reserve_bytes=0)
    srpm = store.put(b'explicit source package fixture')
    archive = builder_archive()
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        manifest = json.load(stream.extractfile('index.json'))['manifests'][0]['digest']
    retained = store.put(archive)
    entry = {**ENTRY,'kernel_srpm_sha256':srpm.sha256,'builder_image_digest':ENTRY['builder_image_digest']}
    builder = {'builder_image_digest':ENTRY['builder_image_digest'],'builder_config_digest':IMAGE,'builder_archive_sha256':retained.sha256}
    monkeypatch.setattr(builder_setup,'reserve_bytes',lambda _:0)
    return root, stage, store, entry, builder


def run(fixture, *,execute=None,verify=lambda:None):
    root, stage, store, entry, builder = fixture
    return worker.prepare(root,stage,entry,builder,1700000000,'kernel',verify,lambda *a,**kw:None,9999999999,execute=execute)


def injected(argv, log, **kwargs):
    stage = Path(argv[-1])
    worker.inner(stage,runner=FakeRunner(),limits=LIMITS)
    return {'exit_code':0}


def test_joined_fixed_native_plan_and_private_distribution_import(fixture):
    root, stage, store, entry, builder = fixture; commands = []
    def execute(argv, log, **kwargs):
        commands.append(argv)
        assert kwargs['deadline'] == 9999999999 and kwargs['max_duration'] == 7200
        kwargs['verify']()
        return injected(argv,log,**kwargs)
    result = run(fixture,execute=execute)
    assert result['workspace_id'] == 'kernel' and result['base_oid']
    assert (stage/'preparation/output/workspace/init/main.c').read_text() == 'init/main.c'
    command = commands[0]
    assert '--network=none' in command and '--pull=never' in command and '--userns=keep-id' in command
    assert '--security-opt=no-new-privileges' in command
    assert command[-5:] == ['/usr/bin/python3','-m','quirkbench.distribution_source_worker','--stage-dir',str(stage/'distribution')]
    mounts = [command[n+1] for n,value in enumerate(command) if value == '--volume']
    assert len(mounts) == 2 and mounts[0] == f'{stage}/distribution:{stage}/distribution:rw,z'
    assert not any(str(root/'private') in item or str(root/'artifacts') in item or 'controller.sqlite' in item for item in command)
    assert commands[0][-6] == builder['builder_config_digest']


def test_retained_manifest_and_config_are_distinct_from_fedora_base(fixture):
    root, stage, store, entry, builder = fixture
    with tarfile.open(fileobj=io.BytesIO(store.get(builder['builder_archive_sha256']))) as archive:
        manifest=json.load(archive.extractfile('index.json'))['manifests'][0]['digest']
    assert entry['builder_image_digest'] not in (manifest,IMAGE)
    with store.path(builder['builder_archive_sha256']).open('rb') as stream:
        inspect_builder_archive(stream,IMAGE,require_no_entrypoint=True)
    with store.path(builder['builder_archive_sha256']).open('rb') as stream:
        with pytest.raises(BuildError,match='manifest differs'):
            inspect_builder_archive(stream,IMAGE,expected_manifest='sha256:'+'f'*64)


@pytest.mark.parametrize('mutation', ['base','config','archive','srpm'])
def test_incoherent_retained_builder_or_package_blocks_launch(fixture, mutation):
    root, stage, store, entry, builder = fixture; invoked = []
    if mutation == 'base': builder['builder_image_digest'] = 'sha256:'+'f'*64
    elif mutation == 'config': builder['builder_config_digest'] = 'sha256:'+'f'*64
    elif mutation == 'archive': store.path(builder['builder_archive_sha256']).write_bytes(b'changed')
    else: store.path(entry['kernel_srpm_sha256']).write_bytes(b'changed')
    with pytest.raises((Conflict,ContractError,BuildError)):
        run(fixture,execute=lambda *a,**kw:invoked.append(a))
    assert not invoked


def test_failed_package_prep_never_yields_git_workspace(fixture):
    root, stage, store, entry, builder = fixture
    with pytest.raises(ContractError,match='preparation failed'):
        run(fixture,execute=lambda *a,**kw:{'exit_code':1})
    assert not (stage/'preparation/output/workspace').exists()


def test_lost_claim_stops_before_container_launch(fixture):
    calls = []
    def expired(): raise Conflict('claim expired')
    with pytest.raises(Conflict,match='claim expired'):
        run(fixture,verify=expired,execute=lambda *a,**kw:calls.append(a))
    assert not calls


def test_inner_default_requires_container_before_package_code(fixture,monkeypatch):
    from quirkbench import build
    root, stage, store, entry, builder = fixture
    def outside(): raise BuildError('dedicated container required')
    monkeypatch.setattr(build,'_require_container',outside)
    atomic_write(stage/'manifest.json',canonical({'schema_version':1,'entry':entry,'source_date_epoch':1700000000}))
    with pytest.raises(BuildError,match='container required'): worker.inner(stage)
    assert not (stage/'source').exists()


def test_inner_rejects_unknown_manifest_before_runner(fixture):
    root, stage, store, entry, builder = fixture
    atomic_write(stage/'manifest.json',canonical({'schema_version':1,'entry':entry,'source_date_epoch':1700000000,'shell':'forbidden'}))
    runner = FakeRunner()
    with pytest.raises(ContractError): worker.inner(stage,runner=runner,limits=LIMITS)
    assert not runner.phases


def test_destination_substitution_does_not_redirect_copy(fixture,tmp_path):
    root, stage, store, entry, builder = fixture; destination = stage/'source-copy'; moved = tmp_path/'outside-copy'
    def space(count):
        if destination.exists() and not moved.exists():
            destination.rename(moved); destination.write_bytes(b'exact replacement')
    with pytest.raises(BuildError,match='destination moved'):
        _copy_cas_object(root/'artifacts',entry['kernel_srpm_sha256'],destination,1024**2,space_check=space)
    assert moved.read_bytes() == b''


def test_retry_requires_fresh_worker_stage(fixture):
    run(fixture,execute=injected)
    with pytest.raises(FileExistsError): run(fixture,execute=injected)


def test_refused_short_read_copy_never_flushes_later_bytes_outside(fixture,tmp_path,monkeypatch):
    import os
    root, stage, store, entry, builder = fixture
    destination = stage/'short-copy'; moved = tmp_path/'outside-short-copy'; before = []
    native_read = os.read
    monkeypatch.setattr(os,'read',lambda fd,count:native_read(fd,min(count,2)))
    chunks = [0]
    def space(count):
        if count == 2:
            chunks[0] += 1
            if chunks[0] == 2:
                destination.rename(moved); before.append(moved.read_bytes())
                destination.write_bytes(b'exact replacement')
    with pytest.raises(BuildError,match='destination moved'):
        _copy_cas_object(root/'artifacts',entry['kernel_srpm_sha256'],destination,1024**2,space_check=space)
    assert before == [b'ex'] and moved.read_bytes() == before[0]


@pytest.mark.parametrize('marker',[None,b'',b'sha256:'+b'f'*64,b'x'*257])
def test_native_inner_refuses_missing_wrong_or_oversize_base_before_package_work(fixture,monkeypatch,marker):
    from quirkbench import build,build_pipeline
    root,stage,store,entry,builder=fixture
    atomic_write(stage/'manifest.json',canonical({'schema_version':1,'entry':entry,'source_date_epoch':0}))
    read=worker.read_file
    def fake_read(root,relative,**kw):
        if Path(root)==Path('/etc'):
            assert kw['limit']==256
            if marker is None:raise FileNotFoundError('no base marker')
            if len(marker)>kw['limit']:raise ContractError('record exceeds read budget')
            return marker
        return read(root,relative,**kw)
    monkeypatch.setattr(worker,'read_file',fake_read)
    monkeypatch.setattr(build,'_require_container',lambda:None)
    called=[];monkeypatch.setattr(build_pipeline,'BoundedRunner',lambda stage:called.append(stage))
    with pytest.raises(ContractError):worker.inner(stage)
    assert not called and not (stage/'prepared.json').exists()


def test_native_inner_checks_distinct_correct_base_then_uses_existing_runner(fixture,monkeypatch):
    from quirkbench import build,build_pipeline
    root,stage,store,entry,builder=fixture
    atomic_write(stage/'manifest.json',canonical({'schema_version':1,'entry':entry,'source_date_epoch':0}))
    atomic_write(stage/'input.src.rpm',store.get(entry['kernel_srpm_sha256']))
    read=worker.read_file
    def fake_read(root,relative,**kw):
        if Path(root)==Path('/etc'):return entry['builder_image_digest'].encode()+b'\n'
        return read(root,relative,**kw)
    monkeypatch.setattr(worker,'read_file',fake_read)
    monkeypatch.setattr(build,'_require_container',lambda:None)
    class InjectedRunner(FakeRunner):
        def __init__(self,workspace):super().__init__();self.workspace=workspace
    monkeypatch.setattr(build_pipeline,'BoundedRunner',InjectedRunner)
    monkeypatch.setattr(build_pipeline.ResourceLimits,'from_cgroup',lambda:LIMITS)
    worker.inner(stage)
    assert (stage/'prepared.json').is_file()
