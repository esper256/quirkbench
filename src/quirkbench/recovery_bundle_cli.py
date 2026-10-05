"""Controller-free recovery input and build commands."""
import json
from pathlib import Path
import sys
from .build import BuildError
from .cli_output import emit, error


def run(args):
    from . import recovery_bundle as service
    from .store import StoragePressure
    import subprocess
    try:
        reserve=getattr(args,'free_space_reserve_gib',2)
        if not 0<=reserve<=1048576:raise BuildError('free-space reserve must be nonnegative GiB')
        if args.action=='plan':result=service.plan(args.spec,args.output)
        elif args.action=='prepare':
            result=service.prepare(packages=args.packages,public_key=args.public_key,spec=args.spec,output=args.output,
                builder_image=args.builder_image,epoch=args.epoch,reserve_bytes=reserve*1024**3,vendor_inventory=args.vendor_inventory)
        elif args.action=='verify':result=service.verify(args.bundle,expected=args.manifest_sha256,engine=args.engine)
        elif args.action in ('export','import'):
            result=service.transfer(args.bundle,args.output,expected=args.manifest_sha256,reserve_bytes=reserve*1024**3)
        else:
            overrides={name:getattr(args,name) for name in ('cpus','memory_gib') if getattr(args,name) is not None}
            result=service.build(args.bundle,args.output,expected=args.manifest_sha256,engine=args.engine,
                timeout=args.timeout,reserve_gib=reserve,**overrides)
        emit(args,result)
        return 0 if result.get('ready',True) else 2
    except (BuildError,OSError,ValueError,KeyError,StoragePressure,subprocess.SubprocessError) as exc:
        error(args,'Recovery inputs unavailable: '+str(exc));return 2
