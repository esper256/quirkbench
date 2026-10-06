"""Public admission/dispatch with real source readers and disposable metadata."""
import json
import os

import pytest

from quirkbench import preparation,preparation_plan,cli
from quirkbench.commission import CommissionError
from quirkbench.contracts import canonical
from test_preparation_source import artifact
from test_preparation_plan import selected
from test_prepared_media import factory


def test_public_plan_joins_real_artifact_and_captured_selected_bytes(tmp_path,artifact,selected):
    image,manifest,candidate,key=artifact;value,fd=selected
    root=tmp_path/'controller';root.mkdir();output=tmp_path/'plan.json'
    image_alias=tmp_path/'image-alias';image_alias.symlink_to(image)
    def access(*argv,**kwargs):
        assert argv==('observe','--device',str(tmp_path/'selected-usb-fixture'))
        return {'device':value['device'],'observed_layout':value['observed_layout'],
            'metadata':[{'offset':entry['offset'],'hex':os.pread(fd,entry['length'],entry['offset']).hex()}
                        for entry in value['observed_layout']]}
    result=preparation.plan(root,image=image_alias,device=str(tmp_path/'selected-usb-fixture'),target='unrelated-laptop',output=output,
        unsigned_development=True,access=access,controller_reader=lambda root:value['controller'])
    saved=json.loads(output.read_bytes())
    assert result['written'] is False and result['confirmation']==preparation_plan.reference(saved)
    assert saved['factory']==manifest['commissioning'] and saved['source']['sha256']==candidate['image_sha256']
    assert 'path' not in saved['source']
    assert not {'path','sysfs_path','attachment_path'} & saved['device'].keys()
    assert saved['target']=='unrelated-laptop' and result['library_payload_bytes']==0
    assert not (root/'controller.sqlite').exists()


@pytest.mark.parametrize('fault',['device','source','factory','controller'])
def test_apply_revalidates_before_native_staging_or_invitation(selected,tmp_path,fault):
    (tmp_path/'image').touch();(tmp_path/'disk').touch()
    value,fd=selected;output=tmp_path/'staging';path=tmp_path/'plan';path.write_bytes(canonical(value))
    from copy import deepcopy
    source={'source':deepcopy(value['source']),'factory':deepcopy(value['factory'])}
    controller=deepcopy(value['controller']);device=deepcopy(value['device'])
    if fault=='source':source['source']['sha256']='f'*64
    elif fault=='factory':source['factory']['factory_data_end']-=1
    elif fault=='controller':controller['certificate_sha256']='f'*64
    else:device['diskseq']+=1
    with pytest.raises(CommissionError,match='changed'):
        preparation.apply(tmp_path,plan_path=path,confirmation=preparation_plan.reference(value),erase=True,output=output,image=tmp_path/'image',device=tmp_path/'disk',
            source_reader=lambda *a:source,controller_reader=lambda root:controller,
            access=lambda *a,**kw:{'device':device,'observed_layout':value['observed_layout'],'metadata':[]})
    assert not output.exists() and not (tmp_path/'controller.sqlite').exists()


@pytest.mark.parametrize('arguments',[[],['--plan','/tmp/plan'],['--image','/tmp/image','--erase']])
def test_missing_public_choices_never_prompt_or_create_state(tmp_path,monkeypatch,capsys,arguments):
    monkeypatch.setattr('builtins.input',lambda *_:pytest.fail('controller commands never prompt'))
    root=tmp_path/'absent'
    assert cli.main(['recovery','prepare','--json',*arguments], state_root=str(root))==2
    assert 'requires' in json.loads(capsys.readouterr().out)['error']['message']
    assert not root.exists()


def test_missing_selected_image_names_the_actionable_input(tmp_path):
    with pytest.raises(CommissionError,match='set --image to the completed build'):
        preparation.plan(tmp_path,image=tmp_path/'missing/recovery.img',device='/dev/not-selected',
            target='target',output=tmp_path/'plan',unsigned_development=True,
            access=lambda *a,**k:pytest.fail('missing image cannot inspect a device'))


@pytest.mark.parametrize('json_output',[False,True])
def test_real_plan_progress_is_human_unless_json_requested(tmp_path,artifact,selected,monkeypatch,capsys,json_output):
    from quirkbench import preparation_layout,image as image_tools
    image,manifest,candidate,key=artifact;value,fd=selected
    root=tmp_path/'state';root.mkdir()
    output=tmp_path/'plan.json'
    original=preparation.plan
    def access(*a,**k):
        return {'device':value['device'],'observed_layout':value['observed_layout'],
            'metadata':[{'offset':entry['offset'],'hex':os.pread(fd,entry['length'],entry['offset']).hex()}
                for entry in value['observed_layout']]}
    def joined(*a,**k):
        image_tools._emit('image-tool-sgdisk',status='running',timeout_s=30)
        image_tools._emit('image-tool-sgdisk',status='complete',output_bytes=127)
        return original(*a,**k,access=access,controller_reader=lambda _:value['controller'])
    monkeypatch.setattr(preparation,'plan',joined)
    command=['recovery','prepare','--image',str(image),'--device','/dev/selected-test',
        '--target','chromebook','--plan-out',str(output),'--unsigned-development']
    assert cli.main(command+(['--json'] if json_output else []),state_root=str(root))==0
    captured=capsys.readouterr()
    if json_output:
        assert json.loads(captured.out)['written'] is False
        assert all(isinstance(json.loads(line),dict) for line in captured.err.splitlines())
    else:
        assert 'Plan ready. The USB has not been changed.' in captured.out
        assert 'GiB' in captured.out and 'Apply:' in captured.out
        assert '{' not in captured.out+captured.err


def test_failed_plan_never_reports_completion_and_restores_progress(tmp_path,monkeypatch,capsys):
    from quirkbench.image import _image_event
    root=tmp_path/'state';root.mkdir()
    previous=_image_event.get()
    def fail(*args,**kwargs):raise CommissionError('selected device disappeared')
    monkeypatch.setattr(preparation,'plan',fail)
    assert cli.main(['recovery','prepare','--image','image','--device','device','--target','chromebook',
        '--plan-out',str(tmp_path/'plan'),'--unsigned-development'],state_root=str(root))==2
    captured=capsys.readouterr()
    assert 'Plan ready' not in captured.out and 'completed and verified' not in captured.out
    assert 'selected device disappeared' in captured.err
    assert _image_event.get() is previous
    assert not (tmp_path/'plan').exists()
