"""Fixed candidate package assembly inside the existing delegated job service.

This adapter stages an unpublished sysroot. It has no database, signing, image
publication, target execution or operator approval authority.
"""
import argparse
import json
from contextlib import ExitStack
from pathlib import Path

from . import baseline_inputs
from .contracts import ContractError,Conflict,canonical,sha256
from .controller_setup import _managed_path
from .state_reader import read_file
from .store import atomic_write


def inner(cas,record,output, *,runner=None,marker=Path('/etc/quirkbench-container'),
          base_marker=Path('/etc/quirkbench-base-digest'),euid=None):
    from .build import _require_container
    from .build_pipeline import ResourceLimits,_tree_hash,_reject_credentials
    from .recovery_rootfs import CASReader
    from .source_capture import load_document
    output = _managed_path(output)
    if runner is None:
        _require_container();ResourceLimits.from_cgroup()
    value = baseline_inputs.validate(load_document(read_file(Path(record).parent,Path(record).name,limit=16384)))
    rootfs = baseline_inputs.install(CASReader(cas),value,output/'rootfs',runner=runner,
        marker=marker,base_marker=base_marker,euid=euid)
    _reject_credentials(rootfs)
    result = {'schema_version':1,'rootfs':'rootfs','input_sha256':sha256_file_record(value),
              'target_tree_sha256':_tree_hash(rootfs)}
    atomic_write(output/'rootfs-result.json',canonical(result))
    return result


def sha256_file_record(value):
    from .contracts import digest
    return digest(canonical(value))


def validate_result(output,value,result):
    """Stopped consumers must call this again before retaining any sysroot bytes."""
    from .build_pipeline import _tree_hash,_reject_credentials
    fields = {'schema_version','rootfs','input_sha256','target_tree_sha256'}
    if (not isinstance(result,dict) or set(result)!=fields or type(result['schema_version']) is not int
            or result['schema_version']!=1 or result['rootfs']!='rootfs'
            or result['input_sha256']!=sha256_file_record(baseline_inputs.validate(value))):
        raise ContractError('candidate rootfs result differs from pinned input')
    sha256(result['target_tree_sha256']);output = _managed_path(output)
    rootfs = output/'rootfs'
    if rootfs.resolve()!=rootfs or rootfs.is_symlink() or not rootfs.is_dir():
        raise ContractError('candidate rootfs is unavailable or linked')
    _reject_credentials(rootfs)
    if _tree_hash(rootfs)!=result['target_tree_sha256']:raise Conflict('candidate rootfs changed after assembly')
    raw = read_file(rootfs,'usr/lib/quirkbench/candidate-rootfs-input.json',limit=16384)
    if raw!=canonical(value)+b'\n':raise Conflict('candidate rootfs retained input differs')
    if read_file(rootfs,'etc/quirkbench-rootfs',limit=128)!=b'quirkbench-fedora-target-v1\n':
        raise ContractError('candidate rootfs marker differs')
    return rootfs


def prepare(root,stage,value,builder,verify,report,deadline, *,execute=None):
    from .recovery_podman import _verify_retained_builder_archive,_copy_cas_object
    from .recovery_rootfs import CASReader,MAX_CLOSURE_BYTES
    from .recovery_worker import execute_rootfs
    from .source_capture import _directory_owner,_staged_identity,load_document
    from .builder_setup import reserve_bytes,check_space
    root = Path(root);stage = _managed_path(stage)
    with _directory_owner(stage) as stage_owner:
        original_value,original_builder=value,builder
        frozen_value=canonical(baseline_inputs.validate(value));frozen_builder=canonical(builder)
        value=json.loads(frozen_value);builder=json.loads(frozen_builder)
        def pinned():
            verify();stage_owner()
            if canonical(original_value)!=frozen_value or canonical(original_builder)!=frozen_builder:
                raise Conflict('candidate worker binding changed')
        entry,snapshot,lock,refs = baseline_inputs.resolve(CASReader(root/'artifacts'),value,verify=pinned)
        fields = {'builder_image_digest','builder_config_digest','builder_archive_sha256'}
        if not isinstance(builder,dict) or set(builder)!=fields or builder['builder_image_digest']!=entry['builder_image_digest']:
            raise Conflict('candidate builder differs from pinned baseline')
        for field in ('builder_image_digest','builder_config_digest'):
            identity = builder[field]
            if not isinstance(identity,str) or not identity.startswith('sha256:'):raise ContractError('pinned candidate builder required')
            sha256(identity[7:])
        sha256(builder['builder_archive_sha256'])
        _verify_retained_builder_archive(root,builder['builder_archive_sha256'],builder['builder_config_digest'],require_no_entrypoint=True)
        pinned()
        inputs = stage/'candidate-inputs';inputs.mkdir(mode=0o700)
        cas = inputs/'cas';cas.mkdir(mode=0o700);(cas/'objects').mkdir(mode=0o700)
        output = stage/'candidate-output';output.mkdir(mode=0o700)
        with ExitStack() as stack:
            owners=[stack.enter_context(_directory_owner(path)) for path in (stage,inputs,cas,cas/'objects',output)]
            record = inputs/'input.json';record_identity = None
            def guard():
                pinned()
                for owner in owners:owner()
                if record_identity is not None:
                    if _staged_identity(record)!=record_identity:raise Conflict('candidate launch record changed')
                    if read_file(inputs,'input.json',limit=16384)!=frozen_value:raise Conflict('candidate launch record bytes changed')
                    if _staged_identity(record)!=record_identity:raise Conflict('candidate launch record changed')
            remaining = MAX_CLOSURE_BYTES
            reserve = reserve_bytes(root)
            def space(count):guard();check_space(stage,count,reserve);guard()
            for identity in refs:
                guard();remaining -= _copy_cas_object(root/'artifacts',identity,cas/'objects'/identity,remaining,space_check=space)
            baseline_inputs.resolve(CASReader(cas),value,verify=guard)
            atomic_write(record,frozen_value);record_identity=_staged_identity(record);guard()
            package = Path(__file__).resolve().parent
            # Container UID 0 maps to the rootless controller user. No host privilege
            # or device is passed; installroot writes are confined to private output.
            argv = ['/usr/bin/bash',str(package/'run-bounded-podman.sh'),'--rm','--pull=never','--network=none',
                '--user=0','--security-opt=no-new-privileges',
                '--volume',f'{inputs}:/workspace/inputs:ro,z','--volume',f'{output}:/workspace/output:rw,z',
                '--volume',f'{package}:/workspace/code/quirkbench:ro,z','--env','PYTHONPATH=/workspace/code',
                '--env','PYTHONDONTWRITEBYTECODE=1',builder['builder_config_digest'],
                '/usr/bin/python3','-m','quirkbench.candidate_rootfs_worker','--cas','/workspace/inputs/cas',
                '--input','/workspace/inputs/input.json','--output','/workspace/output']
            report('candidate-rootfs','Assembling the pinned baseline from retained local RPMs.')
            guard();summary = (execute or execute_rootfs)(argv,stage/'diagnostics/candidate-rootfs.log',verify=guard,
                deadline=deadline,max_duration=7200);guard()
            if summary['exit_code']!=0:raise ContractError('candidate rootfs assembly failed; inspect candidate-rootfs.log')
            result = load_document(read_file(output,'rootfs-result.json',limit=16384))
            validate_result(output,value,result);guard()
            validate_result(output,value,result)
            return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cas',type=Path,required=True)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args(argv);inner(args.cas,args.input,args.output)
    return 0


if __name__=='__main__':raise SystemExit(main())
