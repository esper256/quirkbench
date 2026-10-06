"""Operator invitations, secret-free status and legacy client compatibility."""
import json
from pathlib import Path

import pytest

from quirkbench import cli,target_setup
from quirkbench.contracts import CapabilityReport,Conflict,ContractError,canonical
from quirkbench.enrollment import create_code
from quirkbench.credential_registry import revoke_generation
from test_enrollment_credentials import publication,complete
from test_enrollment_certificate import bound
from test_enrollment_proof import request,signed_nonce,args
from test_enrollment import issuer
from test_setup_service import initialized


def test_parser_preserves_flags_only_client_and_rejects_mixed_syntax():
    old=['target','--url','https://host:8443','--ca','ca.pem','--token-file','token','--report','report','--once']
    with pytest.raises(SystemExit):cli.parser().parse_args(old)
    for argv in (['target'],['target', 'pair'],['target','show','lab','--request-id','new'],
                 old+['add','lab'],['target', 'pair', 'lab', '--once']):
        with pytest.raises(SystemExit):cli.parser().parse_args(argv)
    assert cli.parser().parse_args(['target', 'pair', 'lab', '--request-id', 'first', '--json']).json
    assert cli.parser().parse_args(['target','show','lab','--json']).action=='show'


@pytest.mark.parametrize('version',[2,3])
def test_frozen_additive_target_cli_and_status_schema(version):
    from jsonschema import Draft202012Validator
    root=Path(__file__).resolve().parents[1]
    fixture=json.loads((root/f'examples/target-cli.v{version}.json').read_bytes())
    assert fixture['schema_version']==version
    for case in fixture['cases']:
        parsed=cli.parser().parse_args(case['argv'])
        assert all(getattr(parsed,key)==value for key,value in case['expected'].items())
    schema=json.loads((root/'schemas/target-status.v1.schema.json').read_bytes())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(json.loads((root/'examples/target-status.json').read_bytes()))


@pytest.mark.parametrize('boundary',['secret_retained','code_committed'])
def test_human_add_ack_loss_uses_one_durable_name_intent(issuer,boundary):
    c,kwargs=issuer
    def fail(stage):
        if stage==boundary:raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):target_setup.add_target(c.root,'lab',ttl_seconds=60,fault_hook=fail,**kwargs)
    first=target_setup.add_target(c.root,'lab',**kwargs)
    assert target_setup.add_target(c.root,'lab',**kwargs)==first
    with c.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM enrollment_codes').fetchone()[0]==1
        for table in ('devices','campaigns','jobs','attempts','credential_generations'):
            assert db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0
    with pytest.raises(Conflict):target_setup.add_target(c.root,'lab',ttl_seconds=120,**kwargs)


def test_expired_human_intent_requires_explicit_new_request_and_retains_ttl(issuer):
    c,kwargs=issuer;first=target_setup.add_target(c.root,'lab',ttl_seconds=60,**kwargs)
    assert target_setup.add_target(c.root,'lab',**kwargs)==first
    late=kwargs|{'clock':lambda:1060}
    with pytest.raises(Conflict):target_setup.add_target(c.root,'lab',**late)
    second=target_setup.add_target(c.root,'lab','explicit-new',**late)
    assert second['code']!=first['code']
    status=target_setup.show_target(c.root,'lab',clock=late['clock'])
    assert status['enrollment']['state']=='ACTIVE' and not status['enrollment']['credentials_live']


def test_show_complete_without_report_does_not_infer_contact_or_boot_authority(publication):
    c,req,code,kwargs=publication;result=complete(c,req,kwargs)
    before=(c.root/'controller.sqlite').read_bytes()
    answer=target_setup.show_target(c.root,'target')
    from jsonschema import Draft202012Validator
    schema=json.loads((Path(__file__).resolve().parents[1]/'schemas/target-status.v1.schema.json').read_bytes())
    Draft202012Validator(schema).validate(answer)
    by_id=target_setup.show_target(c.root,result['device_id'])
    assert answer|{'target':result['device_id']}==by_id
    assert answer['enrollment']['state']=='COMPLETE' and answer['enrollment']['credentials_live']
    assert answer['recovery']['report_available'] is False and answer['recovery']['contact_current'] is None
    assert answer['unattended_eligible'] is None and not answer['execution_authorized']
    assert not answer['candidate_preparation']['recorded_inputs_ready']
    assert result['device_token'] not in json.dumps(answer) and 'certificate_pem' not in json.dumps(answer)
    assert (c.root/'controller.sqlite').read_bytes()==before
    revoke_generation(c,result['credential_generation']['generation'])
    assert target_setup.show_target(c.root,'target')['enrollment']['state']=='REVOKED'


def test_show_recorded_wrong_media_blocks_binding_and_missing_inventory(publication):
    c,req,code,kwargs=publication;result=complete(c,req,kwargs)
    report=CapabilityReport(result['device_id'],'real-boot',[],inventory={
        'target_binding':req['target_binding'],'media_instance_id':'different-media'})
    c.register(report)
    answer=target_setup.show_target(c.root,'target')
    assert answer['recovery']['reported_mode']=='recovery' and answer['recovery']['binding_matches'] is False
    assert answer['recovery']['contact_current'] is None
    assert not answer['candidate_preparation']['recorded_inputs_ready']
    assert 'recovery_inventory_unavailable' in answer['candidate_preparation']['blocking_reasons']
    assert 'enrollment_binding_mismatch' in answer['candidate_preparation']['blocking_reasons']


@pytest.mark.parametrize('lookup',['name','id'])
@pytest.mark.parametrize('change',['media','state','redeemed_key'])
def test_show_validates_same_durable_links_for_name_and_assigned_id(publication,lookup,change):
    c,req,code,kwargs=publication;result=complete(c,req,kwargs)
    with c.transaction() as db:
        if change=='media':db.execute("UPDATE credential_generations SET media_instance_id='other-media'")
        elif change=='state':db.execute("UPDATE enrollment_requests SET state='BOUND'")
        else:db.execute("UPDATE enrollment_codes SET redeemed_key_sha256=?",('f'*64,))
    with pytest.raises(Conflict):target_setup.show_target(c.root,'target' if lookup=='name' else result['device_id'])


def test_assigned_target_id_takes_precedence_over_coincident_invitation(publication):
    c,req,code,kwargs=publication;result=complete(c,req,kwargs)
    create_code(c,result['device_id'],'coincident-name',**kwargs)
    answer=target_setup.show_target(c.root,result['device_id'])
    assert answer['enrollment']['state']=='COMPLETE' and answer['device_id']==result['device_id']


def test_wrong_media_full_inventory_cannot_be_ready_even_with_supported_inputs(publication,tmp_path,monkeypatch):
    from test_hardware_plan import inventory
    from quirkbench.state_reader import StateReader
    c,req,code,kwargs=publication;result=complete(c,req,kwargs)
    hardware=inventory(tmp_path)
    c.register(CapabilityReport(result['device_id'],'reported-boot',[],inventory={
        'target_binding':req['target_binding'],'media_instance_id':'other-media',
        'architecture':hardware['platform']['architecture'],'hardware_inventory':hardware}))
    def ready_inventory(reader,device_id):
        with reader.transaction() as db:
            assert db.in_transaction
            assert db.execute('PRAGMA query_only').fetchone()[0]==1
        return {'ready_for_candidate_preparation':True,'blocking_reasons':[]}
    monkeypatch.setattr(StateReader,'target_inventory',ready_inventory)
    answer=target_setup.show_target(c.root,result['device_id'])
    assert not answer['candidate_preparation']['recorded_inputs_ready']
    assert answer['candidate_preparation']['blocking_reasons']==['enrollment_binding_mismatch']


def test_unknown_show_and_unavailable_add_do_not_create_state(tmp_path):
    missing=tmp_path/'no-state'
    with pytest.raises((ContractError,OSError)):target_setup.show_target(missing,'lab')
    with pytest.raises(ValueError):target_setup.add_target(missing,'lab')
    assert not missing.exists()


def test_cli_mutation_requires_id_and_show_has_stable_readonly_envelope(publication,monkeypatch,capsys):
    c,req,code,kwargs=publication;complete(c,req,kwargs)
    before=(c.root/'controller.sqlite').read_bytes()
    monkeypatch.setattr(cli,'Controller',lambda *a,**kw:(_ for _ in ()).throw(AssertionError('readonly status initialized a writer')))
    assert cli.main(['target','show','target','--json'], state_root=str(c.root))==0
    answer=json.loads(capsys.readouterr().out)
    assert answer['ok'] and answer['data']['enrollment']['state']=='COMPLETE'
    assert (c.root/'controller.sqlite').read_bytes()==before
    assert cli.main(['target','show','unknown','--json'], state_root=str(c.root))==2
    assert json.loads(capsys.readouterr().out)['error']['code']=='INVALID_INPUT'
