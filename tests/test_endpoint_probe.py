"""Original enrollment trust and read-only reachability precede any endpoint activation."""
from contextlib import contextmanager
from email.message import Message
import json
from pathlib import Path
import pytest

from quirkbench import endpoint_probe as probe,endpoint_generation as generation
from quirkbench.contracts import Conflict,ContractError,canonical,digest
from quirkbench.enrollment_activation import _bundle
from quirkbench.transport import TransportError
from test_enrollment_activation import received
from test_enrollment_credentials import publication,complete,Commands
from test_enrollment_certificate import bound
from test_enrollment_proof import args
from test_enrollment import issuer
from test_setup_service import initialized

class Reply:
    def __init__(self,body,status=200,kind='application/json'):
        self.body=body;self.status=status;self.headers=Message();self.headers['Content-Length']=str(len(body));self.headers['Content-Type']=kind
    def getheader(self,name,default=None):return self.headers.get(name,default)
    def read1(self,size):value=self.body[:size];self.body=self.body[size:];return value

@pytest.fixture
def prepared(received):
    control,result,pem,local=received;pending=control/'enrollment/pending'
    request=json.loads((pending/'request.json').read_bytes());key=(pending/'key.pem').read_bytes();files=_bundle(result,request,key)
    pin=digest(__import__('ssl').PEM_cert_to_DER_cert(pem))
    record,destination=generation.transition(files,'endpoint-1',digest(canonical(request)),digest(canonical(result)),
        'https://127.0.0.1:8445',{alias:'https://127.0.0.1:8446'+__import__('urllib.parse',fromlist=['urlsplit']).urlsplit(remote['url']).path for alias,remote in result['repository_remotes'].items()},pin)
    calls=[]
    @contextmanager
    def response(url,deadline,monotonic,**kw):
        assert kw['expected_peer_sha256']==pin and kw['context'].check_hostname and not kw['context'].hostname_checks_common_name
        calls.append((url,kw))
        if url.endswith('/endpoint-check'):
            assert kw['headers']['Authorization']=='Bearer '+result['device_token'] and kw['body']==canonical({'schema_version':1})
            yield Reply(canonical({'schema_version':1,'data':{'value':{'device_id':result['device_id'],'credential_accepted':True,'work_queued':False}}}))
        else:yield Reply(b'[core]\nrepo_version=1\n',kind='application/octet-stream')
    return control,record,files,destination,request,result,pem,local,calls,response

def check(prepared,**kw):
    control,record,files,destination,request,result,pem,local,calls,response=prepared
    return probe.probe(record,files,destination,request,result,pem,temporary_parent=control,
        verify_target=kw.pop('verify_target',local['verify_target']),run=kw.pop('run',local['run']),response=kw.pop('response',response),**kw)

def test_native_original_trust_and_both_endpoint_checks_publish_nothing(prepared):
    control,record,files,destination,request,result,pem,local,calls,response=prepared
    before={p:p.read_bytes() for p in control.rglob('*') if p.is_file()};answer=check(prepared)
    assert answer['native_trust_verified'] and answer['reachability_verified'] and not answer['activated'] and not answer['boot_authorized']
    assert len(calls)==1+len(record['remote_urls']) and set(answer['repository_config_sha256'])==set(record['remote_urls'])
    assert {p:p.read_bytes() for p in control.rglob('*') if p.is_file()}==before
    assert result['controller_url']!='https://127.0.0.1:8445'

@pytest.mark.parametrize('change',['request','result','pin','source-ca','source-key','binding'])
def test_changed_original_evidence_or_trust_never_transmits_request(prepared,change):
    control,record,files,destination,request,result,pem,local,calls,response=prepared
    if change=='request':record['enrollment_request_sha256']='f'*64
    elif change=='result':record['enrollment_result_sha256']='f'*64
    elif change=='pin':record['approved_certificate_sha256']='f'*64
    elif change=='source-ca':files['ca.pem']=b'changed'
    elif change=='source-key':files['repository.key']=b'changed'
    else:
        value=json.loads(files['runtime.json']);value['target_binding']['system_uuid']='22345678-1234-1234-1234-123456789abc';files['runtime.json']=canonical(value)
    with pytest.raises((Conflict,ContractError,OSError)):check(prepared)
    assert calls==[]

@pytest.mark.parametrize('change',['native-record','native-destination','network-result','network-source','final-expiry','deadline'])
def test_native_and_network_callbacks_cannot_relax_final_owned_fences(prepared,change):
    control,record,files,destination,request,result,pem,local,calls,response=prepared
    now=[0];wall=[int(__import__('time').time())];done=[False]
    def native(argv,**kw):
        value=local['run'](argv,**kw)
        if change.startswith('native') and not done[0]:
            if change=='native-record':record['controller_url']='https://127.0.0.1:9445'
            else:destination['device.token']=b'changed'
            done[0]=True
        return value
    @contextmanager
    def changed_response(url,*a,**kw):
        with response(url,*a,**kw) as reply:yield reply
        if not done[0]:
            if change=='network-result':result['device_id']='changed'
            elif change=='network-source':files['repository.key']=b'changed'
            elif change=='final-expiry':wall[0]=result['credential_generation']['expires_at']
            elif change=='deadline':now[0]=121
            done[0]=True
    with pytest.raises(Conflict):check(prepared,run=native,response=changed_response,clock=lambda:wall[0],monotonic=lambda:now[0])
    if change.startswith('native'):assert calls==[]

@pytest.mark.parametrize('route',['protocol','repository'])
def test_denied_endpoint_does_not_report_reachability(prepared,route):
    response=prepared[-1]
    @contextmanager
    def denied(url,*a,**kw):
        with response(url,*a,**kw) as reply:
            if (url.endswith('/endpoint-check'))==(route=='protocol'):reply.status=403
            yield reply
    with pytest.raises(TransportError,match='403'):check(prepared,response=denied)


@pytest.mark.parametrize('field,value',[('schema_version',True),('credential_accepted',1),('work_queued',0)])
def test_malformed_reply_primitive_types_are_not_admission_success(prepared,field,value):
    result=prepared[5]
    @contextmanager
    def malformed(url,*a,**kw):
        body={'schema_version':1,'data':{'value':{'device_id':result['device_id'],'credential_accepted':True,'work_queued':False}}}
        if field=='schema_version':body[field]=value
        else:body['data']['value'][field]=value
        yield Reply(canonical(body))
    with pytest.raises(TransportError,match='differs'):check(prepared,response=malformed)


def test_each_native_operation_has_ownership_and_cumulative_deadline_fences(prepared):
    native=prepared[7]['run'];now=[0];calls=[]
    def elapsed(argv,**kw):
        calls.append((argv,kw));answer=native(argv,**kw);now[0]+=40;return answer
    with pytest.raises(Conflict,match='deadline'):check(prepared,run=elapsed,monotonic=lambda:now[0])
    assert len(calls)==3 and not prepared[8]
    assert all(kw['timeout']<=15 for argv,kw in calls)


def test_real_controller_and_repository_acceptance_with_revocation_and_no_work(prepared,publication):
    import threading
    from quirkbench.transport import make_server
    from quirkbench.repository_http import make_repository_server
    from quirkbench.credential_registry import CredentialRegistry,revoke_generation
    from quirkbench.release_http import _response
    from quirkbench.state_reader import StateReader
    c=publication[0];control,record,files,destination,request,result,pem,local,calls,response=prepared
    config=json.loads((c.root/'private/controller-service.json').read_bytes());tls=Path(config['cert']).parent;registry=CredentialRegistry(c.root)
    protocol=make_server(c,host='127.0.0.1',port=0,certfile=config['cert'],keyfile=config['key'],credential_registry=registry)
    repo=make_repository_server(('127.0.0.1',0),config['repositories'],config['cert'],config['key'],tls/'ca.crt',credential_registry=registry)
    threads=[threading.Thread(target=server.serve_forever,daemon=True) for server in (protocol,repo)]
    for thread in threads:thread.start()
    record,new=generation.transition(files,'real-check',record['enrollment_request_sha256'],record['enrollment_result_sha256'],
        'https://127.0.0.1:'+str(protocol.server_address[1]),
        {'lab':'https://127.0.0.1:'+str(repo.server_address[1])+'/lab'},record['approved_certificate_sha256'])
    actual=(control,record,files,new,request,result,pem,local,calls,response)
    def snapshot():
        with StateReader(c.root).connection() as db:
            return tuple(tuple(tuple(row) for row in db.execute('SELECT * FROM '+table)) for table in ('devices','jobs','attempts','protocol_contacts'))
    before=snapshot()
    try:
        assert check(actual,response=_response)['reachability_verified'] and snapshot()==before
        revoke_generation(c,result['credential_generation']['generation'])
        with pytest.raises((TransportError,ContractError)):check(actual,response=_response)
        assert snapshot()==before
    finally:
        for server in (protocol,repo):server.shutdown();server.server_close()
        for thread in threads:thread.join(2)
