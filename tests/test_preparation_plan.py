"""Device-bound confirmation through real held-file layout observations."""
from copy import deepcopy
import os
import uuid

import pytest

from quirkbench import preparation_plan as plans,prepared_factory
from quirkbench.commission import CommissionError
from test_prepared_media import factory


@pytest.fixture
def selected(tmp_path,factory):
    disk=tmp_path/'selected-usb-fixture'
    size=32_000_000_000
    with disk.open('xb') as stream:stream.truncate(size)
    fd=os.open(disk,os.O_RDWR)
    device={
            'major_minor':[8,0],'device_bytes':size,'logical_sector_bytes':512,
            'controller_boot_id':str(uuid.uuid4()),'diskseq':31,
            'usb_busnum':1,'usb_devnum':5}
    parts=[{'start':start,'end':end} for start,end in zip(factory.partition_starts[:3],factory.fixed_ends)]
    parts.append({'start':factory.partition_starts[3],'end':8388574})
    identity=prepared_factory.record(factory.disk_guid,factory.partition_uuids,parts)
    source={'sha256':'a'*64,'size_bytes':4*1024**3,
            'manifest_sha256':'b'*64,'authentication':{'mode':'unsigned-development','trust_sha256':None,'fingerprint':None}}
    controller={'controller_url':'https://192.0.2.49:8443','certificate_sha256':'c'*64}
    value=plans.make(preparation_id='prepare-fixture',target='other-computer',source=source,
        factory=identity,device=device,observed_layout=plans.observe_layout(fd,size),controller=controller)
    try:yield value,fd
    finally:os.close(fd)


def test_confirmation_has_no_time_or_ram_admission_and_binds_whole_erase(selected):
    value,fd=selected
    reference=plans.reference(value)
    assert plans.require_apply(value,reference,True)==value
    assert value['prepared_media']['completion']=='PREPARING'
    assert 'target_ram_mib' not in value
    plans.revalidate(value,fd,device_observer=lambda:value['device'],
        source_reader=lambda:{'source':value['source'],'factory':value['factory']},controller_reader=lambda:value['controller'])
    with pytest.raises(CommissionError,match='erase acknowledgement'):
        plans.require_apply(value,reference,False)
    with pytest.raises(CommissionError,match='confirmation differs'):
        plans.require_apply(value,'f'*64,True)


@pytest.mark.parametrize('offset',['primary','backup'])
def test_live_layout_byte_mutation_invalidates_confirmation(selected,offset):
    value,fd=selected
    position=0 if offset=='primary' else value['device']['device_bytes']-512
    os.pwrite(fd,b'changed-gpt',position)
    with pytest.raises(CommissionError,match='changed; obtain a fresh plan'):
        plans.revalidate(value,fd,device_observer=lambda:value['device'],
            source_reader=lambda:{'source':value['source'],'factory':value['factory']},controller_reader=lambda:value['controller'])


@pytest.mark.parametrize('field',['device','source','controller'])
def test_replacement_attachment_source_or_trust_requires_new_plan(selected,field):
    value,fd=selected
    changed=deepcopy(value[field])
    if field=='device':changed['diskseq']+=1
    elif field=='source':changed['sha256']='d'*64
    else:changed['certificate_sha256']='e'*64
    readers={name:(lambda name=name:changed if name==field else value[name])
             for name in ('device','source','controller')}
    with pytest.raises(CommissionError,match='fresh plan'):
        plans.revalidate(value,fd,device_observer=readers['device'],
            source_reader=lambda:{'source':readers['source'](),'factory':value['factory']},controller_reader=readers['controller'])


@pytest.mark.parametrize('field',['device','source','controller','prepared_media','factory','target'])
def test_confirmation_reference_covers_each_consequential_input(selected,field):
    value,fd=selected;changed=deepcopy(value)
    if field=='device':changed[field]['diskseq']+=1
    elif field=='source':changed[field]['manifest_sha256']='f'*64
    elif field=='controller':changed[field]['controller_url']='https://192.0.2.50:8443'
    elif field=='prepared_media':
        changed['device']['device_bytes']+=1024**2
        identity=prepared_factory.validate(changed['factory'])
        from quirkbench.prepared_media import record
        changed[field]=record(identity,'a'*64,changed['device']['device_bytes'],factory_data_end=identity.factory_data_end,version=2)
        changed['observed_layout'][1]['offset']+=1024**2
    elif field=='factory':
        changed['factory']['factory_data_end']-=1
        changed['prepared_media']['factory_data_end']-=1
    else:changed[field]='unrelated-target'
    assert plans.reference(value)!=plans.reference(changed)


@pytest.mark.parametrize('mutation',[lambda v:v['device'].update(diskseq=True),
    lambda v:v['source']['authentication'].update(mode='signed'),
    lambda v:v['controller'].update(controller_url='https://127.0.0.1:8443'),
    lambda v:v['prepared_media'].update(completion='COMPLETED'),
    lambda v:v.update(unrecognized=True)])
def test_malformed_or_already_completed_plan_is_not_an_apply_grant(selected,mutation):
    value,fd=selected;changed=deepcopy(value);mutation(changed)
    with pytest.raises((CommissionError,ValueError)):
        plans.require_apply(changed,'a'*64,True)


def test_same_source_different_derived_factory_requires_new_plan(selected):
    value,fd=selected
    factory=deepcopy(value['factory']);factory['factory_data_end']-=1
    with pytest.raises(CommissionError,match='fresh plan'):
        plans.revalidate(value,fd,device_observer=lambda:value['device'],
            source_reader=lambda:{'source':value['source'],'factory':factory},
            controller_reader=lambda:value['controller'])
