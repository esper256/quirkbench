"""Exact controller preparation plans; references confer no implicit write grant."""
import hashlib
import os
from pathlib import Path
import struct

from .commission import CommissionError
from .contracts import canonical,digest,identifier,sha256
from .prepared_factory import validate as factory_identity
from .prepared_media import record,validate as validate_media

WINDOW=1024**2
DEVICE_FIELDS={'path','sysfs_path','major_minor','device_bytes','logical_sector_bytes',
               'controller_boot_id','diskseq','attachment_path','usb_busnum','usb_devnum'}
SOURCE_FIELDS={'path','sha256','size_bytes','manifest_sha256','authentication'}
FIELDS={'schema_version','record_type','preparation_id','target','source','factory',
        'device','observed_layout','controller','prepared_media'}


def observe_layout(fd,size_bytes):
    """Capture bounded metadata addresses, not an independent GPT validator.

    This is a read-only confirmation observation, never proof of exclusive use.
    Capture both GPT header/table locations, including old image-end backups.
    Address fields are untrusted and bounded; the existing native GPT reader must
    verify a regular-file view before admitting the layout. No partition entries
    or CRCs are interpreted here. Reconstructing/converting layouts is forbidden.
    """
    if type(size_bytes) is not int or size_bytes<2*WINDOW:
        raise CommissionError('selected USB is too small for preparation')
    ranges=[(0,WINDOW),(size_bytes-WINDOW,size_bytes)]
    def header(lba):
        if type(lba) is not int or not 1<=lba<size_bytes//512:
            raise CommissionError('GPT metadata address is outside selected USB')
        raw=os.pread(fd,512,lba*512)
        if len(raw)!=512 or raw[:8]!=b'EFI PART':
            raise CommissionError('GPT metadata header is missing; repair requires a separate explicit operation')
        size=struct.unpack_from('<I',raw,12)[0]
        current,alternate=struct.unpack_from('<QQ',raw,24)
        array,count,entry_size=struct.unpack_from('<QII',raw,72)
        if (not 92<=size<=512 or current!=lba or count<1 or entry_size<128
                or entry_size%128 or count*entry_size>65536
                or array<2 or array*512+count*entry_size>size_bytes):
            raise CommissionError('unsupported or unbounded GPT metadata addresses')
        ranges.extend([(lba*512,(lba+1)*512),
                       (array*512,array*512+((count*entry_size+511)//512)*512)])
        return alternate
    primary=os.pread(fd,512,512)
    if primary[:8]==b'EFI PART':
        alternate=header(1)
        if header(alternate)!=1:
            raise CommissionError('GPT headers disagree about metadata addresses')
    # Merge only metadata captures, so arbitrary data sectors never become an
    # apparent layout proof. Capture must be followed by native validation.
    merged=[]
    for start,end in sorted(ranges):
        if merged and start<=merged[-1][1]:merged[-1]=(merged[-1][0],max(merged[-1][1],end))
        else:merged.append((start,end))
    result=[]
    for start,end in merged:
        raw=os.pread(fd,end-start,start)
        if len(raw)!=end-start:raise CommissionError('selected USB layout observation ended early')
        result.append({'offset':start,'length':end-start,'sha256':hashlib.sha256(raw).hexdigest()})
    return result


def make(*,preparation_id,target,source,factory,device,observed_layout,controller):
    identity=factory_identity(factory)
    media=record(identity,source['sha256'],device['device_bytes'],
                 factory_data_end=identity.factory_data_end,version=2)
    value={'schema_version':1,'record_type':'usb-preparation-plan',
           'preparation_id':preparation_id,'target':target,'source':source,'factory':factory,
           'device':device,'observed_layout':observed_layout,'controller':controller,
           'prepared_media':media}
    return validate(value)


def reference(value):
    validate(value)
    return digest(canonical(value))


def validate(value):
    if (not isinstance(value,dict) or set(value)!=FIELDS
            or type(value['schema_version']) is not int or value['schema_version']!=1
            or value['record_type']!='usb-preparation-plan'):
        raise CommissionError('invalid USB preparation plan')
    identifier(value['preparation_id']);identifier(value['target'])
    source=value['source'];device=value['device'];controller=value['controller']
    if (not isinstance(source,dict) or set(source)!=SOURCE_FIELDS
            or not isinstance(source['path'],str) or not Path(source['path']).is_absolute()
            or type(source['size_bytes']) is not int or source['size_bytes']<=0
            or not isinstance(source['authentication'],dict)):
        raise CommissionError('invalid selected recovery artifact')
    sha256(source['sha256']);sha256(source['manifest_sha256'])
    authentication=source['authentication']
    if (set(authentication)!={'mode','trust_sha256','fingerprint'}
            or authentication['mode'] not in ('signed','unsigned-development')):
        raise CommissionError('invalid preparation source authentication')
    if authentication['mode']=='signed':
        import re
        sha256(authentication['trust_sha256'])
        if not isinstance(authentication['fingerprint'],str) or not re.fullmatch('[A-F0-9]{40}|[A-F0-9]{64}',authentication['fingerprint']):
            raise CommissionError('full independent recovery publisher fingerprint required')
    elif authentication['trust_sha256'] is not None or authentication['fingerprint'] is not None:
        raise CommissionError('unsigned development input cannot claim publisher authentication')
    if (not isinstance(device,dict) or set(device)!=DEVICE_FIELDS
            or any(not isinstance(device[name],str) or not Path(device[name]).is_absolute()
                   for name in ('path','sysfs_path','attachment_path'))
            or not isinstance(device['major_minor'],list) or len(device['major_minor'])!=2
            or any(type(number) is not int or number<0 for number in device['major_minor'])
            or device['logical_sector_bytes']!=512 or type(device['logical_sector_bytes']) is not int
            or any(type(device[name]) is not int or device[name]<=0
                   for name in ('device_bytes','diskseq','usb_busnum','usb_devnum'))):
        raise CommissionError('invalid selected USB observation')
    import uuid
    try:
        if str(uuid.UUID(device['controller_boot_id']))!=device['controller_boot_id']:raise ValueError()
    except (ValueError,TypeError,AttributeError) as exc:
        raise CommissionError('invalid preparation controller boot identity') from exc
    layout=value['observed_layout']
    if (not isinstance(layout,list) or not 2<=len(layout)<=6
            or any(not isinstance(entry,dict) or set(entry)!={'offset','length','sha256'}
                or type(entry['offset']) is not int or entry['offset']<0 or entry['offset']%512
                or type(entry['length']) is not int or entry['length']<=0 or entry['length']%512
                or entry['offset']+entry['length']>device['device_bytes'] for entry in layout)
            or layout[0]['offset']!=0 or layout[0]['length']<WINDOW
            or layout[-1]['offset']+layout[-1]['length']!=device['device_bytes']
            or layout[-1]['length']<WINDOW
            or sum(entry['length'] for entry in layout)>2*WINDOW+2*65536+1024
            or any(before['offset']+before['length']>=after['offset']
                   for before,after in zip(layout,layout[1:]))):
        raise CommissionError('invalid observed USB layout binding')
    for entry in layout:sha256(entry['sha256'])
    if not isinstance(controller,dict) or set(controller)!={'controller_url','certificate_sha256'}:
        raise CommissionError('invalid preparation controller trust')
    from .enrollment_client import endpoint
    import ipaddress
    address=ipaddress.ip_address(endpoint(controller['controller_url'])[0])
    if address.is_loopback or address.is_unspecified or address.is_multicast or address.is_link_local:
        raise CommissionError('prepared controller must use its configured reachable LAN address')
    sha256(controller['certificate_sha256'])
    factory=factory_identity(value['factory'])
    media=validate_media(value['prepared_media'],factory=factory,
        expected_artifact_sha256=source['sha256'],factory_data_end=factory.factory_data_end)
    if (media['schema_version']!=2 or media['completion']!='PREPARING'
            or media['device_bytes']!=device['device_bytes']
            or source['size_bytes']<(factory.factory_data_end+1)*512):
        raise CommissionError('preparation plan differs from selected source or USB')
    return value


def require_apply(value,confirmation,erase):
    if erase is not True:raise CommissionError('explicit erase acknowledgement is required; all credentials/evidence on this USB will be lost')
    if not isinstance(confirmation,str) or confirmation!=reference(value):
        raise CommissionError('USB confirmation differs; obtain and review a fresh device-bound plan')
    return value


def revalidate(value,fd,*,device_observer,source_reader,controller_reader):
    """No lock/filename/cache substitutes for current observed bytes and identity."""
    validate(value)
    if (device_observer()!=value['device']
            or observe_layout(fd,value['device']['device_bytes'])!=value['observed_layout']
            or source_reader()!={'source':value['source'],'factory':value['factory']}
            or controller_reader()!=value['controller']):
        raise CommissionError('USB, recovery artifact or controller trust changed; obtain a fresh plan')
