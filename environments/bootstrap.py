#!/usr/bin/env python3
"""Prepare a reproducible software-development virtualenv; no controller or image work."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import selectors
import signal
import shutil
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
CONSTRAINTS=ROOT/'development/constraints.txt'
IDENTITY='''import json,platform,sys,sysconfig
print(json.dumps(dict(version=list(sys.version_info[:3]),implementation=sys.implementation.name,
 compiler=platform.python_compiler(),platform=platform.platform(),abi=sysconfig.get_config_var('SOABI'),
 executable=sys.executable,base_executable=getattr(sys,'_base_executable',sys.executable),
 prefix=sys.prefix,base_prefix=sys.base_prefix)))'''
EDITABLE='''import importlib.metadata as m,json
print(m.distribution('quirkbench').read_text('direct_url.json') or '{}')'''
PACKAGES='''import importlib.metadata as m,json
print(json.dumps({d.metadata['Name'].lower():d.version for d in m.distributions()}))'''


class SetupError(RuntimeError):pass


def interpreter(name):
    found=shutil.which(name)
    if not found:raise SetupError('Python interpreter not found: '+name+'; select an installed Python 3.11+ with --python')
    return Path(found).absolute()


def identity(python):
    result=subprocess.run([str(python),'-I','-c',IDENTITY],capture_output=True,text=True,timeout=10)
    if result.returncode:raise SetupError('Selected interpreter cannot start: '+str(python))
    value=json.loads(result.stdout)
    if value['version'][:2]<[3,11]:raise SetupError('Python 3.11+ is required; select another interpreter with --python')
    return value


def compatible(selected,installed,venv):
    fields=('version','implementation','compiler','abi')
    return (all(selected[k]==installed[k] for k in fields)
        and Path(selected['base_executable']).resolve()==Path(installed['base_executable']).resolve()
        and Path(installed['prefix']).resolve()==venv.resolve()
        and installed['prefix']!=installed['base_prefix'])


def environment(python,venv,selected,*,evidence=None):
    """Never clear an existing destination, including incompatible/incomplete virtualenvs."""
    if venv.exists():
        cfg=venv/'pyvenv.cfg';binary=venv/'bin/python'
        if not cfg.is_file() or not binary.is_file():
            raise SetupError('Destination is not a complete virtualenv; preserve it and choose a new --venv path: '+str(venv))
        if 'include-system-site-packages = false' not in cfg.read_text().lower():
            raise SetupError('Virtualenv includes ambient packages; preserve it and choose a new --venv path')
        if not compatible(selected,identity(binary),venv):
            raise SetupError('Virtualenv belongs to a different interpreter/build; preserve it and choose a new --venv path')
        return binary,False
    evidence=evidence or Path(tempfile.mkdtemp(prefix='quirkbench-bootstrap-venv-'))
    log=evidence/'venv.log'
    try:run([python,'-m','venv',venv],log,timeout=60)
    except SetupError as exc:
        raise SetupError('Cannot create virtualenv. Install your distro venv/ensurepip support '
            '(Debian/Ubuntu: python3-venv; Fedora: python3-pip), then choose a new --venv path. '
            +'diagnostic='+str(log)+'; '+log.read_text(errors='replace')[-2000:]) from exc
    return venv/'bin/python',True


def constraint_versions():
    return dict(line.lower().split('==',1) for line in CONSTRAINTS.read_text().splitlines() if line and not line.startswith('#'))


def dependency_key(selected):
    data={'interpreter':{k:selected[k] for k in ('version','implementation','compiler','platform','abi')},
          'constraints':hashlib.sha256(CONSTRAINTS.read_bytes()).hexdigest(),
          'project':hashlib.sha256((ROOT/'pyproject.toml').read_bytes()).hexdigest()}
    return hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()


def run(argv,log,*,timeout=300,env=None):
    # Preserve each phase, including a failed reuse check followed by installation.
    original=log;index=1
    while log.exists():
        index+=1;log=original.with_name(original.stem+'-'+str(index)+original.suffix)
    started=time.monotonic();deadline=started+timeout;tail=bytearray();total=0
    process=subprocess.Popen([str(a) for a in argv],cwd=ROOT,stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,env=env,start_new_session=True)
    selector=selectors.DefaultSelector();selector.register(process.stdout,selectors.EVENT_READ)
    timed_out=False;status=125
    def stop():
        try:os.killpg(process.pid,signal.SIGTERM)
        except ProcessLookupError:return
        # Give ordinary subprocesses a brief orderly termination opportunity.
        try:process.wait(timeout=.2)
        except subprocess.TimeoutExpired:pass
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
    try:
        while selector.get_map():
            if time.monotonic()>=deadline:
                timed_out=True;stop();break
            for entry,_ in selector.select(min(.1,max(0,deadline-time.monotonic()))):
                chunk=os.read(entry.fd,65536)
                if not chunk:selector.unregister(entry.fd);continue
                total+=len(chunk);tail.extend(chunk)
                if len(tail)>1024**2:del tail[:len(tail)-1024**2]
        if timed_out:status=124
        else:status=process.wait(timeout=max(.01,deadline-time.monotonic()))
    except subprocess.TimeoutExpired:
        timed_out=True;stop();status=124
    finally:
        if status or process.poll() is None:stop()
        process.wait(timeout=2);selector.close();process.stdout.close()
        log.write_bytes((('[truncated '+str(total-len(tail))+' bytes; retained final 1 MiB]\n').encode() if total>len(tail) else b'')+tail)
        log.with_suffix(log.suffix+'.exit').write_text(str(status if status>=0 else 128-status)+'\n')
    if timed_out:raise SetupError('Setup phase timed out; diagnostic='+str(log))
    if status:raise SetupError('Command failed ('+str(status)+'); diagnostic='+str(log))
    return round(time.monotonic()-started,3)


def dependencies(python,venv,key,cache,evidence):
    receipt=venv/'bootstrap-report.json'
    if receipt.is_file():
        try:
            previous=json.loads(receipt.read_text())
            actual=json.loads(subprocess.check_output([str(python),'-I','-c',PACKAGES],text=True,timeout=10))
            editable=json.loads(subprocess.check_output([str(python),'-I','-c',EDITABLE],text=True,timeout=10))
            if (editable.get('url')==ROOT.as_uri() and editable.get('dir_info',{}).get('editable') is True
                and previous.get('dependency_key')==key and previous.get('checkout')==str(ROOT)
                and all(actual.get(k)==v for k,v in constraint_versions().items())):
                run([python,'-m','pip','check'],evidence/'pip-check.log',timeout=30)
                return True
        except (ValueError,SetupError,subprocess.SubprocessError):pass
    common=[python,'-m','pip','install','--cache-dir',cache,'--constraint',CONSTRAINTS]
    run([*common,'setuptools','wheel'],evidence/'build-tools.log')
    run([*common,'--no-build-isolation','--editable',str(ROOT)+'[test]'],evidence/'dependencies.log')
    run([python,'-m','pip','check'],evidence/'pip-check.log',timeout=30)
    return False


def tools():
    result={}
    for name in ('git','make','openssl','gpg'):
        path=shutil.which(name)
        result[name]={'available':path is not None}
        if path:
            try:
                argv=[path,'version'] if name=='openssl' else [path,'--version']
                output=subprocess.run(argv,capture_output=True,text=True,timeout=10)
                result[name]['version']=output.stdout.splitlines()[0] if output.stdout else 'unknown'
            except (OSError,subprocess.SubprocessError):result[name]['version']='unavailable'
    missing=[name for name in ('git','make') if not result[name]['available']]
    if missing:raise SetupError('Missing required development tools: '+', '.join(missing)+'; install git and make with your distro package manager')
    return result


def diagnose(python,destination):
    """Opt-in only. Keep normal assertions and product diagnostics enabled."""
    destination.mkdir()
    run([python,'-I',ROOT/'tests/faulthandler_probe.py','--duration','1','--delay','.25'],destination/'stdlib.log',timeout=10)
    copied=destination/'test_minimal_dump.py';shutil.copyfile(ROOT/'tests/faulthandler_minimal.py',copied)
    env=dict(os.environ);env['PYTEST_DISABLE_PLUGIN_AUTOLOAD']='1'
    run([python,'-m','pytest','-c','/dev/null','-q','-s',copied,'-o','faulthandler_timeout=0.1'],destination/'pytest.log',timeout=10,env=env)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python',default=sys.executable,help='installed Python 3.11+; default is the interpreter running this script')
    parser.add_argument('--venv',type=Path,default=ROOT/'.venv')
    parser.add_argument('--cache-dir',type=Path,default=Path(os.environ.get('XDG_CACHE_HOME',Path.home()/'.cache'))/'quirkbench/development')
    parser.add_argument('--report',type=Path,help='also copy the nonsecret report here')
    parser.add_argument('--skip-smoke',action='store_true',help='CI only: the selected existing software suite runs separately')
    parser.add_argument('--check-archive',action='store_true',help='build an unsigned controller archive in temporary storage')
    parser.add_argument('--diagnose-runtime',action='store_true',help='run the opt-in bounded stdlib/pytest crash diagnostic once')
    args=parser.parse_args(argv);started=time.monotonic();report={"schema_version":1,"status":"failed"};evidence=None;venv=None
    try:
        evidence=Path(tempfile.mkdtemp(prefix='quirkbench-bootstrap-',dir=args.report.parent if args.report else None))
        report['evidence_directory']=str(evidence)
        native=tools();python=interpreter(args.python);selected=identity(python)
        venv=args.venv.expanduser().resolve();python,created=environment(python,venv,selected,evidence=evidence)
        key=dependency_key(selected);cache=args.cache_dir.expanduser().resolve()/key;cache.mkdir(parents=True,exist_ok=True)
        reused=dependencies(python,venv,key,cache,evidence)
        report.update({'schema_version':1,'checkout':str(ROOT),'source_sha':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
            'source_dirty':bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True)),
            'interpreter':selected,'dependency_key':key,'venv_created':created,'dependencies_reused':reused,'tools':native,
            'packages':json.loads(subprocess.check_output([str(python),'-I','-c',PACKAGES],text=True,timeout=10)),
            'smoke':'not_run','archive':'not_requested','diagnostic':'not_requested'})
        if args.diagnose_runtime:
            with tempfile.TemporaryDirectory(prefix='quirkbench-runtime-diagnostic-') as tmp:
                # Retain the actual diagnostic logs beyond the temporary test file.
                try:diagnose(python,Path(tmp)/'probe')
                except SetupError as exc:
                    raise SetupError('Runtime diagnostic failed; retained logs='+str(evidence/'stdlib.log')+' and '+str(evidence/'pytest.log')) from exc
                finally:
                    for path in (Path(tmp)/'probe').glob('*.log*'):
                        shutil.copyfile(path,evidence/path.name)
            report['diagnostic']='passed'
        if args.check_archive:
            with tempfile.TemporaryDirectory(prefix='quirkbench-dev-package-') as tmp:
                archive=Path(tmp)/'controller.tar'
                run([python,ROOT/'environments/build-controller-archive.py','--output',archive],evidence/'archive.log',timeout=90)
                if not archive.is_file():raise SetupError('Archive helper did not emit an archive')
            report['archive']='passed'
        if not args.skip_smoke:
            report['smoke_seconds']=run(['make','smoke','PYTHON='+str(python)],evidence/'smoke.log',timeout=120)
            report['smoke']='passed'
        report['status']='ready';report['elapsed_seconds']=round(time.monotonic()-started,3)
        raw=json.dumps(report,sort_keys=True,indent=2)+'\n';(venv/'bootstrap-report.json').write_text(raw)
        print(('Development dependencies ready; smoke not run' if args.skip_smoke else 'Development setup and smoke passed')+'; report='+str(venv/'bootstrap-report.json'))
        for name in ('openssl','gpg'):
            if not native[name]['available']:print('Optional '+name+' unavailable; its native regression coverage is not verified')
        print('Focused tests: make test PYTHON='+shlex.quote(str(python))+' TESTS=tests/test_cli.py')
        print('Archive: make controller-archive PYTHON='+shlex.quote(str(python))+' OUTPUT=/path/to/new-controller.tar')
        return 0
    except (SetupError,OSError,ValueError,subprocess.SubprocessError) as exc:
        report['error']=str(exc)
        print('Development setup failed: '+str(exc),file=sys.stderr);return 2
    finally:
        report['elapsed_seconds']=round(time.monotonic()-started,3)
        raw=json.dumps(report,sort_keys=True,indent=2)+'\n'
        if evidence:(evidence/'report.json').write_text(raw)
        if args.report and args.report.parent.is_dir():args.report.write_text(raw)
        if evidence:print('Setup evidence: '+str(evidence))


if __name__=='__main__':raise SystemExit(main())
