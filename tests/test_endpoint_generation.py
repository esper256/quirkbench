"""Provenance cannot grant authority to credential, binding or trust edits."""
import json
from pathlib import Path
import pytest
from quirkbench import endpoint_generation as endpoint
from quirkbench.contracts import Conflict,ContractError,canonical,digest
from quirkbench.retarget_activation import _active
from quirkbench.store import atomic_write

@pytest.fixture
def source():
    runtime={'schema_version':1,'device_id':'target-a','controller_url':'https://192.0.2.10:8443','ca':'ca.pem','token_file':'device.token',
        'target_binding':{'schema_version':1,'system_uuid':'12345678-1234-1234-1234-123456789abc'},
        'remotes':{'lab':{'url':'https://192.0.2.10:8444/lab','ca':'ca.pem','public_key':'lab.public.asc','client_cert':'repository.crt','client_key':'repository.key'}}}
    return {'runtime.json':canonical(runtime),'ca.pem':b'CA test bytes','device.token':b'test-token-only',
        'repository.crt':b'client test bytes','repository.key':b'private test bytes','lab.public.asc':b'public test bytes'}

def move(files,request='move-1',host='192.0.2.11'):
    return endpoint.transition(files,request,'a'*64,'b'*64,'https://'+host+':8443',{'lab':'https://'+host+':8444/lab'},'c'*64)

def test_url_delta_preserves_all_original_bytes(source):
    before=dict(source);record,files=move(source)
    assert source==before and all(files[name]==raw for name,raw in source.items() if name!='runtime.json')
    runtime=json.loads(files['runtime.json']);original=json.loads(source['runtime.json'])
    assert runtime==original|{'controller_url':'https://192.0.2.11:8443','remotes':{'lab':original['remotes']['lab']|{'url':'https://192.0.2.11:8444/lab'}}}
    assert endpoint.verify_transition(record,source,files)==_active(files,record['destination_generation'])

@pytest.mark.parametrize('name',['ca.pem','device.token','repository.crt','repository.key','lab.public.asc'])
def test_credential_edits_fail_even_with_recomputed_hashes(source,name):
    record,files=move(source);files[name]=b'changed';record=record|{'destination_generation':endpoint._bundle(files)[1]}
    record['destination_runtime_sha256']=digest(_active(files,record['destination_generation']))
    with pytest.raises(Conflict):endpoint.verify_transition(record,source,files)

@pytest.mark.parametrize('field,value',[('device_id','target-b'),('target_binding',{'schema_version':1,'system_uuid':'22345678-1234-1234-1234-123456789abc'}),('qualification_run','q'),('recovery_profile',{}),('token_file','old.token')])
def test_scope_edits_never_gain_authority(source,field,value):
    record,files=move(source);runtime=json.loads(files['runtime.json']);runtime[field]=value;files['runtime.json']=canonical(runtime)
    with pytest.raises((Conflict,ContractError)):endpoint.verify_transition(record,source,files)

@pytest.mark.parametrize('url',['http://192.0.2.11:8444/lab','https://192.0.2.12:8444/lab','https://192.0.2.11:8443/lab',
    'https://192.0.2.11:8444/another','https://192.0.2.11:8444/../lab','https://192.0.2.11:8444/lab?x=1',
    'https://192.0.2.11:8444/%6cab','https://192.0.2.11:8444/lab/',
    'https://192.0.2.11:8444/la\nb','https://192.0.2.11:8444/la\tb','https://192.0.2.11:8444/la\rb'])
def test_repository_identity_and_origin_cannot_change(source,url):
    with pytest.raises((Conflict,ContractError)):endpoint.transition(source,'move','a'*64,'b'*64,'https://192.0.2.11:8443',{'lab':url},'c'*64)

@pytest.mark.parametrize('urls',[{}, {'other':'https://192.0.2.11:8444/lab'}, {'lab':'https://192.0.2.11:8444/lab','extra':'https://192.0.2.11:8444/extra'}])
def test_aliases_require_exact_explicit_urls(source,urls):
    with pytest.raises(Conflict):endpoint.transition(source,'move','a'*64,'b'*64,'https://192.0.2.11:8443',urls,'c'*64)

def test_exact_bounded_chain_preserves_original_enrollment(source):
    first,a=move(source);second,b=move(a,'move-2','192.0.2.12')
    generations={first['source_generation']:source,first['destination_generation']:a,second['destination_generation']:b}
    original=_active(source,first['source_generation'])
    assert endpoint.verify_chain([first,second],generations,original,'a'*64,'b'*64)==_active(b,second['destination_generation'])
    for records,mapping,request_sha in ([second,first],generations,'a'*64),([first,second],generations,'d'*64),([first,second|{'request_id':first['request_id']}],generations,'a'*64),([first,second],generations|{'e'*64:source},'a'*64),([first,second],{first['source_generation']:source},'a'*64):
        with pytest.raises(Conflict):endpoint.verify_chain(records,mapping,original,request_sha,'b'*64)
    third,c=move(b,'move-3','192.0.2.10')
    with pytest.raises(Conflict):endpoint.verify_chain([first,second,third],generations,original,'a'*64,'b'*64)
    with pytest.raises(ContractError):endpoint.verify_chain([first]*33,generations,original,'a'*64,'b'*64)

@pytest.mark.parametrize('change',['bytes','extra','missing','symlink','hardlink','permissions','manifest'])
def test_private_reader_uses_fixed_bounded_owned_namespace(tmp_path,source,change):
    _,generation,manifest=endpoint._bundle(source);control=tmp_path/'control';directory=control/'generations'/generation
    directory.mkdir(parents=True,mode=0o700);control.chmod(0o700);directory.parent.chmod(0o700)
    for name,raw in source.items():atomic_write(directory/name,raw)
    atomic_write(directory/'generation.json',canonical(manifest));assert endpoint.read_generation(control,generation)==source
    path=directory/'ca.pem'
    if change=='bytes':atomic_write(path,b'changed')
    elif change=='extra':atomic_write(directory/'extra.pem',b'unknown')
    elif change=='missing':path.unlink()
    elif change=='symlink':path.unlink();path.symlink_to(directory/'device.token')
    elif change=='hardlink':path.rename(directory/'retained.pem');path.hardlink_to(directory/'retained.pem')
    elif change=='permissions':path.chmod(0o644)
    else:atomic_write(directory/'generation.json',canonical(manifest|{'ca.pem':'f'*64}))
    with pytest.raises((Conflict,ContractError,OSError)):endpoint.read_generation(control,generation)

def test_schema_and_strict_reader(source):
    from jsonschema import Draft202012Validator
    record,_=move(source);root=Path(__file__).resolve().parents[1]
    validator=Draft202012Validator(json.loads((root/'schemas/target-endpoint-transition.v1.schema.json').read_bytes()))
    validator.validate(record);validator.validate(json.loads((root/'examples/target-endpoint-transition.json').read_bytes()))
    with pytest.raises(ContractError):endpoint.validate_transition(record|{'approval':True})
