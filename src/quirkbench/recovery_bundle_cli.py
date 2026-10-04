"""Controller-free recovery input and build commands."""
import json
from pathlib import Path
import sys
from .build import BuildError


def add_parser(commands):
    root=commands.add_parser('recovery-bundle',help='prepare, verify and build from one portable unsigned input bundle')
    actions=root.add_subparsers(dest='action',required=True)
    plan=actions.add_parser('plan',help='stage selected repositories and print exact download argv; does not download')
    plan.add_argument('--spec',type=Path,required=True);plan.add_argument('--output',type=Path,required=True)
    prepare=actions.add_parser('prepare',help='verify downloaded signed RPMs and retain a complete bundle')
    for name in ('packages','public-key','spec','output'):prepare.add_argument('--'+name,type=Path,required=True)
    prepare.add_argument('--builder-image',required=True);prepare.add_argument('--epoch',type=int,required=True)
    prepare.add_argument('--free-space-reserve-gib',type=int,default=2)
    verify=actions.add_parser('verify',help='verify all inputs and RPM signatures; optionally check the local builder')
    verify.add_argument('bundle',type=Path);verify.add_argument('--manifest-sha256')
    verify.add_argument('--engine',choices=('podman','docker'))
    for name in ('export','import'):
        command=actions.add_parser(name,help='copy verified immutable inputs into a new directory')
        command.add_argument('bundle',type=Path);command.add_argument('--output',type=Path,required=True)
        command.add_argument('--manifest-sha256',required=name=='import',help='expected digest from the selected trusted snapshot')
        command.add_argument('--free-space-reserve-gib',type=int,default=2)
    build=actions.add_parser('build',help='build an unsigned image using the existing foreground container pipeline')
    build.add_argument('bundle',type=Path);build.add_argument('--output',type=Path,required=True)
    build.add_argument('--manifest-sha256');build.add_argument('--engine',choices=('podman','docker'),default='podman')
    build.add_argument('--cpus',type=int);build.add_argument('--memory-gib',type=int)
    build.add_argument('--timeout',type=int,default=3600);build.add_argument('--free-space-reserve-gib',type=int,default=2)


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
                builder_image=args.builder_image,epoch=args.epoch,reserve_bytes=reserve*1024**3)
        elif args.action=='verify':result=service.verify(args.bundle,expected=args.manifest_sha256,engine=args.engine)
        elif args.action in ('export','import'):
            result=service.transfer(args.bundle,args.output,expected=args.manifest_sha256,reserve_bytes=reserve*1024**3)
        else:
            overrides={name:getattr(args,name) for name in ('cpus','memory_gib') if getattr(args,name) is not None}
            result=service.build(args.bundle,args.output,expected=args.manifest_sha256,engine=args.engine,
                timeout=args.timeout,reserve_gib=reserve,**overrides)
        print(json.dumps(result,sort_keys=True))
        return 0 if result.get('ready',True) else 2
    except (BuildError,OSError,ValueError,KeyError,StoragePressure,subprocess.SubprocessError) as exc:
        print('Recovery inputs unavailable: '+str(exc),file=sys.stderr);return 2
