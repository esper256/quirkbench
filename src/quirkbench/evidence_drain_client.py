"""Two-method TLS client for an explicitly provisioned private drain credential."""
import base64
import re
import ssl
import time

from .contracts import Conflict,ContractError
from .evidence_drain import validate_grant
from .transport import MAX_CHUNK,MAX_BODY,TransportError,_strict_json
from .enrollment_client import endpoint
from .release_http import _response,_length,_remaining
from .http_bounds import BoundedHTTPError


class HTTPSDrainClient:
    def __init__(self,base_url,credential,cafile, *,timeout=15):
        if (not isinstance(credential,dict) or set(credential)!={'record','token'}
                or not isinstance(credential['token'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',credential['token'])):
            raise ContractError('explicit private drain credential required')
        self.record=validate_grant(credential['record']);self.plan=self.record['plan']
        self.device_id=self.plan['device_id']
        endpoint(base_url)
        if type(timeout) not in (int,float) or not 0<timeout<=45:raise ContractError('drain request deadline must be 0 to 45 seconds')
        self.base_url=base_url;self._token=credential['token'];self.timeout=timeout
        self._monotonic=time.monotonic;self._absolute_deadline=None
        self.context=ssl.create_default_context(cafile=cafile);self.context.hostname_checks_common_name=False

    def _entry(self,attempt,sha,size, *,boot=None,upload=None,stream=None,sequence=None):
        if attempt!=self.plan['attempt_id'] or (boot is not None and boot!=self.plan['boot_id']):raise ContractError('drain original attempt/boot differs')
        for entry in self.plan['evidence']:
            if ((sha,size)==(entry['sha256'],entry['size'])
                    and (upload is None or upload==attempt+'.'+str(entry['sequence']))
                    and (stream is None or (stream,sequence)==(entry['stream'],entry['sequence']))):return
        raise ContractError('evidence differs from explicit approved drain scope')

    def _request(self,action,payload):
        from .contracts import canonical
        from .product_contracts import _depth
        if action not in ('upload','evidence'):raise ContractError('drain client permits only upload/evidence')
        deadline=self._monotonic()+self.timeout
        if self._absolute_deadline is not None:deadline=min(deadline,self._absolute_deadline)
        _remaining(deadline,self._monotonic,operation='evidence drain request')
        raw=canonical({'schema_version':1,**payload});_remaining(deadline,self._monotonic,operation='evidence drain request')
        headers={'X-Evidence-Drain-ID':self.record['grant_id'],'X-Device-ID':self.device_id,
            'Authorization':'Bearer '+self._token,'Content-Type':'application/json'}
        try:
            with _response(self.base_url+'/v1/evidence-drain/'+action,deadline,self._monotonic,
                    method='POST',body=raw,headers=headers,context=self.context,expected_status=None,operation='evidence drain request') as response:
                if response.status!=200:raise TransportError('HTTP '+str(response.status))
                size=_length(response,MAX_BODY);answer=bytearray()
                while len(answer)<size:
                    _remaining(deadline,self._monotonic,operation='evidence drain request');chunk=response.read1(min(65536,size-len(answer)))
                    _remaining(deadline,self._monotonic,operation='evidence drain request')
                    if not chunk:raise TransportError('incomplete drain response')
                    answer.extend(chunk)
                value=_strict_json(bytes(answer));_depth(value)
        except (TransportError,Conflict):raise
        except BoundedHTTPError as exc:raise TransportError(str(exc)) from exc
        except (OSError,ValueError,RecursionError) as exc:raise TransportError('bounded drain connection/response failed') from exc
        if type(value.get('schema_version')) is not int or value['schema_version']!=1 or not isinstance(value.get('data'),dict):
            raise TransportError('invalid drain response envelope')
        return value['data'].get('value')

    def upload(self,attempt_id,token,boot_id,upload_id,offset,data,expected_digest,total_size):
        self._entry(attempt_id,expected_digest,total_size,boot=boot_id,upload=upload_id)
        if not isinstance(data,bytes) or len(data)>MAX_CHUNK:raise ContractError('drain upload chunk exceeds bound')
        return self._request('upload',{'attempt_id':attempt_id,'token':token,'boot_id':boot_id,'upload_id':upload_id,
            'offset':offset,'data_b64':base64.b64encode(data).decode(),'expected_digest':expected_digest,'total_size':total_size})

    def evidence(self,attempt_id,token,stream,sequence,sha256,size):
        self._entry(attempt_id,sha256,size,stream=stream,sequence=sequence)
        return self._request('evidence',{'attempt_id':attempt_id,'token':token,'stream':stream,'sequence':sequence,'sha256':sha256,'size':size})
