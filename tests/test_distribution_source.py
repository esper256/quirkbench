"""Prepared distribution source import; all RPM/container work is injected."""
import json
import os
from pathlib import Path
import pytest
from quirkbench import distribution_source as distro
from quirkbench.build_pipeline import _tree_hash
from quirkbench.contracts import Conflict, ContractError
from quirkbench.source_capture import _git
from quirkbench.store import ArtifactStore
from test_source_capture import git


@pytest.fixture
def prepared(tmp_path):
    store = ArtifactStore(tmp_path/'state/artifacts', reserve_bytes=0)
    package = store.put(b'explicitly injected package fixture')
    entry = json.loads((Path(__file__).resolve().parents[1]/'examples/baseline-catalog.json').read_bytes())['entries'][0]
    entry['kernel_srpm_sha256'] = package.sha256
    stage = tmp_path/'prepared'; stage.mkdir(mode=0o700)
    source = stage/'source'; source.mkdir()
    for name, data in {'Makefile':'fixture kernel', 'driver.c':'prepared distribution patches', '.gitignore':'ignored.c',
                       'ignored.c':'force imported source'}.items():
        (source/name).write_text(data)
    (source/'scripts').mkdir(); (source/'scripts/tool').write_text('executable'); (source/'scripts/tool').chmod(0o755)
    (source/'link').symlink_to('driver.c')
    inputs = stage/'rpm-topdir'
    (inputs/'SPECS').mkdir(parents=True); (inputs/'SOURCES').mkdir()
    spec = inputs/'SPECS/kernel.spec'; spec.write_text('reviewed fixture prep')
    (inputs/'SOURCES/distribution.patch').write_text('explicit retained distribution patch')
    (inputs/'SOURCES/upstream.tar.xz').write_bytes(b'fixture upstream archive')
    from quirkbench.build import sha256_file
    record = {'schema_version':1, 'kernel_srpm_sha256':package.sha256, 'kernel_source_nevra':entry['kernel_source_nevra'],
              'spec_sha256':sha256_file(spec), 'source':str(source), 'source_tree_sha256':_tree_hash(source, excluded_paths=frozenset()),
              'source_date_epoch':1700000000}
    return entry, record, stage, tmp_path/'import', store


def run(prepared, **kwargs):
    entry, record, source_stage, stage, store = prepared
    return distro.import_prepared(entry, record, source_stage, stage, store, 'kernel', verify=kwargs.pop('verify', lambda:None), **kwargs)


def test_private_git_base_retains_exact_package_patch_provenance_and_modes(prepared):
    entry, record, source_stage, stage, store = prepared
    result = run(prepared); workspace = stage/'output/workspace'
    assert result['base_oid'] == git(workspace, 'rev-parse', 'HEAD')
    assert git(workspace, 'rev-parse', '--abbrev-ref', 'HEAD') == 'HEAD'
    assert (workspace/'driver.c').read_text() == 'prepared distribution patches'
    assert (workspace/'ignored.c').read_text() == 'force imported source'
    assert (workspace/'scripts/tool').stat().st_mode & 0o777 == 0o755
    assert (workspace/'link').readlink() == Path('driver.c')
    assert not result['allowed_untracked']
    assert git(workspace, 'status', '--porcelain') == ''
    assert git(workspace, 'show', '-s', '--format=%an <%ae> %at %ct') == 'Quirkbench source import <source-import@quirkbench.invalid> 1700000000 1700000000'
    capture = json.loads(store.get(result['capture_sha256']))
    provenance = distro.validate(json.loads(store.get(capture['provenance']['distribution_patches_sha256'])))
    assert provenance['source_package_sha256'] == entry['kernel_srpm_sha256']
    assert provenance['upstream_relationship']['upstream_base_oid'] is None
    assert {item['path'] for item in provenance['package_files']} == {'SPECS/kernel.spec','SOURCES/distribution.patch','SOURCES/upstream.tar.xz'}
    for digest in distro.references(provenance): store.verify(digest)
    assert json.loads(store.get(provenance['baseline_sha256'])) == entry


def test_import_base_is_reproducible_from_same_prepared_inputs(prepared, tmp_path):
    import shutil
    entry, record, source_stage, stage, store = prepared
    copy = tmp_path/'second-prepared'; shutil.copytree(source_stage, copy, symlinks=True); copy.chmod(0o700)
    result = run(prepared)
    record = {**record,'source':str(copy/'source')}
    other = distro.import_prepared(entry, record, copy, tmp_path/'second-import', store, 'second', verify=lambda:None)
    assert other['base_oid'] == result['base_oid']
    assert other['capture_sha256'] == result['capture_sha256']


@pytest.mark.parametrize('mutation', ['wrong-package','wrong-tree','changed-spec','absolute-link','fifo','hardlink','credentials','existing-git'])
def test_changed_or_unsafe_distribution_input_never_yields_preparation(prepared, mutation):
    entry, record, source_stage, stage, store = prepared; source = source_stage/'source'
    if mutation == 'wrong-package': entry['kernel_srpm_sha256'] = 'f'*64
    elif mutation == 'wrong-tree': (source/'driver.c').write_text('changed source')
    elif mutation == 'changed-spec': (source_stage/'rpm-topdir/SPECS/kernel.spec').write_text('changed spec')
    elif mutation == 'absolute-link': (source/'bad-link').symlink_to('/etc/passwd')
    elif mutation == 'fifo': os.mkfifo(source/'blocked')
    elif mutation == 'hardlink': os.link(source/'driver.c', source/'duplicate.c')
    elif mutation == 'credentials': (source/'credential.key').write_text('fixture secret')
    elif mutation == 'existing-git': (source/'.git').mkdir()
    with pytest.raises((Conflict, ContractError)): run(prepared)
    assert not (stage/'output/workspace').exists()


def test_lost_claim_stops_before_git_metadata(prepared):
    entry, record, source_stage, stage, store = prepared
    def lost(): raise Conflict('claim expired')
    with pytest.raises(Conflict, match='claim expired'): run(prepared, verify=lost)
    assert not (source_stage/'source/.git').exists()


def test_changed_package_during_final_callbacks_is_rejected(prepared):
    entry, record, source_stage, stage, store = prepared; changed = [False]
    def mutate():
        if (stage/'output/workspace/.git/HEAD').exists() and not changed[0]:
            changed[0] = True
            (source_stage/'rpm-topdir/SOURCES/distribution.patch').write_text('late changed patch')
    with pytest.raises(Conflict, match='package inputs changed'): run(prepared, verify=mutate)
    assert changed[0]


def test_source_root_link_cannot_change_outside_mode(prepared, tmp_path):
    entry, record, source_stage, stage, store = prepared
    source = source_stage/'source'; source.rename(source_stage/'saved-source')
    outside = tmp_path/'outside'; outside.mkdir(mode=0o755); source.symlink_to(outside)
    original_mode = outside.stat().st_mode
    with pytest.raises(Conflict, match='root is linked'): run(prepared)
    assert outside.stat().st_mode == original_mode and list(outside.iterdir()) == []


def test_import_identity_is_fixed_and_not_a_general_git_environment(prepared):
    root = prepared[2]/'source'
    with pytest.raises(ContractError, match='fixed distribution import'):
        _git(root, ['status'], lambda:None, import_epoch=1700000000)


def test_versioned_schema_example_and_reader_agree():
    from jsonschema import Draft202012Validator
    root = Path(__file__).resolve().parents[1]
    value = json.loads((root/'examples/distribution-source-provenance.json').read_bytes())
    schema = json.loads((root/'schemas/distribution-source-provenance.v1.schema.json').read_bytes())
    Draft202012Validator(schema).validate(value); distro.validate(value)
    with pytest.raises(ContractError): distro.validate({**value,'upstream_relationship':{'kind':'upstream-commit','upstream_base_oid':'f'*40}})


def test_moved_serialization_receives_no_outside_writes(prepared, tmp_path):
    entry, record, source_stage, stage, store = prepared
    moved = tmp_path/'outside-serialization'; attacked = [False]
    def mutate():
        for path in stage.glob('.distribution-input-*/input'):
            if not attacked[0]:
                attacked[0] = True; path.rename(moved)
                # A replacement with the exact approved bytes must not make the
                # retained output descriptor safe to continue writing.
                path.write_bytes((source_stage/'rpm-topdir/SPECS/kernel.spec').read_bytes()); path.chmod(0o600)
    with pytest.raises(Conflict, match='serialization output moved'): run(prepared, verify=mutate)
    assert attacked[0] and moved.read_bytes() == b''


@pytest.mark.parametrize('phase', ['init', 'add'])
def test_callback_injected_git_filter_never_executes(prepared, tmp_path, monkeypatch, phase):
    entry, record, source_stage, stage, store = prepared; source = source_stage/'source'
    (source/'.gitattributes').write_text('driver.c filter=escape\n')
    record['source_tree_sha256'] = _tree_hash(source, excluded_paths=frozenset())
    marker = tmp_path/'unapproved-filter-executed'; active = [False]; attacked = [False]
    original = distro._git
    def native(root, arguments, verify, **kwargs):
        active[0] = arguments[0] == phase
        return original(root, arguments, verify, **kwargs)
    monkeypatch.setattr(distro, '_git', native)
    def mutate():
        if active[0] and not attacked[0]:
            attacked[0] = True; directory = source/'.git'; directory.mkdir(exist_ok=True)
            config = directory/'config'
            config.write_text('[core]\nrepositoryformatversion=0\nfilemode=true\nbare=false\nlogallrefupdates=true\n'
                              '[filter "escape"]\nclean=touch '+str(marker)+'; cat\n')
    with pytest.raises(ContractError, match='Git metadata|Git configuration'): run(prepared, verify=mutate)
    assert attacked[0] and not marker.exists()


@pytest.mark.parametrize('node', ['logs', 'object-bucket'])
def test_nested_git_links_never_redirect_native_writes(prepared, tmp_path, monkeypatch, node):
    entry, record, source_stage, stage, store = prepared; source = source_stage/'source'
    outside = tmp_path/'outside-git'; outside.mkdir(); attacked = [False]; active = [False]
    original = distro._git
    def native(root, arguments, verify, **kwargs):
        active[0] = arguments[0] == ('commit' if node == 'logs' else 'add')
        return original(root, arguments, verify, **kwargs)
    monkeypatch.setattr(distro, '_git', native)
    def mutate():
        if active[0] and not attacked[0]:
            attacked[0] = True
            if node == 'logs':
                (source/'.git/logs').symlink_to(outside)
            else:
                # A valid object's two-hex bucket must be rejected even when
                # its target is a directory and top-level objects is unchanged.
                import hashlib
                data = (source/'driver.c').read_bytes()
                digest = hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
                (source/'.git/objects'/digest[:2]).symlink_to(outside)
    with pytest.raises(ContractError, match='Git tree is linked'): run(prepared, verify=mutate)
    assert attacked[0] and list(outside.iterdir()) == []


@pytest.mark.parametrize('name',['packed-refs.lock','AUTO_MERGE.lock'])
def test_checkout_locks_are_allowed_only_during_native_import_and_never_linked(prepared,name):
    run(prepared);source = prepared[2]/'source';lock = source/'.git'/name
    lock.write_bytes(b'')
    distro.import_git_policy(source,active=True)
    with pytest.raises(ContractError,match='namespace'):distro.import_git_policy(source)
    lock.unlink();lock.symlink_to('HEAD')
    with pytest.raises(ContractError,match='Git tree is linked'):distro.import_git_policy(source,active=True)
