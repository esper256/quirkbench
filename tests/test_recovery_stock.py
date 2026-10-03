"""Stock recovery must never require candidate sources or invoke a compiler."""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from quirkbench.build import BuildError
from quirkbench.build_pipeline import ResourceLimits
from quirkbench.contracts import canonical, digest
from quirkbench.recovery_dracut import STOCK_DRACUT_CONFIG
from quirkbench.recovery_recipe import load_recipe, preflight_recipe
from quirkbench.recovery_rootfs import CASReader, _rpm_row, verify_stock_rpm_signatures
from quirkbench.recovery_stock import PROFILE, POLICY, validate_lock
from quirkbench.recovery_synthesis import run_recovery_base_stage
from quirkbench.store import ArtifactStore

ROOT = Path(__file__).resolve().parents[1]


def stock_fixture(tmp_path):
    from quirkbench.recovery_recipe import REQUIRED_UNITS
    from quirkbench.recovery_runtime_revision import capture_runtime_revision
    store = ArtifactStore(tmp_path / 'store', reserve_bytes=0)
    put = lambda raw: store.put(raw).sha256
    profile = put(canonical(PROFILE))
    release = '7.2.7-200.fc44.x86_64'
    packages = [{'name': name, 'nevra': name + '-0:' + release,
                 'sha256': put(name.encode())}
                for name in ['kernel-core', 'kernel-modules', 'kernel-modules-core', 'linux-firmware']]
    snapshot = {'schema_version': 1, 'packages': packages}
    lock = {'schema_version': 2, 'architecture': 'x86_64', 'fedora_release': '44',
            'builder_image_digest': 'sha256:' + 'a' * 64, 'kernel_release': release,
            'rpm_snapshot_sha256': put(canonical(snapshot)),
            'target_rpm_lock_sha256': put(('\n'.join(sorted(_rpm_row(p['name'], p['nevra']) for p in packages)) + '\n').encode()),
            'rpm_key_sha256': put(b'pinned key'), 'rpm_key_fingerprint': 'A' * 40,
            'storage_policy_sha256': profile}
    recipe = {'schema_version': 2, 'recipe_id': 'stock-recovery-test',
              'rootfs_lock_sha256': put(canonical(lock)), 'storage_policy_sha256': profile,
              'builder_image_digest': lock['builder_image_digest'],
              'dracut_config_sha256': put(STOCK_DRACUT_CONFIG),
              'runtime_revision_sha256': put(canonical(capture_runtime_revision(ROOT/'src/quirkbench', ROOT/'target-assets'))),
              'unit_allowlist_sha256': put(canonical({'schema_version': 1, 'units': sorted(REQUIRED_UNITS)})),
              'source_date_epoch': 1700000000,
              'layout': {'factory_size_mib': 4096, 'root_mib': 2048, 'experiment_mib': 32768,
                         'library_mib': 32768, 'log_budget_mib': 4096}, 'policy': copy.deepcopy(POLICY)}
    return recipe, lock, CASReader(tmp_path/'store'), store


def install_fixture(root, release):
    (root/'etc').mkdir(parents=True)
    (root/'etc/quirkbench-rootfs').write_text('quirkbench-fedora-target-v1\n')
    for name in ('openssl', 'gpg'):
        program = root/'usr/bin'/name
        program.parent.mkdir(parents=True, exist_ok=True)
        program.write_bytes(b'synthetic pairing program')
        program.chmod(0o755)
    modules = root/'lib/modules'/release
    modules.mkdir(parents=True)
    (modules/'vmlinuz').write_bytes(b'stock kernel')
    settings = ['CONFIG_'+name+'=y' for name in
                ['64BIT','EFI','EFI_STUB','BLK_DEV_INITRD','MODULES','DMI_SYSFS','DEVTMPFS',
                 'USB_XHCI_HCD','USB_STORAGE','BLK_DEV_SD','EXT4_FS','VFAT_FS','SATA_AHCI']]
    (modules/'config').write_text('\n'.join(settings)+'\n')
    (modules/'modules.dep').write_text('')
    (modules/'modules.builtin').write_text('\n'.join('kernel/'+name+'.ko' for name in
                                           PROFILE['required_boot_drivers'])+'\n')
    return root


def test_v2_preflight_without_catalog_or_sources_and_schema(tmp_path):
    recipe, lock, reader, _ = stock_fixture(tmp_path)
    checked = preflight_recipe(recipe, None, reader)
    assert checked['rootfs_lock'] == lock
    assert 'baseline_id' not in recipe and 'kernel_srpm_sha256' not in recipe
    assert load_recipe(canonical(recipe)) == recipe
    for name, value in [('recovery-recipe', recipe), ('recovery-rootfs-lock', lock)]:
        schema = json.loads((ROOT/'schemas'/f'{name}.v2.schema.json').read_text())
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(value)


@pytest.mark.parametrize('field', ['rootfs_lock_sha256', 'storage_policy_sha256', 'runtime_revision_sha256',
                                   'dracut_config_sha256', 'unit_allowlist_sha256'])
def test_stock_missing_input_blocks(tmp_path, field):
    recipe, _, reader, _ = stock_fixture(tmp_path)
    recipe[field] = '0'*64
    with pytest.raises((BuildError, ValueError)):
        preflight_recipe(recipe, None, reader)


def test_mismatched_stock_packages_and_policy_block(tmp_path):
    recipe, lock, reader, store = stock_fixture(tmp_path)
    lock['kernel_release'] = '7.2.8-200.fc44.x86_64'
    recipe['rootfs_lock_sha256'] = store.put(canonical(lock)).sha256
    with pytest.raises(BuildError, match='releases differ'):
        preflight_recipe(recipe, None, reader)
    recipe['policy']['userspace_internal_block_access'] = True
    with pytest.raises(BuildError, match='policy'):
        load_recipe(canonical(recipe))


def test_base_stages_stock_payload_without_runner_or_sources(tmp_path):
    recipe, lock, reader, _ = stock_fixture(tmp_path)
    def install(catalog, passed_lock, store, root):
        assert catalog is None and passed_lock == lock
        return install_fixture(root, lock['kernel_release'])
    class NoCompiler:
        def run(self, *args, **kwargs):
            pytest.fail('stock base stage invoked a build command')
    stage = tmp_path/'stock'
    result = run_recovery_base_stage(recipe, None, reader, stage, runner=NoCompiler(),
                                    limits=ResourceLimits(cpus=1, memory_bytes=4*1024**3, jobs=1), rootfs_installer=install)
    assert result['schema_version'] == 2
    assert not (stage/'source').exists() and not (stage/'kernel-obj').exists()
    assert result['kernel_stage']['module_audit']['builtin_count'] == 5
    from quirkbench.recovery_stock_pipeline import verify_base
    verify_base(recipe, reader, stage, result)
    (stage/'rootfs/lib/modules'/lock['kernel_release']/'vmlinuz').write_bytes(b'changed')
    with pytest.raises(BuildError, match='installed stock payload'):
        verify_base(recipe, reader, stage, result)


def test_stock_signature_checks_use_only_pinned_key_and_private_database(tmp_path):
    _, lock, reader, _ = stock_fixture(tmp_path)
    stage = tmp_path/'signature'; stage.mkdir()
    commands = []
    def runner(argv, timeout):
        commands.append(argv)
        if argv[0] == 'rpm':
            raise BuildError('redundant RPM initialization is unavailable')
        if argv[0] == 'gpg':
            return 'pub:::::::::\nfpr:::::::::'+lock['rpm_key_fingerprint']+':\n'
        if '--checksig' in argv:
            return 'package.rpm: digests signatures OK\n'
        return ''
    verify_stock_rpm_signatures(lock, reader, ['first.rpm', 'second.rpm'], stage, runner)
    assert [argv[-1] for argv in commands if '--checksig' in argv] == ['first.rpm', 'second.rpm']
    assert [argv for argv in commands if '--import' in argv and argv[0] == 'rpmkeys'] == [
        ['rpmkeys', '--dbpath', str(stage/'signature-rpmdb'), '--import', str(stage/'rpm-signing-key.asc')]]
    assert all(str(stage/'signature-rpmdb') in argv for argv in commands if argv[0] in {'rpm','rpmkeys'})


@pytest.mark.parametrize('reply', ['package.rpm: digests OK', 'package.rpm: NOKEY', 'package.rpm: BAD signatures OK'])
def test_unsigned_or_untrusted_rpm_is_rejected(tmp_path, reply):
    _, lock, reader, _ = stock_fixture(tmp_path)
    stage=tmp_path/'signature'; stage.mkdir()
    def runner(argv, timeout):
        if argv[0]=='gpg': return 'pub:::::::::\nfpr:::::::::'+lock['rpm_key_fingerprint']+':\n'
        return reply if '--checksig' in argv else ''
    with pytest.raises(BuildError, match='signature'):
        verify_stock_rpm_signatures(lock, reader, ['package.rpm'], stage, runner)


def test_stock_cache_reuses_only_identical_stock_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr("quirkbench.build_cache.RESERVE", 0)
    from quirkbench.build_cache import BuildStageCache
    recipe, lock, reader, _ = stock_fixture(tmp_path)
    installed=[]
    def install(catalog, passed, store, root):
        installed.append(root)
        return install_fixture(root, lock['kernel_release'])
    cache=BuildStageCache(tmp_path/'cache')
    limits=ResourceLimits(cpus=1,memory_bytes=4*1024**3,jobs=1)
    for name in ['first', 'second']:
        result=run_recovery_base_stage(recipe,None,reader,tmp_path/name,runner=None,limits=limits,
                                      rootfs_installer=install,cache=cache)
        assert result['rootfs']==str(tmp_path/name/'rootfs')
    assert len(installed)==1
    assert not (tmp_path/'second/source').exists()


def test_stock_profile_rejects_boolean_integer_substitution():
    from quirkbench.recovery_stock import validate_policy
    value=copy.deepcopy(PROFILE)
    value['policy']['passive_kernel_metadata']=1
    with pytest.raises(BuildError): validate_policy(value)


def test_stock_worker_staging_has_no_catalog_or_source_dependencies(tmp_path):
    from quirkbench.recovery_podman import stage_rootfs_inputs
    from quirkbench.operations import operation_intent,recovery_rootfs_arguments
    recipe,lock,reader,store=stock_fixture(tmp_path)
    stage=tmp_path/'worker'; stage.mkdir(mode=0o700)
    stage_rootfs_inputs(catalog_sha256=None,lock_sha256=recipe['rootfs_lock_sha256'],
                        cas_root=reader.objects.parent,stage=stage)
    assert not (stage/'inputs/catalog.json').exists()
    staged=CASReader(stage/'inputs/cas')
    assert preflight_recipe(recipe,None,reader)['rootfs_lock']==lock
    from quirkbench.recovery_stock import preflight_lock
    assert preflight_lock(lock,staged)[0]['kernel_release']==lock['kernel_release']
    args={'schema_version':2,'builder_config_digest':'sha256:'+'a'*64,
          'builder_archive_sha256':'b'*64,'rootfs_lock_sha256':recipe['rootfs_lock_sha256']}
    intent=operation_intent('image_prepare',args,input_refs=[args['builder_archive_sha256'],args['rootfs_lock_sha256']])[0]
    assert recovery_rootfs_arguments(intent)==args
    intent['arguments']['catalog_sha256']='c'*64
    with pytest.raises(ValueError): recovery_rootfs_arguments(intent)


def test_fixed_stock_operation_retains_transitive_closure_across_backup(tmp_path):
    from quirkbench.controller import Controller
    from test_recovery_podman import builder_archive
    recipe,lock,reader,store=stock_fixture(tmp_path/'inputs')
    controller=Controller(tmp_path/'controller',reserve_bytes=0)
    for path in store.objects.iterdir(): controller.store.put_file(path)
    archive=controller.store.put(builder_archive()).sha256
    lock_digest=controller.store.put(canonical(lock)).sha256
    arguments={'schema_version':2,'builder_config_digest':lock['builder_image_digest'],
               'builder_archive_sha256':archive,'rootfs_lock_sha256':lock_digest}
    result=controller.admit_operation('stock','image_prepare',arguments,input_refs=[archive,lock_digest])
    snapshot=json.loads(store.get(lock['rpm_snapshot_sha256']))
    closure={lock[key] for key in lock if key.endswith('_sha256')}|{p['sha256'] for p in snapshot['packages']}
    assert closure<=set(result['references']['input'])
    assert result==controller.admit_operation('stock','image_prepare',arguments,input_refs=[archive,lock_digest])
    controller.backup(tmp_path/'backup')
    restored=Controller.restore(tmp_path/'backup',tmp_path/'restored',reserve_bytes=0)
    from quirkbench.recovery_stock import preflight_lock
    assert preflight_lock(lock,restored.store)[1]==snapshot['packages']


def test_signature_status_does_not_treat_cas_filename_as_error(tmp_path):
    _, lock, reader, _ = stock_fixture(tmp_path)
    stage=tmp_path/'signature'; stage.mkdir()
    def runner(argv, timeout):
        if argv[0]=='gpg': return 'pub:::::::::\nfpr:::::::::'+lock['rpm_key_fingerprint']+':\n'
        return '/private/cas/objects/bad123: digests signatures OK\n' if '--checksig' in argv else ''
    verify_stock_rpm_signatures(lock,reader,['/private/cas/objects/bad123'],stage,runner)


@pytest.mark.parametrize('name', ['openssl', 'gpg'])
def test_stock_staging_rejects_missing_pairing_tool_before_cache_publication(tmp_path, name):
    recipe,lock,reader,_=stock_fixture(tmp_path)
    def install(catalog, passed, store, root):
        install_fixture(root,lock['kernel_release'])
        (root/'usr/bin'/name).unlink()
        return root
    with pytest.raises(BuildError,match='pairing executable.*'+name):
        run_recovery_base_stage(recipe,None,reader,tmp_path/'stock',runner=None,
            limits=ResourceLimits(1,4*1024**3,1),rootfs_installer=install)
    assert not (tmp_path/'stock/artifacts').exists()
