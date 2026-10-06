"""Actual packaged frontend in a PTY, inspected through pyte's visible screen."""
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import time

import pyte
import pytest

from test_boot import CONFIG

ROOT=Path(__file__).resolve().parents[1]
UUID='864fad97-1557-41e0-9f9a-ed27c4f17256'
PROGRAM=r'''
import json,os,runpy,subprocess,sys
from pathlib import Path
from quirkbench import recovery_actions as a,recovery_status as s,local_terminal as t
from quirkbench.boot import RecoveryConfig,validate_capacity
root=Path(sys.argv[1]); release=int(sys.argv[2]); events=int(sys.argv[3]); original=s.read_status
def host():return json.loads((root/'host.json').read_bytes())
def command(argv):
    h=host()
    if 'quirkbench-recovery.service' in argv:return 'ActiveState='+h.get('boot_service','active')+'\nSubState=exited\nResult=success\n'
    if argv[1]=='is-active':return h.get('network_service','active')
    if 'device' in argv:return 'wifi:'+h['network']
    return 'enabled:enabled'
def proof(path):
    value=json.loads(path.read_bytes());config=RecoveryConfig(**value['config']);boot=value['boot']
    validate_capacity(boot['quirkbench.capacity'])
    return config,boot,lambda:True
def observe():
    return original(boot_record=root/'boot.json',control=root/'evidence/control',experiments=root/'experiments',
        command=command,verify_boot=proof,binding_reader=lambda:''' + repr(UUID) + r''',profiles_ready=lambda:True,
        contact=lambda:host().get('authenticated',False))
def native(argv,**kw):
    if len(argv)>1 and argv[1]=='show':
        return subprocess.CompletedProcess(argv,0,stdout='LoadState=loaded\nActiveState=inactive\nMainPID=0\nKillMode=control-group\nControlGroup=\nJob=0\n')
    if argv[0].endswith('nmtui'):
        return subprocess.run([sys.executable,'-c','print("NETWORK_SUBPROGRAM"); input(); print("NETWORK_RETURNED")'],check=False)
    if argv[0].endswith('systemctl') and argv[1]=='restart' and argv[-1]=='quirkbench-recovery.service':
        path=root/'actions';count=int(path.read_text()) if path.exists() else 0;path.write_text(str(count+1))
        os.read(release,1)
    return subprocess.CompletedProcess(argv,0,stdout='active')
original_actions=a.RecoveryActions
a.RecoveryActions=lambda:original_actions(control=root/'evidence/control',context=lambda:proof(root/'boot.json'),run=native,profiles_ready=lambda:True)
s.read_status=observe
original_present=t.present_once
t.switch_vt=lambda n:os.write(events,('HOST_VT_EVENT '+str(n)+'\n').encode())
t.present_once=lambda:original_present(marker=root/'presented',switch=t.switch_vt)
runpy.run_module('quirkbench.console',run_name='__main__')
'''


class Console:
    def __init__(self,root,*,term='linux'):
        self.root=root;self.master,slave=pty.openpty();self.release_read,self.release_write=os.pipe()
        self.events_read,self.events_write=os.pipe();self.events=bytearray();self.closed=False
        fcntl.ioctl(slave,0x5414,struct.pack('HHHH',24,80,0,0))
        script=root/'run-console.py';script.write_text(PROGRAM)
        self.process=subprocess.Popen([sys.executable,str(script),str(root),str(self.release_read),str(self.events_write)],
            stdin=slave,stdout=slave,stderr=slave,start_new_session=True,pass_fds=(self.release_read,self.events_write),
            env=dict(os.environ,TERM=term,PYTHONPATH=str(ROOT/'src')))
        os.close(slave);os.close(self.release_read)
        self.screen=pyte.Screen(80,24);self.stream=pyte.Stream(self.screen);self.raw=bytearray()

    @property
    def visible(self):return '\n'.join(self.screen.display)

    def until(self,predicate,timeout=5):
        deadline=time.monotonic()+timeout
        while not predicate():
            remaining=deadline-time.monotonic()
            assert remaining>0,self.visible+'\n'+self.raw[-4096:].decode(errors='replace')
            ready=select.select([self.master,self.events_read],[],[],remaining)[0]
            assert ready,self.visible
            if self.events_read in ready:self.events.extend(os.read(self.events_read,65536))
            if self.master not in ready:continue
            try:block=os.read(self.master,65536)
            except OSError:pytest.fail(self.visible)
            self.raw.extend(block);del self.raw[:-65536]
            self.stream.feed(block.decode('utf-8',errors='replace'))

    def see(self,value):self.until(lambda:value in self.visible)
    def send(self,value):os.write(self.master,value)
    def resize(self,width,height):
        self.screen.resize(height,width)
        fcntl.ioctl(self.master,0x5414,struct.pack('HHHH',height,width,0,0))
        os.kill(self.process.pid,signal.SIGWINCH)

    def close(self):
        if self.closed:return
        if self.process.poll() is None:
            os.killpg(self.process.pid,signal.SIGKILL);self.process.wait(timeout=2)
        for fd in (self.master,self.release_write,self.events_read,self.events_write):os.close(fd)
        self.closed=True


@pytest.fixture
def console(tmp_path):
    (tmp_path/'evidence/control').mkdir(parents=True);(tmp_path/'experiments').mkdir()
    (tmp_path/'host.json').write_text(json.dumps({'network':'disconnected'}))
    (tmp_path/'boot.json').write_text(json.dumps({'config':CONFIG.to_dict(),'boot':{
        'quirkbench.mode':'recovery','quirkbench.capacity':{'schema_version':1,'record_type':'prepared-capacity',
        'eligible':True,'prepared':True,'prepared_media_sha256':'a'*64,'experiment_mib':64,'evidence_mib':64}}}))
    c=Console(tmp_path)
    try:yield c
    finally:c.close()


def test_actual_initial_paint_help_navigation_resize_and_stable_refresh(console):
    c=console;c.see('QUIRKBENCH');c.see("Let's get this computer connected")
    assert 'USB' in c.visible and 'prepared' in c.visible
    assert (c.root/'presented').exists()  # no input needed
    c.send(b'?');c.see('Pairing never grants permission')
    c.send(b'?');c.see('Wi-Fi & Ethernet')
    c.send(b'\x1b[B');c.see('> Connect to controller')
    (c.root/'host.json').write_text(json.dumps({'network':'connected'}))
    c.see('Connect to your controller')
    assert '> Connect to controller' in c.visible
    c.resize(110,32);c.see('Enter Open')
    assert '> Connect to controller' in c.visible
    c.send(b'\r');c.see('Controller connection')
    c.send(b'\x04');assert c.process.wait(timeout=2)==0


def test_nmtui_subprogram_return_redraws_actual_dashboard(console):
    c=console;c.see("Let's get this computer connected")
    c.send(b'\r');c.see('NETWORK_SUBPROGRAM');c.send(b'\r')
    c.until(lambda:b'NETWORK_RETURNED' in c.raw)
    c.see('QUIRKBENCH');c.see('> Wi-Fi & Ethernet')


def test_slow_real_action_dispatch_remains_accessible_and_is_not_duplicated(console):
    c=console;c.see("Let's get this computer connected")
    c.send(b'\x1b[B\x1b[B\r');c.see('Troubleshooting')
    c.send(b'\x1b[B\r');c.see('Retrying existing recovery checks')
    assert (c.root/'actions').read_text()=='1'
    c.send(b'T');c.until(lambda:b'HOST_VT_EVENT 3' in c.events)
    c.send(b'\x1b');c.see('> Troubleshooting')
    c.send(b'\r');c.see('Restart local networking')
    c.send(b'\r');c.see('Retrying existing recovery checks')
    assert (c.root/'actions').read_text()=='1'
    c.send(b'\x1b');c.see('> Troubleshooting')
    os.write(c.release_write,b'x')
    c.until(lambda:'is running' not in c.visible)
    assert '> Troubleshooting' in c.visible and (c.root/'actions').read_text()=='1'


def test_failing_boot_and_ui_restart_keep_terminal_and_focus_available(console):
    c=console;c.see('prepared')
    (c.root/'boot.json').write_bytes(b'{malformed')
    c.see('Recovery checks need attention')
    c.send(b'T');c.until(lambda:b'HOST_VT_EVENT 3' in c.events)
    c.send(b'\x04');assert c.process.wait(timeout=2)==0
    c.close()
    # Same RAM marker prevents this UI restart from stealing VT3 focus.
    restarted=Console(c.root)
    try:
        restarted.see('Recovery checks need attention')
        assert b'HOST_VT_EVENT 2' not in restarted.events
        restarted.send(b'T');restarted.until(lambda:b'HOST_VT_EVENT 3' in restarted.events)
    finally:restarted.close()
