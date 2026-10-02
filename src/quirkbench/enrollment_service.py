"""Bounded anonymous application for the existing HTTPS/controller owner."""
import subprocess
import time
from pathlib import Path

from .contracts import Conflict,ContractError
from .enrollment_credentials import publication,complete_bound
from .enrollment_proof import challenge,reserve_redemption
from .transport import _body


class EnrollmentService:
    def __init__(self,controller, *, clock=time.time,run=subprocess.run,tls_inspector=None):
        self.controller=controller;self.clock=clock;self.run=run;self.tls_inspector=tls_inspector
        self.available=lambda:None
        self.guard=None

    def preflight(self,certfile,keyfile):
        from .controller_service import configuration
        config=configuration(self.controller.root)
        if config.get('credential_registry') is not True or config['cert']!=str(Path(certfile)) or config['key']!=str(Path(keyfile)):
            raise Conflict('enrollment requires the configured registry-mode controller TLS identity')
        publication(self.controller,run=self.run,tls_inspector=self.tls_inspector)

    def handle(self,path,data,peer):
        self.available()
        kwargs={'clock':self.clock,'run':self.run,'tls_inspector':self.tls_inspector,'guard':self.guard}
        if path=='/v1/enrollment/challenge':
            _body(data,{'request'})
            result=challenge(self.controller,data['request'],peer,**kwargs)
            self.available();return result
        if path=='/v1/enrollment/redeem':
            _body(data,{'request','challenge_id','code','signature'})
            # Every reply, including COMPLETE recovery, requires a fresh one-use
            # proof of the same retained request/key before private reply lookup.
            reserve_redemption(self.controller,data['request'],data['challenge_id'],data['code'],data['signature'],**kwargs)
            result=complete_bound(self.controller,data['request'],**kwargs)
            self.available();return result
        raise ContractError('unknown enrollment route')
