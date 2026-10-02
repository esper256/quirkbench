"""Candidate baseline closure and shared Fedora assembly, all native calls injected."""
import json
from pathlib import Path
import pytest
from quirkbench import baseline_inputs as inputs
from quirkbench.contracts import ContractError,Conflict,canonical
from quirkbench.build import BuildError
from test_recovery_rootfs import locked_fixture,_rpm_line


@pytest.fixture
def setup(tmp_path):
    catalog,unused,reader,store,snapshot = locked_fixture(tmp_path/'inputs')
    entry = catalog['entries'][0]
    return entry,store,snapshot


def test_complete_candidate_input_records_pin_every_package_without_recovery_policy(setup):
    entry,store,snapshot = setup
    value,refs = inputs.input_record(store,entry)
    assert inputs.validate(value)==value
    observed,packages,lock,closure = inputs.resolve(store,value)
    assert observed==entry and packages==snapshot
    assert lock==''.join(sorted(_rpm_line(item) for item in snapshot['packages'])).encode()
    assert set(refs)==set(closure)
    assert {item['sha256'] for item in snapshot['packages']}<=set(refs)
    assert 'recovery_fragment_sha256' not in value


def test_missing_rpm_content_never_counts_manifest_as_complete(setup):
    entry,store,snapshot = setup
    missing = snapshot['packages'][0]['sha256'];store.path(missing).unlink()
    with pytest.raises(ContractError):inputs.input_record(store,entry)


@pytest.mark.parametrize('mutation',['omitted','extra','identity','lock','key'])
def test_snapshot_closure_and_lock_are_exact_supported_identities(setup,mutation):
    entry,store,snapshot = setup
    if mutation=='omitted':snapshot['packages'].pop()
    elif mutation=='extra':snapshot['packages'].append({'name':'zzz','nevra':'zzz-0:1-1.fc44.x86_64','sha256':store.put(b'zzz').sha256})
    elif mutation=='identity':snapshot['packages'][0]['nevra']='NetworkManager-0:1-1.fc44.x86_64'
    elif mutation=='lock':entry['target_rpm_lock_sha256']=store.put(b'wrong lock\n').sha256
    else:
        package={'name':'gpg-pubkey','nevra':'gpg-pubkey-0:1-1.fc44.(none)'}
        entry['packages']=sorted(entry['packages']+[package],key=lambda item:(item['name'],item['nevra']))
        snapshot['packages']=sorted(snapshot['packages']+[{**package,'sha256':store.put(b'unpinned key').sha256}],key=lambda item:(item['name'],item['nevra']))
    entry['rpm_snapshot_sha256']=store.put(canonical(snapshot)).sha256
    with pytest.raises((ContractError,BuildError)):inputs.input_record(store,entry)


def test_last_verification_callback_cannot_change_an_already_verified_package(setup):
    entry,store,snapshot = setup;calls=[0];count=len(inputs.preflight(store,entry)[2])
    def change():
        calls[0]+=1
        if calls[0]==count*2+1:
            store.path(snapshot['packages'][0]['sha256']).write_bytes(b'late package mutation')
    with pytest.raises(ContractError):inputs.preflight(store,entry,verify=change)


def test_existing_snapshot_whitespace_reader_stays_compatible(setup):
    entry,store,snapshot = setup
    entry['rpm_snapshot_sha256']=store.put(json.dumps(snapshot,indent=2).encode()+b'\n').sha256
    assert inputs.input_record(store,entry)[0]['rpm_snapshot_sha256']==entry['rpm_snapshot_sha256']


def test_shared_installer_uses_exact_local_rpms_without_host_repositories(setup,tmp_path):
    entry,store,snapshot = setup;value,refs = inputs.input_record(store,entry)
    marker = tmp_path/'container';marker.write_text('quirkbench-fedora-rootless-build-v1')
    base = tmp_path/'base';base.write_text(entry['builder_image_digest'])
    output = tmp_path/'candidate-root';calls=[]
    def run(argv,timeout_s,**kwargs):
        calls.append(argv)
        if argv[0]=='rpm' and '-qp' in argv:return ''.join(sorted(_rpm_line(item) for item in snapshot['packages']))
        if argv[0]=='dnf5':
            root = Path(next(item.split('=',1)[1] for item in argv if item.startswith('--installroot=')))
            (root/'sbin').mkdir();(root/'sbin/init').touch();return ''
        return ''.join(sorted(_rpm_line(item) for item in snapshot['packages']))
    assert inputs.install(store,value,output,runner=run,marker=marker,base_marker=base,euid=0)==output
    command = next(argv for argv in calls if argv[0]=='dnf5')
    repos = Path(next(item.removeprefix('--setopt=reposdir=') for item in command if item.startswith('--setopt=reposdir=')))
    assert not any(repos.iterdir()) and '--no-plugins' in command
    assert all(Path(item).suffix=='.rpm' for item in command[command.index('install')+1:])
    assert json.loads((output/'usr/lib/quirkbench/candidate-rootfs-input.json').read_bytes())==value
    assert not (output/'usr/lib/quirkbench/recovery-rootfs-lock.json').exists()
    assert (output/'etc/quirkbench-rootfs').read_text()=='quirkbench-fedora-target-v1\n'


def test_candidate_record_strict_schema_and_changed_baseline_binding(setup):
    from jsonschema import Draft202012Validator
    root = Path(__file__).resolve().parents[1]
    example = json.loads((root/'examples/candidate-rootfs-input.json').read_bytes())
    Draft202012Validator(json.loads((root/'schemas/candidate-rootfs-input.v1.schema.json').read_bytes())).validate(example)
    assert inputs.validate(example)==example
    with pytest.raises(ContractError):inputs.validate({**example,'schema_version':True})
    entry,store,snapshot = setup;value,refs = inputs.input_record(store,entry)
    with pytest.raises(Conflict):inputs.resolve(store,{**value,'rpm_snapshot_sha256':'0'*64})


@pytest.mark.parametrize('kind',['symlink','fifo','hardlink','oversize','aggregate'])
def test_candidate_retained_objects_are_bounded_regular_owned_files(setup,tmp_path,monkeypatch,kind):
    import os
    from quirkbench import recovery_rootfs
    entry,store,snapshot=setup;identity=snapshot['packages'][0]['sha256'];path=store.path(identity)
    if kind in ('symlink','fifo','hardlink'):
        raw=path.read_bytes();path.unlink();other=tmp_path/'elsewhere';other.write_bytes(raw)
        if kind=='symlink':path.symlink_to(other)
        elif kind=='fifo':os.mkfifo(path)
        else:os.link(other,path)
    elif kind=='oversize':monkeypatch.setattr(recovery_rootfs,'MAX_RPM_BYTES',1)
    else:monkeypatch.setattr(recovery_rootfs,'MAX_CLOSURE_BYTES',1)
    with pytest.raises(ContractError):inputs.input_record(store,entry)


def test_callback_cannot_change_nonpackage_baseline_input(setup):
    entry,store,snapshot=setup
    def mutate():entry['kernel_srpm_sha256']='f'*64
    with pytest.raises(Conflict,match='baseline changed'):inputs.preflight(store,entry,verify=mutate)


def test_callback_cannot_change_bound_input_record(setup):
    entry,store,snapshot=setup;value,refs=inputs.input_record(store,entry)
    def mutate():value['baseline_sha256']='f'*64
    with pytest.raises(Conflict,match='input changed'):inputs.resolve(store,value,verify=mutate)


def test_publication_hook_cannot_remove_a_verified_package(setup):
    entry,store,snapshot=setup
    def mutate(stage):
        if stage=='after_publish':store.path(snapshot['packages'][0]['sha256']).unlink()
    store.fault_hook=mutate
    with pytest.raises(ContractError):inputs.input_record(store,entry)
