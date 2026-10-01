"""Fixed, bounded ALSA observations and an explicitly requested quiet test tone."""
from pathlib import Path
import math
import os
import selectors
import signal
import stat
import struct
import subprocess
import tempfile
import time
import wave

from .contracts import Outcome,canonical,digest
from .target import EvidenceChunk,RecipeOutput

LIMIT=256*1024


def command(argv,*,timeout=10):
    """Fixed tools inherit the recipe group so its outer deadline stops them too."""
    try:
        process=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL,
                                 env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','LC_ALL':'C.UTF-8'})
    except OSError:
        return {'status':'tool_unavailable','output':''}
    output=bytearray();deadline=time.monotonic()+timeout;status='complete'
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout,selectors.EVENT_READ)
            while selector.get_map():
                if time.monotonic()>=deadline: status='timed_out';break
                for key,_ in selector.select(min(0.1,max(0,deadline-time.monotonic()))):
                    block=os.read(key.fileobj.fileno(),8192)
                    if not block: selector.unregister(key.fileobj);continue
                    if len(output)+len(block)>LIMIT:
                        output.extend(block[:LIMIT-len(output)]);status='truncated';break
                    output.extend(block)
                if status=='truncated':break
        if status=='complete':
            try: code=process.wait(timeout=max(0.01,deadline-time.monotonic()))
            except subprocess.TimeoutExpired: status='timed_out'
            else:
                if code: status='failed'
        return {'status':status,'output':output.decode('utf-8',errors='replace')}
    finally:
        try: process.kill()
        except ProcessLookupError: pass
        process.wait(timeout=2);process.stdout.close()


def tone(path):
    """Five seconds, stereo 440 Hz, amplitude 5%; never changes mixer settings."""
    frames=bytearray()
    for n in range(48000*5):
        # Short fades avoid start/stop clicks.
        fade=min(1,n/4800,(48000*5-1-n)/4800)
        sample=int(32767*0.05*fade*math.sin(2*math.pi*440*n/48000))
        frames.extend(struct.pack('<hh',sample,sample))
    with wave.open(str(path),'wb') as stream:
        stream.setnchannels(2);stream.setsampwidth(2);stream.setframerate(48000);stream.writeframes(frames)
    return digest(path.read_bytes())


def audio_observation(experiment,*,proc_root=Path('/proc'),runner=command):
    parameters=experiment.parameters
    if set(parameters)!={'card','pcm_device','playback'} or type(parameters['card']) is not int or not 0<=parameters['card']<=31 or type(parameters['pcm_device']) is not int or not 0<=parameters['pcm_device']<=31 or type(parameters['playback']) is not bool:
        raise ValueError('invalid bounded audio parameters')
    card=parameters['card'];records={}
    allowed=['cards','pcm','version',f'card{card}/id']
    allowed.extend(f'card{card}/codec#{n}' for n in range(8))
    for name in allowed:
        path=proc_root/'asound'/name
        try:
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode): raise ValueError('nonregular ALSA metadata')
                raw=os.read(fd,LIMIT+1)
            finally: os.close(fd)
            records[name]={'status':'truncated' if len(raw)>LIMIT else 'observed',
                           'output':raw[:LIMIT].decode('utf-8',errors='replace')}
        except (OSError,ValueError): records[name]={'status':'unavailable','output':''}
    yield EvidenceChunk('audio-metadata',canonical(records))
    commands=[('kernel-log',['/usr/bin/dmesg','--kernel']),('alsa-playback-devices',['/usr/bin/aplay','--list-devices']),
              ('alsa-capture-devices',['/usr/bin/arecord','--list-devices']),
              ('alsa-controls',['/usr/bin/amixer','--card='+str(card),'contents'])]
    statuses={}
    for role,argv in commands:
        record=runner(argv);statuses[role]=record['status'];yield EvidenceChunk(role,canonical(record))
    measurements={'kernel_release':os.uname().release,'card':card,'pcm_device':parameters['pcm_device'],
                  'tool_status':statuses,'playback_requested':parameters['playback']}
    if parameters['playback']:
        with tempfile.TemporaryDirectory(prefix='quirkbench-audio-') as directory:
            path=Path(directory)/'tone.wav';measurements['stimulus_sha256']=tone(path)
            yield EvidenceChunk('audio-stimulus',path.read_bytes())
            record=runner(['/usr/bin/aplay','--device=hw:'+str(card)+','+str(parameters['pcm_device']),str(path)],timeout=10)
            measurements['playback_status']=record['status'];yield EvidenceChunk('audio-playback',canonical(record))
        outcome=Outcome.NEEDS_HUMAN if record['status']=='complete' else Outcome.INCONCLUSIVE
    else: outcome=Outcome.INCONCLUSIVE
    yield RecipeOutput(outcome,'Collected audio diagnostics'+(' and attempted the fixed test tone.' if parameters['playback'] else '.'),
                       measurements=measurements,limitations=['Tool success does not establish audible output or a kernel fault.',
                       'Record listening and the selected symptom through the controller observation commands.',
                       'No mixer, firmware, installed OS or internal storage changes were requested.'])
