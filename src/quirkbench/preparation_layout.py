"""Describe bounded old metadata for explicit erasure, never repair/import it."""
import math
import os
import struct
import time

from .commission import CommissionError
from .preparation_plan import observe_layout


def inspect(fd,size_bytes,work,*,deadline,guard,runner=None):
    if (isinstance(deadline,bool) or not isinstance(deadline,(int,float))
            or not math.isfinite(deadline) or deadline<=0):
        raise CommissionError('USB layout observation requires a finite positive deadline')
    guard()
    if time.monotonic()>=deadline:raise CommissionError('USB layout observation deadline exceeded')
    observations=observe_layout(fd,size_bytes)
    primary=os.pread(fd,512,512);first=os.pread(fd,512,0);tail=os.pread(fd,512,size_bytes-512)
    source_bytes=None
    if primary[:8]==b'EFI PART':
        kind='gpt';alternate=struct.unpack_from('<Q',primary,32)[0]
        if 1<=alternate<size_bytes//512:source_bytes=(alternate+1)*512
    elif first[510:512]==b'\x55\xaa':kind='flat-mbr'
    elif not any(first+primary+tail):kind='no-partition-table'
    else:kind='unrecognized-metadata'
    result={'kind':kind,'previous_quirkbench_labels':False,'gpt_source_bytes':source_bytes,
        'stale_tail_gpt':tail[:8]==b'EFI PART' and source_bytes!=size_bytes}
    final=observe_layout(fd,size_bytes);guard()
    if time.monotonic()>=deadline or final!=observations:
        raise CommissionError('USB layout changed or observation deadline exceeded')
    return {'observation':observations,'description':result}
