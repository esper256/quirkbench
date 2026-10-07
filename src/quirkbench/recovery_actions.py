"""Explicit recovery action adapters; existing services retain all authority."""
from pathlib import Path
import subprocess

from .contracts import Conflict


class RecoveryActions:
    def __init__(self, *, control=None, context=None, run=subprocess.run, profiles_ready=None, report_root=None, report_ready=None, report_sources=None):
        from .runtime import CONTROL, boot_context
        from .console import network_profiles_ready
        self.control=Path(control or CONTROL);self.context=context or boot_context
        self.run=run;self.profiles_ready=profiles_ready or network_profiles_ready
        self.report_root=report_root;self.report_ready=report_ready;self.report_sources=report_sources

    def native(self,argv,timeout=45):
        result=self.run(argv,check=False,capture_output=True,text=True,timeout=timeout)
        if result.returncode:raise Conflict('native recovery action failed; inspect its service log')
        return result.stdout

    def network(self):
        if not self.profiles_ready():raise Conflict('private RAM profile storage unavailable')
        if self.native(['/usr/bin/systemctl','is-active','NetworkManager.service'],2).strip()!='active':
            raise Conflict('NetworkManager unavailable')
        return self.run(['/usr/bin/nmtui'],check=False)

    def terminal(self):
        from .local_terminal import switch_vt
        switch_vt(3)

    def dispatch(self,name,stream):
        if name=='details':
            from .recovery_status import read_status
            facts=read_status(control=self.control)
            for label,value in (('Computer',facts.target or 'not paired'),('Hardware UUID',facts.system_uuid or 'unavailable'),
                    ('Controller endpoint',facts.endpoint or ('not checked' if facts.controller=='not-checked' else 'unavailable')),
                    ('Prepared public trust SHA256',facts.prepared_fingerprint or 'unavailable'),
                    ('Boot checks',facts.boot),('USB',facts.usb),('Binding',facts.binding),
                    ('Authenticated contact',facts.controller),('Current recorded activity',facts.activity or 'none')):
                stream.write(label+': '+value+'\n')
            stream.write('Controller details: '+facts.controller_detail+'\n')
            stream.write('Connected/pairing never grants a target run. Temporary connections need explicit remembering.\n')
            return {'read_only':True}
        if name=='logs':
            from .ostree import CommandRunner
            command=CommandRunner(lambda *a:None,lambda:None,timeout_s=5,operation='recovery log view')
            stream.write(command(['/usr/bin/journalctl','-b','--no-pager','-n','300']))
            return {'read_only':True}
        if name=='retry_checks':
            from .shutdown_local import _service
            _service(self.run,self_owned=False)
            stream.write('Retrying existing recovery checks; no partition or format action.\n')
            self.native(['/usr/bin/systemctl','restart','quirkbench-recovery.service'],45)
            stream.write('Recovery service returned. The dashboard will re-read current checks.\n')
            return {'retry_requested':True}
        if name=='reset_network':
            from .network_profiles import PROFILES
            from .filesystem import nested_mounts
            from .process_identity import verify_empty_cgroup
            stream.write('Forget all temporary connections and uncertain RAM copies. Saved USB selections remain intact. Type RESET CONNECTIONS to continue: ')
            if stream.readline().strip()!='RESET CONNECTIONS':return {'cancelled':True}
            props=self.native(['/usr/bin/systemctl','show','NetworkManager.service','--property=ActiveState,MainPID,ControlGroup,KillMode,Job'],2)
            before=dict(line.split('=',1) for line in props.splitlines() if '=' in line)
            if before.get('KillMode')!='control-group':raise Conflict('NetworkManager whole-worker shutdown unavailable')
            group=before.get('ControlGroup','')
            if group and not group.endswith('/NetworkManager.service'):raise Conflict('NetworkManager ownership differs')
            self.native(['/usr/bin/systemctl','stop','NetworkManager.service'])
            props=self.native(['/usr/bin/systemctl','show','NetworkManager.service','--property=ActiveState,MainPID,ControlGroup,KillMode,Job'],2)
            after=dict(line.split('=',1) for line in props.splitlines() if '=' in line)
            if after.get('ActiveState') not in ('inactive','failed') or after.get('MainPID')!='0' or after.get('Job') not in ('','0'):
                raise Conflict('NetworkManager shutdown remains uncertain; RAM profiles retained')
            verify_empty_cgroup(Path('/sys/fs/cgroup'),group)
            verify_empty_cgroup(Path('/sys/fs/cgroup'),after.get('ControlGroup',''))
            self.native(['/usr/bin/systemctl','stop','quirkbench-network-state.service'])
            if nested_mounts(PROFILES):raise Conflict('old RAM mount remains; leave networking stopped and inspect logs')
            self.native(['/usr/bin/systemctl','start','quirkbench-network-state.service'])
            if not self.profiles_ready():raise Conflict('new private RAM profiles unavailable; networking remains stopped')
            self.native(['/usr/bin/systemctl','start','NetworkManager.service'])
            return {'temporary_connections_reset':True}
        if name in ('retry_network','replay_network'):
            if name=='replay_network':
                _,_,verify=self.context();verify()
                from .retarget_local import require_runtime_available
                from .endpoint_local import require_available
                require_runtime_available(self.control);require_available(self.control)
            stream.write('Restarting networking disconnects temporary connections. Saved secrets still require current media and binding. Type RESTART to continue: ')
            if stream.readline().strip()!='RESTART':return {'cancelled':True}
            self.native(['/usr/bin/systemctl','start','quirkbench-network-state.service'])
            if not self.profiles_ready():raise Conflict('private RAM profile storage remains unavailable')
            self.native(['/usr/bin/systemctl','restart','NetworkManager.service'])
            stream.write('NetworkManager restarted. Review the observed connection state.\n')
            return {'restart_requested':True}
        if name=='local_power':
            stream.write('Local OS power action: evidence preservation could not be confirmed. Existing fences and records are retained.\nType LOCAL RESTART or LOCAL SHUTDOWN to request it (empty cancels): ')
            answer=stream.readline().strip()
            selected={'LOCAL RESTART':'reboot','LOCAL SHUTDOWN':'poweroff'}.get(answer)
            if selected is None:return {'cancelled':True}
            self.native(['/usr/bin/systemctl',selected])
            stream.write('Local OS '+selected+' requested; preservation and physical completion remain unconfirmed.\n')
            return {'local_os_requested':selected,'evidence_preservation_confirmed':False}
        if name in ('collect','export','upload'):
            from .recovery_reports import attended, ROOT
            options={}
            if name=='upload':
                _,report_boot,report_verify=self.context()
                if report_boot.get('quirkbench.mode')!='recovery':raise Conflict('recovery report upload requires current recovery')
                options={'control':self.control,'verify_target':report_verify}
            if name=='collect' and self.report_sources is not None:options['source_root']=self.report_sources
            return attended(name,input_stream=stream,output_stream=stream,root=self.report_root or ROOT,ready=self.report_ready,**options)
        config,boot,verify=self.context()
        if boot.get('quirkbench.mode')!='recovery':raise Conflict('attended action requires current recovery')
        verify()
        if name=='setup_file':
            from .retarget_local import require_runtime_available
            from .endpoint_local import require_available
            from .enrollment_target import _storage
            _, storage_verify = _storage(self.control,verify)
            storage_verify();require_runtime_available(self.control);require_available(self.control)
            stream.write('Apply the private setup bundle staged at '+str(self.control/'setup')+'. This stops the existing target supervisor and does not approve experiments. Type APPLY SETUP (empty cancels): ')
            if stream.readline().strip()!='APPLY SETUP':return {'cancelled':True}
            from .shutdown_local import _stop, _service, UNIT
            from .provisioning import activate_bundle
            _service(self.run,self_owned=False,before_stop=True)
            def current():
                storage_verify();require_runtime_available(self.control);require_available(self.control)
            try:
                _stop(self.run,Path('/sys/fs/cgroup'))
                current()
                result=activate_bundle(self.control/'setup',self.control,verify_target=current)
                stream.write('Validated controller setup activated. Exact-run approval is still required.\n')
                return result
            finally:
                # A stop client interruption can still have submitted its manager job.
                self.native(['/usr/bin/systemctl','start',UNIT],120)
        if name=='pair':
            from .prepared_enrollment import connect
            capacity=boot.get('quirkbench.capacity',{})
            if capacity.get('record_type')!='prepared-capacity':raise Conflict('Prepare compatible media on the controller before initial pairing')
            stream.write('Connecting using the controller trust prepared on this USB. A timeout retains the same request and key.\n')
            answer=connect(self.control,verify_target=verify,
                           prepared_media_sha256=capacity['prepared_media_sha256'],run=self.run)
            stream.write('Initial pairing activated. Each target run still requires its own approval.\n')
            return answer
        if name=='save_network':
            from .network_profiles import save_attended_network
            return save_attended_network(control=self.control,verify_target=verify,input_stream=stream,output_stream=stream)
        if name=='drain':
            from .evidence_drain_target import attended_drain
            return attended_drain(control=self.control,input_stream=stream,output_stream=stream)
        if name=='retarget':
            from .retarget_console import connect_retarget
            return connect_retarget(control=self.control,config=config,input_stream=stream,output_stream=stream,run=self.run,verify_target=verify)
        if name=='endpoint':
            from .endpoint_console import connect_endpoint
            return connect_endpoint(control=self.control,config=config,input_stream=stream,output_stream=stream,run=self.run,verify_target=verify)
        if name in ('shutdown','restart'):
            from .shutdown_local import attended
            return attended(control=self.control,config=config,verify_target=verify,input_stream=stream,output_stream=stream,
                            run=self.run,power_action='reboot' if name=='restart' else 'poweroff')
        raise ValueError('unknown recovery action')
