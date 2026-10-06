"""Selected candidates and retained repository bytes, without downloading packages."""
import copy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from quirkbench.build import BuildError
from quirkbench.contracts import canonical,digest
from quirkbench.recovery_acquisition import load_spec,stage_spec,acquisition_command
from quirkbench.recovery_inputs import recorded_packages,retain_packages


def spec():
    content='[reviewed-fedora]\nname=Reviewed Fedora\nbaseurl=https://example.invalid/fedora/$releasever/$basearch\ngpgcheck=1\n'
    return {'schema_version':1,'candidate_id':'reviewed-fedora-candidate',
            'platform_adapter_id':'x86_64-uefi-usb-v1','fedora_release':'44',
            'kernel_release':'7.2.8-200.fc44.x86_64','rpm_key_fingerprint':'A'*40,
            'packages':recorded_packages(),
            'repositories':[{'name':'reviewed.repo','content':content,'sha256':digest(content.encode()),'ids':['reviewed-fedora']}]}


def test_selected_candidate_does_not_reuse_historical_kernel_or_host_repositories(tmp_path):
    selected=spec();stage_spec(selected,tmp_path)
    argv=acquisition_command(tmp_path/'rpms',selected)
    assert 'kernel-core-7.2.8-200.fc44.x86_64' in argv
    assert '--setopt=reposdir='+str(tmp_path/'repositories') in argv
    assert not any('/etc/yum.repos.d' in value for value in argv)
    assert '--enablerepo=reviewed-fedora' in argv
    assert load_spec((tmp_path/'acquisition-spec.v1.json').read_bytes())==selected


def test_changed_repository_bytes_block_execution(tmp_path):
    selected=spec();stage_spec(selected,tmp_path)
    (tmp_path/'repositories/reviewed.repo').write_text('[changed]\n')
    with pytest.raises(BuildError,match='repository differs'):acquisition_command(tmp_path/'rpms',selected)


@pytest.mark.parametrize('field,value',[('platform_adapter_id','unsupported-arm'),('kernel_release','7.2.8-200.fc45.x86_64'),('rpm_key_fingerprint','unknown')])
def test_unknown_platform_or_mismatched_identity_blocks(field,value):
    selected=spec();selected[field]=value
    with pytest.raises(BuildError):load_spec(canonical(selected))


def test_spec_repo_identity_and_schema():
    import json
    root=Path(__file__).resolve().parents[1]
    schema=json.loads((root/'schemas/recovery-acquisition.v1.schema.json').read_bytes())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(spec())
    selected=spec();selected['repositories'][0]['content']+='changed'
    with pytest.raises(BuildError,match='pinned identity'):load_spec(canonical(selected))


def test_registered_spec_cannot_change_or_disappear(tmp_path):
    from quirkbench.controller import Controller
    from quirkbench.recovery_acquisition import bound_spec
    from quirkbench.retention import register
    root=tmp_path/'state';controller=Controller(root,reserve_bytes=0)
    generation=root/'inputs/one';generation.mkdir(parents=True)
    selected=spec();stage_spec(selected,generation)
    expected=controller.store.put(canonical(selected)).sha256
    owner='storage:acquisition-v1:'+expected+':'+'a'*32
    register(root,'input',[expected],owner=owner,paths=(generation,),state='WAITING')
    assert bound_spec(root,owner,generation)==selected
    changed=copy.deepcopy(selected);changed['rpm_key_fingerprint']='B'*40
    path=generation/'acquisition-spec.v1.json';path.write_bytes(canonical(changed))
    with pytest.raises(BuildError,match='planned identity'):bound_spec(root,owner,generation)
    path.unlink()
    with pytest.raises(FileNotFoundError):bound_spec(root,owner,generation)


def test_legacy_command_freezes_repositories_before_download(tmp_path):
    from quirkbench.recovery_acquisition import freeze_legacy_spec
    repository=tmp_path/'fedora.repo';repository.write_text('[fedora]\nbaseurl=https://example.invalid/fedora\n[updates]\nbaseurl=https://example.invalid/updates\n')
    selected=freeze_legacy_spec(tmp_path)
    stage=tmp_path/'generation';stage.mkdir();stage_spec(selected,stage)
    repository.write_text('[replaced]\n')
    assert '--enablerepo=fedora' in acquisition_command(stage/'rpms',selected)
    assert '[fedora]' in (stage/'repositories/fedora.repo').read_text()


def test_additional_repository_cannot_override_pinned_enabled_id(tmp_path):
    selected=spec();stage_spec(selected,tmp_path)
    (tmp_path/'repositories/extra.repo').write_text('[reviewed-fedora]\nbaseurl=https://example.invalid/override\n')
    with pytest.raises(BuildError,match='directory differs'):acquisition_command(tmp_path/'rpms',selected)


def test_pairing_candidate_preserves_historical_inputs_and_requests_executable(tmp_path, capsys):
    import json
    from quirkbench.cli import main
    from quirkbench.recovery_acquisition import stock_candidate_spec
    repository = tmp_path/'retained.repo'
    repository.write_text('[retained-stock]\nbaseurl=file:///retained/exact-rpms\ngpgcheck=1\n')
    selected = stock_candidate_spec('fedora44-pairing-v1', repository, ['retained-stock'])
    historical = {p['name']:p for p in recorded_packages()}
    current = {p['name']:p for p in selected['packages']}
    assert 'openssl' not in historical
    assert {name:p for name,p in current.items() if name != 'openssl'} == historical
    assert current['openssl']['nevra'] == 'openssl-1:3.5.8-1.fc44.x86_64'
    assert current['openssl-libs'] == historical['openssl-libs']
    assert current['gnupg2'] == historical['gnupg2']
    assert main(['dev', 'recovery', 'inputs', 'candidate-spec', '--candidate', 'fedora44-pairing-v1', '--repository', str(repository), '--repository-id', 'retained-stock'], state_root=str(tmp_path / 'unused-state')) == 0
    assert json.loads(capsys.readouterr().out) == selected
    assert not (tmp_path/'unused-state').exists()
    generation=tmp_path/'generation';generation.mkdir();stage_spec(selected,generation)
    argv=acquisition_command(generation/'rpms',selected)
    assert current['openssl']['nevra'] in argv
    assert '--enablerepo=retained-stock' in argv
    assert selected['repositories'][0]['sha256'] == digest(repository.read_bytes())


def test_pairing_candidate_refuses_unknown_repository_id(tmp_path):
    from quirkbench.recovery_acquisition import stock_candidate_spec
    repository=tmp_path/'retained.repo';repository.write_text('[retained-stock]\nbaseurl=file:///exact\n')
    with pytest.raises(BuildError,match='ID missing'):
        stock_candidate_spec('fedora44-pairing-v1',repository,['unknown'])


def test_candidate_spec_repository_ancestor_loop_is_command_error(tmp_path, capsys):
    from quirkbench.cli import main
    loop=tmp_path/'loop';loop.symlink_to('loop')
    assert main(['dev', 'recovery', 'inputs', 'candidate-spec', '--candidate', 'fedora44-pairing-v1', '--repository', str(loop / 'selected.repo'), '--repository-id', 'selected'], state_root=str(tmp_path / 'unused-state')) == 2
    output=capsys.readouterr()
    assert output.out == '' and 'Stock specification unavailable:' in output.err
    assert not (tmp_path/'unused-state').exists()
