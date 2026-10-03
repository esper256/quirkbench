"""Exact RPM inventory is neither signature acceptance nor dependency resolution."""
import json
from pathlib import Path

import pytest

from quirkbench.build import BuildError
from quirkbench.contracts import canonical,digest
from quirkbench.recovery_replay import check_replay
from test_recovery_acquisition import spec


def inputs(tmp_path):
    selected=spec()
    selected['packages']=[{'name':'glibc-common','nevra':'glibc-common-0:2.44-1.fc44.x86_64',
                           'sha256':digest(b'glibc-common')}]
    directory=tmp_path/'retained';directory.mkdir()
    nevras={'glibc-common':selected['packages'][0]['nevra']}
    for name in ('kernel-core','kernel-modules-core','kernel-modules','kernel-modules-extra'):
        nevras[name]=name+'-0:'+selected['kernel_release']
    for name in nevras:(directory/(name+'.rpm')).write_bytes(name.encode())
    def query(argv):
        name=Path(argv[-1]).stem
        return name+'\t'+nevras[name]+'\n'
    return selected,directory,query


def test_retained_exact_inputs_without_dependency_or_signature_claim(tmp_path):
    selected,directory,query=inputs(tmp_path)
    (directory/'repodata').mkdir()
    result=check_replay(selected,directory,query=query)
    assert result['selected_inputs_available'] and result['files_checked']==5
    assert result['missing']==result['mismatched']==[]
    assert 'inventory-only' in result['qualification']


@pytest.mark.parametrize('change',['missing','bytes','version'])
def test_unavailable_dependency_reports_exact_identity_and_observation(tmp_path,change):
    selected,directory,query=inputs(tmp_path)
    package=directory/'glibc-common.rpm'
    if change=='missing':package.unlink()
    if change=='bytes':package.write_bytes(b'changed signed representation')
    if change=='version':
        original=query
        query=lambda argv: original(argv).replace('2.44-1','2.44-2')
    result=check_replay(selected,directory,query=query)
    assert not result['selected_inputs_available']
    assert result['missing']==selected['packages']
    if change!='missing':
        assert result['mismatched'][0]['expected']==selected['packages'][0]
        assert result['mismatched'][0]['observed']['file']==str(package)


def test_kernel_input_missing_is_not_hidden_by_userspace_availability(tmp_path):
    selected,directory,query=inputs(tmp_path)
    (directory/'kernel-modules-extra.rpm').unlink()
    result=check_replay(selected,directory,query=query)
    assert result['missing']==[{'name':'kernel-modules-extra',
        'nevra':'kernel-modules-extra-0:'+selected['kernel_release'],'sha256':None}]


def test_inventory_refuses_rpm_leaf_alias(tmp_path):
    selected,directory,query=inputs(tmp_path)
    path=directory/'glibc-common.rpm';path.unlink();path.symlink_to(tmp_path/'other')
    with pytest.raises(BuildError,match='direct regular'):
        check_replay(selected,directory,query=query)


def test_inventory_cli_is_read_only_and_blocks_incomplete_inputs(tmp_path,monkeypatch,capsys):
    from quirkbench.cli import main
    selected,directory,query=inputs(tmp_path)
    specfile=tmp_path/'candidate.json';specfile.write_bytes(canonical(selected))
    import quirkbench.recovery_replay as replay
    original=replay.check_replay
    monkeypatch.setattr(replay,'check_replay',lambda spec,path:original(spec,path,query=query))
    args=['--state',str(tmp_path/'unused'),'recovery-inputs','replay-check','--spec',str(specfile),
          '--directory',str(directory)]
    assert main(args)==0
    assert json.loads(capsys.readouterr().out)['selected_inputs_available']
    (directory/'glibc-common.rpm').unlink()
    assert main(args)==4
    assert not json.loads(capsys.readouterr().out)['selected_inputs_available']
    assert not (tmp_path/'unused').exists()


def test_malformed_rpm_inspection_is_actionable(tmp_path):
    import subprocess
    selected,directory,_=inputs(tmp_path)
    def query(argv):raise subprocess.CalledProcessError(1,argv)
    with pytest.raises(BuildError,match='cannot inspect retained RPM: glibc-common.rpm'):
        check_replay(selected,directory,query=query)


def test_declared_other_kernel_release_cannot_hide_requested_kernel(tmp_path):
    selected,directory,query=inputs(tmp_path)
    old='kernel-core-0:7.2.6-200.fc44.x86_64'
    selected['packages'].append({'name':'kernel-core','nevra':old,'sha256':digest(b'kernel-core')})
    original=query
    def query(argv):
        return 'kernel-core\t'+old+'\n' if Path(argv[-1]).stem=='kernel-core' else original(argv)
    result=check_replay(selected,directory,query=query)
    assert not result['selected_inputs_available']
    assert {'name':'kernel-core','nevra':'kernel-core-0:'+selected['kernel_release'],
            'sha256':None} in result['missing']
