"""Editable private workspace preparation from a handed-off local Git source.

Only controller-owned private staging is changed. Native fetch permits the
explicit local source path; no remote protocol or user source commit is allowed.
"""
import os
from contextlib import contextmanager
from pathlib import Path
import shutil
import hashlib
import stat

from .contracts import Conflict,ContractError,canonical,identifier,sha256
from .filesystem import _managed_path
from .source_capture import capture,_git,_directory_owner,validate_capture
from .source_operation import verify_tree


@contextmanager
def _source_owner(path):
    # Approved user repositories need not be private; staging always must be.
    if not path.is_absolute() or path.resolve()!=path:
        raise ContractError('canonical approved source root required')
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    held=os.fstat(fd)
    def guard():
        named=path.lstat()
        if (held.st_dev,held.st_ino,held.st_uid,held.st_mode)!=(named.st_dev,named.st_ino,named.st_uid,named.st_mode) or path.resolve()!=path:
            raise Conflict('approved source root ownership changed')
    try:yield guard
    finally:os.close(fd)


def _git_metadata(workspace):
    # This is the fresh adapter-created ordinary Git directory, with detached
    # HEAD. Bound native/index interpretation through the last callback without
    # running Git or callbacks after the final source observation.
    directory=workspace/'.git';info=directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or directory.resolve()!=directory or info.st_uid!=os.geteuid():
        raise Conflict('prepared Git metadata directory changed')
    values=[(info.st_dev,info.st_ino,info.st_mode,info.st_uid)]
    for name in ('HEAD','index','config'):
        path=directory/name;fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            before=os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_uid!=os.geteuid() or before.st_size>128*1024**2:
                raise ContractError('prepared Git metadata is not bounded and owned')
            state=hashlib.sha256();total=0
            while block:=os.read(fd,1024**2):
                total+=len(block)
                if total>128*1024**2:raise ContractError('prepared Git metadata exceeds bounds')
                state.update(block)
            from .source_capture import _identity
            if _identity(before)!=_identity(os.fstat(fd)) or _identity(before)!=_identity(path.lstat()):
                raise Conflict('prepared Git metadata changed during observation')
            values.append((_identity(before),state.hexdigest()))
        finally:os.close(fd)
    final=directory.lstat()
    if values[0]!=(final.st_dev,final.st_ino,final.st_mode,final.st_uid):raise Conflict('prepared Git directory changed')
    return values


def validate(value):
    fields={'schema_version','record_type','workspace_id','base_oid','capture_sha256','allowed_untracked','provenance'}
    if (not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int
            or value['schema_version']!=1 or value['record_type']!='source-workspace-preparation'):
        raise ContractError('invalid private workspace preparation')
    identifier(value['workspace_id']);sha256(value['capture_sha256'])
    validate_capture({'schema_version':1,'record_type':'source-capture','base_oid':value['base_oid'],
        'archive_sha256':'0'*64,'manifest_sha256':'0'*64,'file_count':1,'allowed_untracked':value['allowed_untracked'],
        'provenance':value['provenance'],'complete':True})
    if len(canonical(value))>1024**2:raise ContractError('private workspace preparation exceeds bounds')
    return value


def prepare(repository,base_oid,allowed_untracked,stage,store,workspace_id, *,writer_quiesced,verify,provenance=None,fault_hook=None):
    """Keep the actual base and reproduce approved dirty bytes in a separate tree.

    The caller later proves whole-worker stop before publishing/registering the
    staged workspace. An interrupted stage is never a live editable workspace.
    """
    identifier(workspace_id);stage=Path(stage);root=Path(repository)
    if not stage.is_absolute() or stage.resolve()!=stage or stage.is_symlink():raise ContractError('canonical private preparation stage required')
    stage=_managed_path(stage);stage.mkdir(mode=0o700,parents=True,exist_ok=True)
    with _directory_owner(stage) as initial_stage_guard,_source_owner(root) as initial_source_guard:
        def initial_guard():
            verify();initial_stage_guard();initial_source_guard()
        original=capture(root,base_oid,allowed_untracked,stage/'capture',store,writer_quiesced=writer_quiesced,
            verify=initial_guard,provenance=provenance,fault_hook=fault_hook)
        verify_tree(store,original,verify=initial_guard)
        original_receipt=store.put(canonical(original))
        initial_stage_guard();initial_source_guard()
        output=_managed_path(stage/'output');output.mkdir(mode=0o700,exist_ok=True)
        if any(output.iterdir()):raise Conflict('source workspace staging already contains output; use a new reconciled worker stage')
        workspace=output/'workspace';workspace.mkdir(mode=0o700)
        fault=fault_hook or (lambda _:None)
        with _directory_owner(stage) as stage_guard,_directory_owner(output) as output_guard,_source_owner(root) as source_guard,_directory_owner(workspace) as workspace_guard:
            def guard():
                verify();stage_guard();output_guard();source_guard();workspace_guard();store.check_space(1024**2)
            _git(workspace,['init','--quiet','--template=','--object-format='+('sha256' if len(base_oid)==64 else 'sha1')],guard)
            _git(workspace,['fetch','--quiet','--no-tags','--no-recurse-submodules','--depth=1','--',str(root),base_oid],guard,
                local_fetch=True,timeout_s=3600)
            _git(workspace,['checkout','--quiet','--detach',base_oid],guard)
            fault('source_workspace_base_checked_out');guard()
            # All removed files belong to this fresh private checkout, never the user
            # tree. Existing extraction retains contained links and file modes.
            for path in workspace.iterdir():
                if path.name=='.git':continue
                guard()
                if path.is_dir() and not path.is_symlink():shutil.rmtree(path)
                else:path.unlink()
            from .build_pipeline import _extract_archive
            captured=_extract_archive(store.path(original['archive_sha256']),stage/'captured-tree',
                preserve_mode=True,reserve_bytes=store.reserve_bytes,verify=guard)
            guard()
            for path in captured.iterdir():
                guard();os.rename(path,workspace/path.name)
            fault('source_workspace_dirty_applied');guard()
            # Stage approved imported files in this private Git index; preserve all
            # actual bytes and retain the original base commit without committing.
            _git(workspace,['add','--force','--all','--','.'],guard)
            fault('source_workspace_verified');guard()
            metadata=_git_metadata(workspace)
            copied=capture(workspace,base_oid,allowed_untracked,stage/'replica-check',store,
                writer_quiesced=True,verify=guard,provenance=provenance)
            if copied!=original:raise Conflict('editable workspace does not reconstruct exact captured source')
            if _git_metadata(workspace)!=metadata:raise Conflict('prepared Git metadata changed after capture')
            stage_guard();output_guard();source_guard();workspace_guard()
            # The owner independently verifies this stopped stage before selection.
            return validate({'schema_version':1,'record_type':'source-workspace-preparation','workspace_id':workspace_id,
                'base_oid':base_oid,'capture_sha256':original_receipt.sha256,
                'allowed_untracked':original['allowed_untracked'],'provenance':original['provenance']})
