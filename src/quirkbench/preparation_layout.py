"""Observe existing layout through bounded bytes and the existing native reader.

No selected device is passed to a tool. GPT inspection uses a disposable regular
metadata view; this never grants repair, conversion, formatting or device writes.
"""
import hashlib
import math
import os
from pathlib import Path
import re
import struct
import time

from .commission import CommissionError
from .image import _run
from .preparation_plan import observe_layout


def inspect(fd,size_bytes,work,*,deadline,guard,runner=_run):
    if (isinstance(deadline,bool) or not isinstance(deadline,(int,float))
            or not math.isfinite(deadline) or deadline<=0):
        raise CommissionError('USB layout observation requires a finite positive deadline')
    guard()
    if time.monotonic()>=deadline:raise CommissionError('USB layout observation deadline exceeded')
    observations=observe_layout(fd,size_bytes)
    raw=[]
    for entry in observations:
        guard()
        if time.monotonic()>=deadline:raise CommissionError('USB layout observation deadline exceeded')
        block=os.pread(fd,entry['length'],entry['offset'])
        if len(block)!=entry['length'] or hashlib.sha256(block).hexdigest()!=entry['sha256']:
            raise CommissionError('USB layout changed during observation')
        raw.append(block)
    first=raw[0];primary=first[512:1024]
    # Observe stale tail metadata too: full replacement erases it, never repairs
    # or imports the old layout. Its bytes remain bound to the confirmation.
    tail=raw[-1][-512:]
    stale_tail=tail[:8]==b'EFI PART' and (primary[:8]!=b'EFI PART'
            or struct.unpack_from('<Q',primary,32)[0]!=size_bytes//512-1)
    if primary[:8]!=b'EFI PART':
        if not any(first) and stale_tail:
            kind='stale-backup-gpt'
        elif not any(any(block) for block in raw):
            kind='no-partition-table'
        elif first[510:512]==b'\x55\xaa':
            # Flat MBR partition records are wholly contained in the first
            # sector. Extended/EBR and protective-without-GPT are not accepted.
            partitions=[]
            for position in range(446,510,16):
                entry=first[position:position+16]
                start,count=struct.unpack_from('<II',entry,8)
                if entry[4] in (0x05,0x0f,0x85,0xee):
                    raise CommissionError('unsupported extended or damaged protective layout; no preparation writes authorized')
                if not any(entry):continue
                if (entry[0] not in (0,0x80) or entry[4]==0 or start<1 or count<1
                        or (start+count)*512>size_bytes):
                    raise CommissionError('unsupported MBR layout; no preparation writes authorized')
                partitions.append((start,start+count))
            partitions.sort()
            if not partitions or any(a[1]>b[0] for a,b in zip(partitions,partitions[1:])):
                raise CommissionError('ambiguous MBR layout; no preparation writes authorized')
            kind='flat-mbr'
        else:
            raise CommissionError('unrecognized USB metadata; inspect it locally before requesting preparation')
        result={'kind':kind,'previous_quirkbench_labels':False,'gpt_source_bytes':None}
    else:
        # A flashed factory's valid secondary header can be at the original
        # image end. Verify that original geometry, not a tool-reconstructed GPT.
        alternate=struct.unpack_from('<Q',primary,32)[0]
        source_bytes=(alternate+1)*512
        view=Path(work)/'observed-layout'
        with view.open('xb') as stream:stream.truncate(source_bytes)
        destination=os.open(view,os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC)
        try:
            for entry,block in zip(observations,raw):
                start=entry['offset']
                if start>=source_bytes:continue
                block=block[:source_bytes-start]
                if os.pwrite(destination,block,start)!=len(block):
                    raise CommissionError('short regular layout-view write')
            def native(*a):
                guard();remaining=deadline-time.monotonic()
                if remaining<=0:raise CommissionError('USB layout validation deadline exceeded')
                return runner(*a,timeout_s=remaining)
            output=native('sgdisk','--print',str(view))
            checked=native('sgdisk','--verify',str(view))
            # Partition names are reported data, not tool diagnostics; the
            # shipped QUIRKBENCH-RECOVERY label is not a recovery warning.
            diagnostics='\n'.join(line for line in (output+'\n'+checked).splitlines()
                if not re.match(r'^\s*\d+\s+\d+\s+\d+\s',line))
            if (re.search(r'warning|caution|error|invalid|corrupt|damaged|converting|rebuilding|recovery|problem:',
                          diagnostics,re.I)
                    or 'No problems found.' not in checked):
                raise CommissionError('GPT validation requires repair or conversion; no preparation writes authorized')
            # A successful exit alone cannot hide a native repair of the view.
            for entry,block in zip(observations,raw):
                start=entry['offset']
                if start<source_bytes and os.pread(destination,min(len(block),source_bytes-start),start)!=block[:source_bytes-start]:
                    raise CommissionError('native layout inspection changed captured metadata')
            result={'kind':'gpt','previous_quirkbench_labels':'QUIRKBENCH' in output,
                    'gpt_source_bytes':source_bytes}
        finally:os.close(destination)
    result['stale_tail_gpt']=stale_tail
    final_observation=observe_layout(fd,size_bytes)
    guard()
    if time.monotonic()>=deadline or final_observation!=observations:
        raise CommissionError('USB layout changed or observation deadline exceeded')
    return {'observation':observations,'description':result}
