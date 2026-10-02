"""Exact candidate package closure; no recovery or physical execution authority."""
from pathlib import Path
import json
import hashlib
import os
import stat
from .contracts import ContractError,Conflict,canonical,sha256
from .baseline_catalog import validate_entry,INPUT_DIGEST_FIELDS


def cas_root(store):
    from .store import ArtifactStore
    from .recovery_rootfs import CASReader
    if isinstance(store,ArtifactStore):return store.root
    if isinstance(store,CASReader):return store.objects.parent
    return store.root/'artifacts'


def metadata(store,identity,limit=1024**2):
    from .recovery_podman import _metadata_object
    from .product_contracts import _pairs,_depth
    from .build import BuildError
    try:raw = _metadata_object(cas_root(store),identity,limit)
    except BuildError as exc:raise ContractError('pinned baseline metadata unavailable: '+identity) from exc
    try:
        value = json.loads(raw,object_pairs_hook=_pairs,
            parse_constant=lambda _:(_ for _ in ()).throw(ContractError('nonfinite baseline metadata')))
        _depth(value);return value
    except (ValueError,UnicodeError,RecursionError) as exc:raise ContractError('invalid pinned baseline metadata: '+identity) from exc


def package_closure(store,entry):
    """Reconcile the catalog, snapshot and lock before considering any native work."""
    from .recovery_rootfs import validate_snapshot,_rpm_row
    from .recovery_podman import _metadata_object
    from .build import BuildError
    validate_entry(entry)
    snapshot = validate_snapshot(metadata(store,entry['rpm_snapshot_sha256']))
    expected = [(item['name'],item['nevra']) for item in entry['packages']]
    if [(item['name'],item['nevra']) for item in snapshot['packages']] != expected:
        raise ContractError('candidate RPM snapshot differs from the exact supported baseline')
    if any(item['name']=='gpg-pubkey' for item in snapshot['packages']):
        raise ContractError('candidate package closure contains an unpinned key import')
    expected_lock = ('\n'.join(sorted(_rpm_row(item['name'],item['nevra']) for item in snapshot['packages']))+'\n').encode()
    try:raw = _metadata_object(cas_root(store),entry['target_rpm_lock_sha256'],1024**2)
    except BuildError as exc:raise ContractError('pinned baseline target lock unavailable: '+entry['target_rpm_lock_sha256']) from exc
    if raw != expected_lock:raise ContractError('candidate target lock differs from pinned RPM identities')
    return snapshot,raw


def verify_object(store,identity,limit, *,verify=lambda:None):
    """Bounded streaming CAS read; the held file and its canonical name must agree."""
    from .source_capture import _identity
    sha256(identity);root = Path(cas_root(store));root_fd = objects_fd = fd = -1
    try:
        if not root.is_absolute() or root.resolve() != root:raise ContractError('noncanonical candidate CAS root')
        root_fd = os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        objects_fd = os.open('objects',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=root_fd)
        roots = [os.fstat(root_fd),os.fstat(objects_fd)]
        def guard():
            names = [root.lstat(),os.stat('objects',dir_fd=root_fd,follow_symlinks=False)]
            for before,held,named in zip(roots,[os.fstat(root_fd),os.fstat(objects_fd)],names):
                stable = lambda info:(info.st_dev,info.st_ino,info.st_mode,info.st_uid)
                if stable(before)!=stable(held) or stable(held)!=stable(named):
                    raise Conflict('candidate CAS directory moved')
            if root.resolve()!=root:raise Conflict('candidate CAS root moved')
        guard();fd = os.open(identity,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=objects_fd)
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink!=1
                or before.st_uid!=os.geteuid() or not 0 < before.st_size <= limit):
            raise ContractError('candidate CAS object outside regular-file byte bounds: '+identity)
        state = hashlib.sha256();total = 0
        while True:
            verify();guard()
            if _identity(before)!=_identity(os.fstat(fd)) or _identity(before)!=_identity(os.stat(identity,dir_fd=objects_fd,follow_symlinks=False)):
                raise Conflict('candidate CAS object moved or changed: '+identity)
            block = os.read(fd,min(1024**2,limit-total+1))
            if not block:break
            total += len(block)
            if total>limit:raise ContractError('candidate CAS object exceeds byte bounds: '+identity)
            state.update(block)
        guard()
        if total!=before.st_size or state.hexdigest()!=identity:raise ContractError('candidate CAS object failed hash verification: '+identity)
        return total
    except OSError as exc:raise ContractError('pinned candidate CAS object unavailable or unsafe: '+identity) from exc
    finally:
        for handle in (fd,objects_fd,root_fd):
            if handle>=0:os.close(handle)


def preflight(store,entry, *,verify=lambda:None):
    """Freeze catalog meaning, then verify every retained byte after the last callback."""
    from .recovery_rootfs import MAX_RPM_BYTES,MAX_CLOSURE_BYTES
    frozen = canonical(validate_entry(entry));pinned = json.loads(frozen)
    snapshot,lock = package_closure(store,pinned)
    references = {pinned[key] for key in INPUT_DIGEST_FIELDS}
    references.update(item['sha256'] for item in snapshot['packages'])
    references.add(pinned['build_recipe']['digest'])
    references.update(item['digest'] for item in pinned['target_recipes'])
    def closure(callback):
        remaining = MAX_CLOSURE_BYTES
        for identity in sorted(references):
            remaining -= verify_object(store,identity,min(MAX_RPM_BYTES,remaining),verify=callback)
    closure(verify);verify()
    if canonical(entry)!=frozen:raise Conflict('candidate baseline changed during verification')
    if package_closure(store,pinned)!=(snapshot,lock):raise Conflict('candidate baseline package metadata changed')
    closure(lambda:None)
    return snapshot,lock,sorted(references)


def validate(value):
    fields = {'schema_version','record_type','baseline_sha256','rpm_snapshot_sha256','target_rpm_lock_sha256'}
    if (not isinstance(value,dict) or set(value) != fields or type(value['schema_version']) is not int
            or value['schema_version'] != 1 or value['record_type'] != 'candidate-rootfs-input'):
        raise ContractError('invalid candidate rootfs input')
    for key in fields-{'schema_version','record_type'}:sha256(value[key])
    return value


def input_record(store,entry):
    frozen = canonical(validate_entry(entry));pinned = json.loads(frozen)
    snapshot,lock,refs = preflight(store,pinned)
    baseline = store.put(frozen)
    value = validate({'schema_version':1,'record_type':'candidate-rootfs-input','baseline_sha256':baseline.sha256,
        'rpm_snapshot_sha256':pinned['rpm_snapshot_sha256'],'target_rpm_lock_sha256':pinned['target_rpm_lock_sha256']})
    if canonical(entry)!=frozen:raise Conflict('candidate baseline changed during publication')
    # Publication hooks and storage callbacks cannot leave an acknowledged partial closure.
    resolve(store,value)
    return value,sorted(set(refs)|{baseline.sha256})


def resolve(store,value, *,verify=lambda:None):
    frozen = canonical(validate(value));pinned = json.loads(frozen)
    entry = validate_entry(metadata(store,pinned['baseline_sha256'],4*1024**2))
    if any(pinned[key] != entry[key] for key in ('rpm_snapshot_sha256','target_rpm_lock_sha256')):
        raise Conflict('candidate rootfs input differs from retained baseline')
    snapshot,lock,refs = preflight(store,entry,verify=verify)
    if canonical(value)!=frozen:raise Conflict('candidate input changed during verification')
    verify_object(store,pinned['baseline_sha256'],4*1024**2)
    return entry,snapshot,lock,sorted(set(refs)|{pinned['baseline_sha256']})


def install(store,value,output, *,runner=None,marker=Path('/etc/quirkbench-container'),
            base_marker=Path('/etc/quirkbench-base-digest'),euid=None):
    """Candidate assembly reuses the stock Fedora package installer, without recovery policy."""
    from .recovery_rootfs import _install_packages,_run
    entry,snapshot,lock,refs = resolve(store,value)
    return _install_packages(entry,snapshot['packages'],lock.decode(),store,output,runner=runner or _run,
        marker=marker,base_marker=base_marker,euid=euid,record=value,candidate=True)
