"""Strict offline baseline closure for the existing FedoraComposer.

Only the versioned investigation path selects this adapter. Manual composition
retains its existing repository resolution behavior.
"""
import hashlib
import json
from pathlib import Path
import re
import stat
import tarfile

from .contracts import ContractError, Conflict, canonical, digest, identifier, sha256
from .baseline_catalog import validate_entry, NEVRA
from .recovery_rootfs import validate_snapshot, MAX_RPM_BYTES, MAX_CLOSURE_BYTES
from .source_capture import load_document
from .filesystem import read_file

FIELDS={'entry_file','entry_sha256','snapshot_file','snapshot_sha256','rpms_file','rpms_sha256'}
GENERATED=frozenset({'kernel-quirkbench','quirkbench-experiment-userspace'})
# These are required by the existing rpm-ostree/sysusers/account setup, not additions
# the adapter may fetch from another repository.
REQUIRED=frozenset({'rpm','nss-altfiles','systemd','fedora-release'})


def metadata(value):
    if not isinstance(value,dict) or set(value)!=FIELDS:raise ContractError('invalid pinned composition input')
    for key in ('entry','snapshot','rpms'):
        sha256(value[key+'_sha256'])
        path=Path(value[key+'_file'])
        if path.resolve()!=path or path.is_symlink() or not path.is_file():raise ContractError('pinned composition input is missing or linked')
    from .build import sha256_file
    for key in ('entry','snapshot','rpms'):
        if sha256_file(Path(value[key+'_file']))!=value[key+'_sha256']:raise ContractError('pinned composition input changed')
    entry=validate_entry(load_document(read_file(Path(value['entry_file']).parent,Path(value['entry_file']).name,limit=4*1024**2),limit=4*1024**2))
    snapshot=validate_snapshot(load_document(read_file(Path(value['snapshot_file']).parent,Path(value['snapshot_file']).name,limit=4*1024**2),limit=4*1024**2))
    if digest(canonical(entry))!=value['entry_sha256'] or entry['rpm_snapshot_sha256']!=value['snapshot_sha256']:
        raise ContractError('pinned composition baseline differs')
    if [(p['name'],p['nevra']) for p in snapshot['packages']]!=[(p['name'],p['nevra']) for p in entry['packages']]:
        raise ContractError('pinned composition snapshot differs from catalog')
    names={p['name'] for p in entry['packages']}
    if not REQUIRED<=names:raise ContractError('baseline lacks required rpm-ostree account/package inputs: '+','.join(sorted(REQUIRED-names)))
    if names & (GENERATED|{'kernel','kernel-core','kernel-modules','fwupd','udisks2'}):
        raise ContractError('baseline conflicts with candidate replacement or protection policy')
    return entry,snapshot


def recipes(root, entry):
    """Verify the exact deployed recipe bytes and their fixed reviewed code bindings."""
    from .recipe_registry import installed_registry,load_manifest,reviewed_bindings
    registry=installed_registry(Path(__file__).parent/'recipes',candidate=True)
    directory=Path(root)/'usr/lib/quirkbench/quirkbench'
    for item in entry['target_recipes']:
        local=registry.records.get(item['recipe_id'])
        if local is None or local[1]!=item['digest']:raise ContractError('baseline recipe differs from installed reviewed binding')
        manifest=load_manifest(read_file(directory/'recipes',local[2].name,limit=65536))
        if digest(read_file(directory/'recipes',local[2].name,limit=65536))!=item['digest']:
            raise ContractError('composed recipe manifest differs from pinned baseline')
        binding=reviewed_bindings()[item['recipe_id']]
        module=Path(binding.__module__.rsplit('.',1)[-1]+'.py')
        if digest(read_file(directory,module,limit=1024**2))!=manifest['code_sha256']:
            raise ContractError('composed recipe code differs from manifest')


def validate_result(value):
    fields={'schema_version','record_type','baseline_sha256','rpm_snapshot_sha256','packages','target_recipes'}
    if (not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int or value['schema_version']!=1
            or value['record_type']!='pinned-composition-result'):raise ContractError('invalid pinned composition result')
    sha256(value['baseline_sha256']);sha256(value['rpm_snapshot_sha256'])
    packages=value['packages']
    if not isinstance(packages,list) or not 3<=len(packages)<=8194:raise ContractError('bounded composed package list required')
    keys=[]
    for item in packages:
        if not isinstance(item,dict) or set(item)!={'name','nevra','sha256'}:raise ContractError('invalid composed package')
        identifier(item['name']);sha256(item['sha256'])
        if not isinstance(item['nevra'],str) or not NEVRA.fullmatch(item['nevra']) or not item['nevra'].startswith(item['name']+'-'):
            raise ContractError('invalid composed package identity')
        keys.append((item['name'],item['nevra']))
    if keys!=sorted(set(keys)):raise ContractError('composed package identities must be sorted and unique')
    rs=value['target_recipes']
    if not isinstance(rs,list) or not 1<=len(rs)<=128:raise ContractError('bounded composed recipe list required')
    for item in rs:
        if not isinstance(item,dict) or set(item)!={'recipe_id','digest'}:raise ContractError('invalid composed recipe')
        identifier(item['recipe_id']);sha256(item['digest'])
    if [i['recipe_id'] for i in rs]!=sorted({i['recipe_id'] for i in rs}):raise ContractError('composed recipes must be unique and sorted')
    return value


def package_archive(path,packages, *,destination=None,verify=lambda:None,reserve=0,
                    names=None,epoch=0,mode=0o600):
    """A bounded USTAR transport of exactly the snapshot's ordinary RPM objects."""
    from .builder_setup import check_space
    expected={p['sha256']+'.rpm':p for p in packages} if names is None else names
    if not expected or any(not re.fullmatch(r'[A-Za-z0-9._+-]{1,96}\.rpm',name) for name in expected):
        raise ContractError('invalid RPM archive names')
    from .filesystem import held_parent
    from .source_capture import _identity
    import os
    from contextlib import nullcontext
    path=Path(path)
    with held_parent(path) as (parent,ancestor_guard):
        fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        with os.fdopen(fd,'rb') as stream:
            original=os.fstat(stream.fileno())
            if not stat.S_ISREG(original.st_mode) or original.st_nlink!=1 or original.st_uid!=os.geteuid():
                raise ContractError('RPM archive is foreign, linked or special')
            def guard():
                verify();ancestor_guard()
                if (_identity(os.fstat(stream.fileno()))!=_identity(original) or
                        _identity(os.stat(path.name,dir_fd=parent,follow_symlinks=False))!=_identity(original)):
                    raise Conflict('RPM archive changed during verification')
            seen=set();total=0
            while True:
                guard();header=stream.read(512)
                if len(header)!=512:raise ContractError('truncated pinned RPM archive')
                if header==b'\0'*512:
                    tail=stream.read(10241)
                    if len(tail)>10240 or any(tail):raise ContractError('invalid pinned RPM archive terminator')
                    break
                if header[156:157] not in (b'0',b'\0'):raise ContractError('pinned RPM archive contains extended or special members')
                name=header[:100].rstrip(b'\0').decode('ascii')
                if name not in expected or name in seen or header[345:500].rstrip(b'\0'):
                    raise ContractError('pinned RPM archive has undeclared paths')
                try:size=int(header[124:136].rstrip(b'\0 ').lstrip(b' ') or b'0',8)
                except ValueError as exc:raise ContractError('invalid pinned RPM archive size') from exc
                if not 0<size<=MAX_RPM_BYTES:raise ContractError('pinned RPM archive member exceeds bounds')
                seen.add(name);total+=size
                if total>MAX_CLOSURE_BYTES:raise ContractError('pinned RPM archive exceeds closure bounds')
                stream.seek((size+511)//512*512,1)
            if seen!=set(expected):raise ContractError('pinned RPM archive omits baseline objects')
            guard();stream.seek(0)
            if destination is not None:
                destination=Path(destination)
                with held_parent(destination) as (dest_parent,dest_guard):
                    guard();dest_guard();os.mkdir(destination.name,0o700,dir_fd=dest_parent)
                holder=held_parent(destination/'unused')
            else:holder=nullcontext((None,lambda:None))
            with holder as (dest_fd,dest_guard):
                seen=set()
                with tarfile.open(fileobj=stream,mode='r:') as archive:
                    for member in archive:
                        guard();dest_guard()
                        if (member.name not in expected or member.name in seen or not member.isfile() or member.mode!=mode
                                or member.uid!=0 or member.gid!=0 or member.mtime!=epoch):
                            raise ContractError('pinned RPM archive metadata differs')
                        seen.add(member.name);state=hashlib.sha256()
                        with archive.extractfile(member) as source:
                            output=None
                            if destination is not None:
                                guard();dest_guard()
                                out_fd=os.open(member.name,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=dest_fd)
                                output=os.fdopen(out_fd,'w+b');before=os.fstat(out_fd);written=0;sealed=None
                            def output_guard():
                                guard();dest_guard()
                                if output is not None:
                                    actual=os.fstat(output.fileno());named=os.stat(member.name,dir_fd=dest_fd,follow_symlinks=False)
                                    if ((actual.st_dev,actual.st_ino)!=(before.st_dev,before.st_ino) or
                                            _identity(actual)!=_identity(named) or actual.st_nlink!=1 or actual.st_size!=written or
                                            (sealed is not None and _identity(actual)!=_identity(sealed))):
                                        raise Conflict('RPM extraction output changed')
                            try:
                                while block:=source.read(1024**2):
                                    output_guard();state.update(block)
                                    if output is not None:
                                        check_space(destination,len(block),reserve);output.write(block);output.flush();written+=len(block)
                                    output_guard()
                                if output is not None:
                                    sealed=os.fstat(output.fileno());output.seek(0);actual_hash=hashlib.sha256()
                                    while block:=output.read(1024**2):output_guard();actual_hash.update(block)
                                    output_guard()
                                    if actual_hash.hexdigest()!=expected[member.name]['sha256']:
                                        raise ContractError('RPM extracted bytes differ')
                            finally:
                                if output is not None:output.close()
                        if state.hexdigest()!=expected[member.name]['sha256']:raise ContractError('pinned RPM archive bytes differ')
                if seen!=set(expected):raise ContractError('pinned RPM archive became incomplete')
                guard();dest_guard()
    return expected


def pack(store,packages,path,verify, *,reserve=0):
    from .baseline_inputs import verify_object
    from .builder_setup import check_space
    with Path(path).open('xb') as target:
        with tarfile.open(fileobj=target,mode='w',format=tarfile.USTAR_FORMAT) as archive:
            for package in packages:
                identity=package['sha256'];size=verify_object(store,identity,MAX_RPM_BYTES,verify=verify)
                check_space(Path(path).parent,size+10240,reserve)
                member=tarfile.TarInfo(identity+'.rpm');member.size=size;member.mode=0o600
                with store.path(identity).open('rb') as stream:archive.addfile(member,stream)
    package_archive(path,packages,verify=verify)


def prepare(value,stage,run, *,reserve=0):
    entry,snapshot=metadata(value);packages=Path(stage)/'baseline-rpms'
    package_archive(Path(value['rpms_file']),snapshot['packages'],destination=packages,reserve=reserve)
    paths=[str(packages/(p['sha256']+'.rpm')) for p in snapshot['packages']]
    from .recovery_rootfs import _rpm_row
    observed=run(['rpm','-qp','--qf','%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\t%{ARCH}\n',*paths],'inspect-pinned-baseline',300)
    expected=''.join(sorted(_rpm_row(p['name'],p['nevra'])+'\n' for p in snapshot['packages']))
    if ''.join(sorted(observed.splitlines(keepends=True)))!=expected:raise ContractError('pinned baseline RPM headers differ')
    run(['createrepo_c',str(packages)],'index-pinned-baseline')
    (Path(stage)/'baseline.repo').write_text('[quirkbench-baseline]\nname=Pinned baseline\nbaseurl='+packages.as_uri()+'\nenabled=1\ngpgcheck=0\n')
    return entry,snapshot


def validate_downloads(snapshot,custom,directory):
    from .build import sha256_file
    expected={p['sha256'] for p in snapshot['packages']}|{sha256_file(p) for p in custom.glob('*.rpm')}
    observed={sha256_file(p) for p in directory.glob('*.rpm')}
    if expected!=observed:raise ContractError('composition dependency bytes differ from pinned baseline/replacements')


def verify_checkout(entry,snapshot,custom,checkout,run,baseline_sha):
    from .recovery_rootfs import _rpm_row
    from .build import sha256_file
    packages=list(snapshot['packages']);names=[]
    for path in sorted(custom.glob('*.rpm')):
        raw=run(['rpm','-qp','--qf','%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\t%{ARCH}\n',str(path)],'inspect-generated-'+path.stem,60).strip()
        fields=raw.split('\t')
        if len(fields)!=3 or fields[0] not in GENERATED:raise ContractError('undeclared generated replacement package')
        name,evr,arch=fields;names.append(name)
        packages.append({'name':name,'nevra':name+'-'+evr+'.'+arch,'sha256':sha256_file(path)})
    if set(names)!=GENERATED or len(names)!=len(GENERATED):raise ContractError('exact generated replacement package set required')
    observed=run(['rpm','--root',str(checkout),'-qa','--qf','%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\t%{ARCH}\n'],'inspect-composed-baseline',300)
    expected=''.join(sorted(_rpm_row(p['name'],p['nevra'])+'\n' for p in packages))
    if ''.join(sorted(observed.splitlines(keepends=True)))!=expected:raise ContractError('composed RPM closure differs from pinned baseline/replacements')
    recipes(checkout,entry)
    return validate_result({'schema_version':1,'record_type':'pinned-composition-result','baseline_sha256':baseline_sha,
        'rpm_snapshot_sha256':entry['rpm_snapshot_sha256'],'packages':sorted(packages,key=lambda p:(p['name'],p['nevra'])),
        'target_recipes':entry['target_recipes']})


def validate_lock(value,packages):
    """Check the existing rpm-ostree EVRA/digest lock format, without repinning."""
    if not isinstance(value,dict) or set(value)-{'packages','metadata'} or not isinstance(value.get('packages'),dict):
        raise ContractError('invalid rpm-ostree dependency lock')
    expected={p['name']:p for p in packages}
    if len(expected)!=len(packages):raise ContractError('joined composer does not support multilib lock identities')
    if set(value['packages'])!=set(expected):raise ContractError('dependency lock package names differ')
    for name,item in value['packages'].items():
        if not isinstance(item,dict) or set(item)-{'evra','digest','metadata'} or not isinstance(item.get('evra'),str):
            raise ContractError('unsupported rpm-ostree dependency lock package format')
        evra=item['evra'];evra=evra if ':' in evra else '0:'+evra
        package=expected[name]
        if package['nevra']!=name+'-'+evra or item.get('digest')!='sha256:'+package['sha256']:
            raise ContractError('dependency lock differs from pinned RPM identity/bytes')
    return value


def expected_tree(raw,entry):
    from .compose import ComposeInputs
    identity=ComposeInputs.from_mapping(raw).identity()
    return {'ref':'quirkbench/experiments/'+identity,'edition':'2024','releasever':entry['fedora_release'],
        'repos':['quirkbench-baseline','quirkbench-experiment'],
        'packages':sorted({p['name'] for p in entry['packages']})+['kernel-quirkbench','quirkbench-experiment-userspace'],
        'exclude-packages':['kernel','kernel-core','kernel-modules','fwupd','udisks2'],
        'recommends':False,'documentation':False,'selinux':False,'boot-location':'modules','no-initramfs':True,
        'default-target':'multi-user.target','units':['NetworkManager.service'],
        'add-commit-metadata':{'quirkbench.protection-profile':raw.get('protection_profile','usb-excluded-controllers-v1'),
            'quirkbench.build-provenance-sha256':raw['artifact_sha256']['build_provenance'],
            'quirkbench.config-sha256':raw['artifact_sha256']['config'],'quirkbench.kernel-release':raw['kernel_release'],
            'quirkbench.baseline-sha256':raw['pinned_baseline']['entry_sha256'],
            'quirkbench.rpm-snapshot-sha256':entry['rpm_snapshot_sha256']},
        'check-passwd':{'type':'none'},'check-groups':{'type':'none'}}
