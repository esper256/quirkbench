"""Explicitly consented frozen diagnostics over normal paired HTTPS only."""
import base64
import json
import time
from .contracts import canonical,Conflict,digest
from .recovery_report_records import validate_manifest,report_id
from .transport import TransportError,MAX_BODY,MAX_CHUNK,_strict_json


def send(manifest,files,*,control=None,verify_target=None,binding_reader=None,progress=None,timeout=45):
    from .runtime import CONTROL,boot_context
    from .binding import read_system_uuid
    from .recovery_status import paired_session
    from .release_http import _response,_length,_remaining
    manifest=json.loads(canonical(validate_manifest(manifest)));files=dict(files);sha=report_id(manifest)
    if set(files)!=set(manifest['files']) or any(not isinstance(v,bytes) or digest(v)!=manifest['files'][n]['sha256'] or len(v)!=manifest['files'][n]['size'] for n,v in files.items()):
        raise Conflict('frozen report attachment changed; collect a new report')
    if type(timeout) not in (int,float) or not 0<timeout<=45:raise ValueError('report transfer deadline must be 0 to 45 seconds')
    if verify_target is None:
        _,boot,verify_target=boot_context()
        if boot.get('quirkbench.mode')!='recovery':raise Conflict('normal recovery pairing required')
    deadline=time.monotonic()+timeout;progress=progress or (lambda _:None)
    runtime,context,token,exact=paired_session(control or CONTROL,verify_target,binding_reader or read_system_uuid,deadline=deadline,operation='recovery report upload')
    def request(action,payload):
        exact();raw=canonical({'schema_version':1,**payload})
        headers={'Content-Type':'application/json','X-Device-ID':runtime['device_id'],'Authorization':'Bearer '+token}
        try:
            with _response(runtime['controller_url'].rstrip('/')+'/v1/recovery-reports/'+action,deadline,time.monotonic,
                method='POST',body=raw,headers=headers,context=context,expected_status=None,operation='recovery report upload',before_request=exact) as response:
                if response.status!=200:raise TransportError('recovery report upload HTTP '+str(response.status))
                size=_length(response,MAX_BODY);reply=bytearray()
                while len(reply)<size:
                    _remaining(deadline,time.monotonic,operation='recovery report upload')
                    chunk=response.read1(min(65536,size-len(reply)))
                    if not chunk:raise TransportError('incomplete recovery report reply')
                    reply.extend(chunk)
                exact();envelope=_strict_json(bytes(reply))
                if set(envelope)!={'schema_version','data'} or envelope['schema_version']!=1 or set(envelope['data'])!={'value'}:raise TransportError('invalid report reply envelope')
                return envelope['data']['value']
        except (OSError,ValueError) as exc:raise TransportError('bounded recovery report connection failed') from exc
    answer=request('begin',{'manifest':manifest})
    if not isinstance(answer,dict) or set(answer)!={'report_id','receipt'} or answer['report_id']!=sha:raise TransportError('report begin identity differs')
    receipt=answer['receipt']
    if receipt is None:
        for name,raw in files.items():
            offset=0
            while offset<len(raw) or len(raw)==0 and offset==0:
                progress('Sending '+name+f': {offset}/{len(raw)} bytes')
                chunk=raw[offset:offset+MAX_CHUNK]
                answer=request('chunk',{'request_id':manifest['request_id'],'file':name,'offset':offset,'data_b64':base64.b64encode(chunk).decode()})
                next_offset=answer.get('offset') if isinstance(answer,dict) else None
                if type(next_offset) is not int or not offset+len(chunk)<=next_offset<=len(raw):raise TransportError('report upload made no valid progress')
                offset=next_offset
                if offset==len(raw):break
        receipt=request('finish',{'request_id':manifest['request_id']})
    expected={'schema_version':1,'report_id':sha,'request_id':manifest['request_id'],'device_id':runtime['device_id'],'reported_diagnostics':True}
    if (not isinstance(receipt,dict) or set(receipt)!=set(expected)|{'received_at'} or any(receipt[k]!=v for k,v in expected.items())
            or type(receipt['received_at']) not in (int,float) or receipt['received_at']<=0):raise TransportError('durable report receipt differs')
    exact();return receipt
