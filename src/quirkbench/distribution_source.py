"""Import already prepared, pinned distribution source into private Git staging.

RPM %prep must have run in the existing pinned rootless worker. This adapter runs
only bounded local Git metadata commands and never executes package code.
"""
import os
import hashlib
from pathlib import Path
import stat
import tempfile
import configparser
import re
import time

from .baseline_catalog import validate_entry
from .contracts import Conflict, ContractError, canonical, identifier, sha256
from .source_capture import _directory_owner, _git, _identity, _observe, _path, _parent, _staged_identity, MAX_FILES
from .source_preparation import prepare
from .controller_setup import _private_path
from .state_config import outside_checkout


def snapshot(root, verify, *, git=False):
    """Exact bounded namespace with no-follow streaming leaf observations."""
    root = Path(root)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    held = os.fstat(fd)
    directories = []; files = []
    def walk(parent, prefix):
        verify()
        for name in sorted(os.listdir(parent)):
            if not prefix and name == '.git':
                if git: continue
                raise ContractError('prepared distribution source already has Git metadata')
            relative = prefix + name; _path(relative)
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if len(directories) + len(files) >= MAX_FILES:
                raise ContractError('distribution source namespace exceeds bounds')
            if info.st_uid != os.geteuid() or info.st_mode & 0o7000:
                raise ContractError('prepared distribution input has foreign ownership or special mode')
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    if _identity(info) != _identity(os.fstat(child)):
                        raise Conflict('prepared source directory changed')
                    directories.append((relative, _identity(info)))
                    walk(child, relative + '/')
                    if _identity(info) != _identity(os.fstat(child)) or _identity(info) != _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)):
                        raise Conflict('prepared source directory changed')
                finally: os.close(child)
            else:
                entry, identity = _observe(fd, relative, verify=verify)
                if identity is None: raise Conflict('prepared source input disappeared')
                files.append((entry, identity))
    try:
        walk(fd, '')
        if (held.st_dev, held.st_ino, held.st_mode, held.st_uid) != tuple(getattr(root.lstat(), key) for key in ('st_dev','st_ino','st_mode','st_uid')):
            raise Conflict('prepared source root changed')
    finally: os.close(fd)
    return (held.st_dev, held.st_ino, held.st_mode, held.st_uid), directories, files


def validate(value):
    fields = {'schema_version','record_type','baseline_id','baseline_sha256','source_package_sha256',
              'spec_sha256','package_files','prepared_source_tree_sha256','source_date_epoch','upstream_relationship'}
    if (not isinstance(value, dict) or set(value) != fields or type(value['schema_version']) is not int
            or value['schema_version'] != 1 or value['record_type'] != 'distribution-source-provenance'):
        raise ContractError('invalid distribution source provenance')
    identifier(value['baseline_id'])
    for name in ('baseline_sha256','source_package_sha256','spec_sha256','prepared_source_tree_sha256'): sha256(value[name])
    if type(value['source_date_epoch']) is not int or not 0 <= value['source_date_epoch'] <= 2**31-1:
        raise ContractError('bounded distribution source epoch required')
    files = value['package_files']
    if not isinstance(files, list) or not 1 <= len(files) <= 8192:
        raise ContractError('bounded distribution package input list required')
    names = []
    for item in files:
        if not isinstance(item, dict) or set(item) != {'path','sha256','mode'}:
            raise ContractError('invalid distribution package input')
        path = _path(item['path'])
        if path.parts[0] not in ('SPECS','SOURCES') or len(path.parts) < 2:
            raise ContractError('distribution input must belong to exact package staging')
        sha256(item['sha256'])
        if type(item['mode']) is not int or not 0 <= item['mode'] <= 0o777:
            raise ContractError('invalid distribution input mode')
        names.append(item['path'])
    if names != sorted(set(names)):
        raise ContractError('distribution package inputs must be unique and sorted')
    if len([name for name in names if name.startswith('SPECS/')]) != 1:
        raise ContractError('distribution source requires one exact spec')
    if value['upstream_relationship'] != {'kind':'distribution-prepared-source','upstream_base_oid':None}:
        raise ContractError('unproved upstream Git ancestry must remain unknown')
    if len(canonical(value)) > 4*1024**2: raise ContractError('distribution provenance exceeds bounds')
    return value


def prepared_hash(observation):
    """Match the existing source-stage tree identity using already checked bytes."""
    values = {}
    for name, identity in observation[1]:
        values[name] = (stat.S_IMODE(identity[2]), b'D'+name.encode())
    for entry, identity in observation[2]:
        name = entry['path']; prefix = name.encode()+b'\0'
        payload = (b'F'+prefix+bytes.fromhex(entry['sha256']) if entry['kind']=='file'
                   else b'L'+prefix+entry['target'].encode())
        values[name] = (entry['mode'], payload)
    state = hashlib.sha256()
    for name in sorted(values):
        mode, payload = values[name]
        state.update(len(payload).to_bytes(8,'big')+mode.to_bytes(4,'big')+payload)
    return state.hexdigest()


def references(value):
    value = validate(value)
    return sorted({value['baseline_sha256'], value['source_package_sha256'], value['spec_sha256']}
                  | {item['sha256'] for item in value['package_files']})


def retain_input(root, entry, expected_identity, stage, store, verify):
    """Stream through a held input into private serialization before CAS effects."""
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with _parent(root_fd, entry['path']) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                if _identity(os.fstat(fd)) != expected_identity: raise Conflict('distribution package input changed before retention')
                with tempfile.TemporaryDirectory(prefix='.distribution-input-', dir=stage) as temporary:
                    directory = Path(temporary)
                    with _directory_owner(directory) as serialization_guard:
                        pending = directory/'input'
                        output = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                        initial = os.fstat(output)
                        def output_guard():
                            serialization_guard()
                            held = os.fstat(output); named = pending.lstat()
                            if ((held.st_dev,held.st_ino,held.st_mode,held.st_uid,held.st_nlink) !=
                                    (initial.st_dev,initial.st_ino,initial.st_mode,initial.st_uid,1)
                                    or _identity(held) != _identity(named)):
                                raise Conflict('distribution serialization output moved or changed')
                        try:
                            state = hashlib.sha256()
                            while True:
                                verify(); output_guard()
                                block = os.read(fd, 1024**2)
                                if not block: break
                                store.check_space(len(block)); output_guard(); state.update(block)
                                view = memoryview(block)
                                while view:
                                    output_guard()
                                    count = os.write(output, view)
                                    if count <= 0: raise Conflict('short distribution input serialization')
                                    view = view[count:]
                            output_guard(); os.fsync(output); output_guard()
                        finally: os.close(output)
                        if (_identity(os.fstat(fd)) != expected_identity
                                or _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != expected_identity
                                or state.hexdigest() != entry['sha256']):
                            raise Conflict('distribution package input changed during retention')
                        before = _staged_identity(pending)
                        artifact = store.put_file(pending, expected_digest=entry['sha256'])
                        serialization_guard()
                        if _staged_identity(pending) != before: raise Conflict('distribution input serialization changed')
                        return artifact
            finally: os.close(fd)
    finally: os.close(root_fd)


class _GitPublicationInProgress(Exception):
    pass


def _git_node(metadata, parts, *,active):
    if (metadata.st_uid != os.geteuid() or metadata.st_mode & 0o7000
            or not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode))):
        raise ContractError('distribution Git tree is linked, foreign or special')
    if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
        # Native Git can replace index/HEAD or remove a transient lock while a
        # stat or held-descriptor observation is in flight. An unlinked ordinary
        # owned file has no external aliases, but must never be accepted as live
        # metadata: restart the entire bounded strict inspection instead.
        if active and metadata.st_nlink == 0:
            raise _GitPublicationInProgress()
        # Git publishes a loose object with link(temp, object), then unlink(temp).
        # Never accept either alias while linked; only request a fresh strict pass.
        if (active and metadata.st_nlink == 2 and len(parts) == 3 and parts[0] == 'objects'
                and re.fullmatch('[0-9a-f]{2}', parts[1])
                and re.fullmatch('(?:[0-9a-f]{38}|tmp_obj_[A-Za-z0-9]{6})', parts[2])):
            raise _GitPublicationInProgress()
        raise ContractError('distribution Git tree is linked, foreign or special')


def import_git_policy(source, *,recursive=True,active=False):
    """Require a strict pass, allowing bounded settling of active Git publication."""
    deadline = None
    for attempt in range(16):
        try:
            return _import_git_policy(source, recursive=recursive, active=active, settling_deadline=deadline)
        except _GitPublicationInProgress as exc:
            now = time.monotonic()
            if deadline is None: deadline = now + 0.25
            if attempt == 15 or now >= deadline:
                raise ContractError('distribution Git metadata publication did not settle') from exc
            time.sleep(0.005)


def _import_git_policy(source, *,recursive=True,active=False,settling_deadline=None):
    """Pure metadata fence; no callbacks or native Git interpretation."""
    def budget():
        if settling_deadline is not None and time.monotonic() >= settling_deadline:
            raise ContractError('distribution Git metadata publication exceeded settling budget')
    budget()
    from .state_reader import read_file
    directory = source/'.git'; info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or directory.resolve() != directory or info.st_uid != os.geteuid()
            or info.st_mode & 0o7000): raise ContractError('distribution Git directory is not freshly owned')
    allowed = {'HEAD','HEAD.lock','config','description','objects','refs','logs','index','index.lock','COMMIT_EDITMSG'}
    # Detaching HEAD locks packed refs and clears the AUTO_MERGE pseudoref,
    # even in a fresh import. Neither transient lock is stopped metadata.
    if active:allowed.update({'packed-refs.lock','AUTO_MERGE.lock'})
    unexpected = set(os.listdir(directory))-allowed
    if unexpected: raise ContractError('distribution Git namespace has unapproved behavior: '+repr(sorted(unexpected)[:4])[:256])
    for name in ('config','HEAD'):
        budget()
        item = directory/name; metadata = item.lstat()
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or name == 'config' and metadata.st_nlink != 1):
            raise ContractError('distribution Git metadata is linked or foreign')
        _git_node(metadata,(name,),active=active if name == 'HEAD' else False)
    config = configparser.ConfigParser(interpolation=None, strict=True)
    config.read_string(read_file(source,'.git/config',limit=16384).decode())
    if {section:dict(config[section]) for section in config.sections()} != {
            'core':{'repositoryformatversion':'0','filemode':'true','bare':'false','logallrefupdates':'true'}}:
        raise ContractError('distribution Git configuration has unapproved behavior')
    for name in os.listdir(directory):
        budget()
        item = directory/name
        try: metadata = item.lstat()
        except FileNotFoundError:
            if active: continue
            raise
        if (metadata.st_uid != os.geteuid() or metadata.st_mode & 0o7000
                or item.resolve() != item or not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode))):
            raise ContractError('distribution Git tree is linked, foreign or special')
        _git_node(metadata,(name,),active=active)
    for path in (directory/'objects', directory/'objects/info', directory/'refs', directory/'logs'):
        budget()
        if path.exists() or path.is_symlink():
            value = path.lstat()
            if not stat.S_ISDIR(value.st_mode) or path.resolve() != path or value.st_uid != os.geteuid():
                raise ContractError('distribution Git object/ref directory changed')
    for path in (directory/'objects/info/alternates',directory/'objects/info/http-alternates',directory/'refs/replace'):
        budget()
        if path.exists() or path.is_symlink(): raise ContractError('distribution Git object interpretation is unapproved')
    if recursive:
        root_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        count = [0]
        def nodes(parent, prefix=()):
            for name in os.listdir(parent):
                budget()
                parts = (*prefix, name)
                count[0] += 1
                if count[0] > 1000000: raise ContractError('distribution Git namespace exceeds bounds')
                try: before = os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    if active: continue
                    raise
                _git_node(before, parts, active=active)
                try:
                    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK |
                                    (os.O_DIRECTORY if stat.S_ISDIR(before.st_mode) else 0), dir_fd=parent)
                except FileNotFoundError:
                    if active: continue
                    raise
                try:
                    held = os.fstat(child)
                    _git_node(held, parts, active=active)
                    if ((not active or stat.S_ISDIR(before.st_mode))
                            and (before.st_dev,before.st_ino,before.st_uid,before.st_mode) != (held.st_dev,held.st_ino,held.st_uid,held.st_mode)):
                        raise Conflict('distribution Git node changed during policy check')
                    if stat.S_ISDIR(before.st_mode): nodes(child, parts)
                    try: current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        if active and stat.S_ISREG(held.st_mode): continue
                        raise
                    _git_node(current, parts, active=active)
                    if ((not active or stat.S_ISDIR(held.st_mode))
                            and (held.st_dev,held.st_ino,held.st_uid,held.st_mode) != (current.st_dev,current.st_ino,current.st_uid,current.st_mode)):
                        raise Conflict('distribution Git node changed during policy check')
                finally: os.close(child)
        try:
            nodes(root_fd)
            held = os.fstat(root_fd); named = directory.lstat()
            if (held.st_dev,held.st_ino,held.st_uid,held.st_mode) != (named.st_dev,named.st_ino,named.st_uid,named.st_mode):
                raise Conflict('distribution Git root changed during policy check')
        finally: os.close(root_fd)
    budget()
    return (info.st_dev,info.st_ino,info.st_mode,info.st_uid)


def import_prepared(entry, prepared, source_stage, stage, store, workspace_id, *, verify):
    """Return existing preparation v1; caller still owes stopped-owner publication."""
    validate_entry(entry); identifier(workspace_id)
    source_stage = Path(source_stage); stage = Path(stage)
    for path in (source_stage, stage):
        if not path.is_absolute() or path.resolve() != path:
            raise ContractError('canonical private distribution staging required')
        outside_checkout(path); _private_path(path)
    if source_stage == stage or source_stage.is_relative_to(stage) or stage.is_relative_to(source_stage):
        raise ContractError('distribution import and source stages must be separate')
    expected = {'schema_version','kernel_srpm_sha256','kernel_source_nevra','spec_sha256','source','source_tree_sha256','source_date_epoch'}
    if (not isinstance(prepared, dict) or set(prepared) != expected or type(prepared['schema_version']) is not int
            or prepared['schema_version'] != 1 or prepared['kernel_srpm_sha256'] != entry['kernel_srpm_sha256']
            or prepared['kernel_source_nevra'] != entry['kernel_source_nevra'] or prepared['source'] != str(source_stage/'source')):
        raise Conflict('distribution source differs from pinned preparation')
    sha256(prepared['source_tree_sha256']); sha256(prepared['spec_sha256'])
    epoch = prepared['source_date_epoch']
    if type(epoch) is not int or not 0 <= epoch <= 2**31-1: raise ContractError('bounded source epoch required')
    store.verify(entry['kernel_srpm_sha256'])
    stage.mkdir(mode=0o700, parents=True, exist_ok=True)
    source = source_stage/'source'
    # The RPM-created tree can start at 0755 beneath private worker staging.
    # Change only the held owned directory, after proving its named path.
    with _directory_owner(source_stage) as parent_guard:
        parent_guard(); verify(); parent_guard()
        if source.resolve() != source: raise Conflict('prepared source root is linked')
        fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or _identity(info) != _identity(source.lstat()):
                raise Conflict('prepared source root changed or is foreign')
            os.fchmod(fd, 0o700)
            if _identity(os.fstat(fd)) != _identity(source.lstat()):
                raise Conflict('prepared source root changed during mode selection')
        finally: os.close(fd)
    with _directory_owner(source_stage) as source_stage_guard, _directory_owner(stage) as stage_guard, _directory_owner(source) as source_guard:
        git_identity = [None]; native_active = [False]
        def guard():
            verify(); source_stage_guard(); stage_guard(); source_guard(); store.check_space(1024**2)
            if git_identity[0] is not None and import_git_policy(source, recursive=False,active=native_active[0]) != git_identity[0]:
                raise Conflict('fresh distribution Git directory changed')
        def git_fence():
            source_stage_guard(); stage_guard(); source_guard()
            if git_identity[0] is None:
                if (source/'.git').exists() or (source/'.git').is_symlink():
                    raise ContractError('prepared distribution source already has Git metadata')
            elif import_git_policy(source) != git_identity[0]:
                raise Conflict('fresh distribution Git directory changed')
        def native_guard():
            guard()
            if git_identity[0] is not None and import_git_policy(source,active=native_active[0]) != git_identity[0]:
                raise Conflict('fresh distribution Git directory changed')
        def native(arguments, **options):
            # Git atomically replaces its index/refs and removes lock files.
            # In-flight reads still reject every unsafe node; pure launch and
            # stopped reads additionally require stable named identities.
            native_active[0] = True
            try: return _git(source, arguments, native_guard, before_launch=git_fence, **options)
            finally: native_active[0] = False
        before = snapshot(source, guard)
        if prepared_hash(before) != prepared['source_tree_sha256']:
            raise Conflict('prepared distribution source tree differs')
        guard(); package_root = source_stage/'rpm-topdir'
        files = []; package_snapshots = {}
        for directory in ('SPECS','SOURCES'):
            root = package_root/directory
            if root.resolve() != root or not root.is_dir(): raise Conflict('distribution package input directory missing or linked')
            initial = snapshot(root, guard); package_snapshots[root] = initial
            for item, identity in initial[2]:
                if item['kind'] != 'file': raise ContractError('package provenance requires regular single-link inputs')
                artifact = retain_input(root, item, identity, stage, store, guard)
                files.append({'path':directory+'/'+item['path'], 'sha256':artifact.sha256, 'mode':item['mode']})
        spec = [item for item in files if item['path'].startswith('SPECS/')]
        if len(spec) != 1 or spec[0]['sha256'] != prepared['spec_sha256']:
            raise Conflict('prepared distribution spec differs')
        baseline = store.put(canonical(entry))
        provenance = validate({'schema_version':1,'record_type':'distribution-source-provenance',
            'baseline_id':entry['baseline_id'], 'baseline_sha256':baseline.sha256,
            'source_package_sha256':entry['kernel_srpm_sha256'], 'spec_sha256':prepared['spec_sha256'],
            'package_files':sorted(files, key=lambda item:item['path']),
            'prepared_source_tree_sha256':prepared['source_tree_sha256'], 'source_date_epoch':epoch,
            'upstream_relationship':{'kind':'distribution-prepared-source','upstream_base_oid':None}})
        artifact = store.put(canonical(provenance))
        if snapshot(source, guard) != before: raise Conflict('prepared distribution source changed before import')
        native(['init','--quiet','--template='])
        git_identity[0] = import_git_policy(source)
        native(['add','--force','--all','--','.'], timeout_s=3600)
        native(['commit','--quiet','--no-gpg-sign','-m','Quirkbench imported distribution source baseline'],
             import_epoch=epoch, timeout_s=3600)
        base = native(['rev-parse','--verify','HEAD^{commit}']).decode().strip()
        native(['checkout','--quiet','--detach',base])
        result = prepare(source, base, [], stage, store, workspace_id, writer_quiesced=True,
            verify=guard, provenance={'source_package_sha256':entry['kernel_srpm_sha256'],
                                     'distribution_patches_sha256':artifact.sha256})
        # No native commands, CAS writes or callbacks follow these final fences.
        git_fence()
        def final_guard():
            source_stage_guard(); stage_guard(); source_guard()
            if import_git_policy(source, recursive=False) != git_identity[0]:
                raise Conflict('fresh distribution Git directory changed')
        if snapshot(source, final_guard, git=True) != before:
            raise Conflict('prepared distribution source changed during import')
        for root, original in package_snapshots.items():
            if snapshot(root, final_guard) != original: raise Conflict('distribution package inputs changed during import')
        final_guard(); git_fence()
        return result
