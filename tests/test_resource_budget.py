"""Budgets follow workload and effective capacity, not desktop assumptions."""
import pytest
from quirkbench.build import BuildError
from quirkbench.resource_budget import Capacity, resolve, GIB


def test_small_preparation_does_not_require_a_kernel_build_machine():
    budget=resolve('preparation',available=Capacity(2,2*GIB),environ={})
    assert (budget.cpus,budget.memory_bytes)==(1,GIB)
    with pytest.raises(BuildError,match='minimum'):resolve('kernel',available=Capacity(2,2*GIB),environ={})


def test_dedicated_and_explicit_budgets_can_use_more_than_half_host():
    host=Capacity(8,8*GIB)
    assert resolve('kernel',available=host,environ={}).memory_bytes==4*GIB
    assert resolve('kernel',available=host,mode='dedicated',environ={}).memory_bytes==8*GIB
    selected=resolve('kernel',available=host,environ={'QUIRKBENCH_CPUS':'8','QUIRKBENCH_MEMORY_GIB':'8'})
    assert (selected.cpus,selected.memory_bytes)==(8,8*GIB)


def test_nested_capacity_is_not_halved_again():
    budget=resolve('recovery',available=Capacity(2,4*GIB,True,True),environ={})
    assert (budget.cpus,budget.memory_bytes)==(2,4*GIB)


@pytest.mark.parametrize('options',[{'cpus':0},{'cpus':True},{'cpus':9},{'memory_bytes':9*GIB},
    {'memory_bytes':3*GIB},{'mode':'unbounded'}])
def test_invalid_or_excess_budgets_fail(options):
    with pytest.raises(BuildError):resolve('recovery',available=Capacity(8,8*GIB),environ={},**options)


def test_cgroup_ancestry_and_affinity_limit_host_capacity(tmp_path,monkeypatch):
    from pathlib import Path
    from quirkbench import resource_budget as budget
    root=tmp_path/'cgroup';current=root/'parent/worker';current.mkdir(parents=True)
    (current/'memory.max').write_text('max');(current/'cpu.max').write_text('max 100000')
    (current.parent/'memory.max').write_text(str(4*GIB));(current.parent/'cpu.max').write_text('200000 100000')
    original=Path.read_text
    def read(path,*args,**kwargs):
        if path==Path('/proc/meminfo'):return 'MemTotal: 16777216 kB\n'
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',read)
    monkeypatch.setattr(budget,'cgroup_directory',lambda root:current)
    monkeypatch.setattr(budget.os,'cpu_count',lambda:16)
    monkeypatch.setattr(budget.os,'sched_getaffinity',lambda _:set(range(4)))
    result=budget.capacity(cgroup_root=root)
    assert result==Capacity(2,4*GIB,True,True)
    (current.parent/'cpu.max').write_text('max 100000')
    assert budget.capacity(cgroup_root=root).cpu_limited
    (current/'memory.max').unlink()
    with pytest.raises(BuildError,match='effective'):budget.capacity(cgroup_root=root)


def test_resource_cli_is_usable_by_shell_launcher(monkeypatch,tmp_path):
    import os,subprocess,sys
    env={**os.environ,'QUIRKBENCH_CPUS':'1','QUIRKBENCH_MEMORY_GIB':'1'}
    result=subprocess.run([sys.executable,'-m','quirkbench.resource_budget','preparation'],
        env=env,text=True,capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()==f'1 {GIB}'


def test_small_selected_disk_reserve_reaches_packaging_and_composition(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from quirkbench.build_pipeline import _tar_directory
    from quirkbench.compose import extract_payload,archive_rpms
    source=tmp_path/'source';(source/'usr/bin').mkdir(parents=True);(source/'usr/bin/program').write_bytes(b'payload')
    monkeypatch.setattr('shutil.disk_usage',lambda _:SimpleNamespace(free=256*1024))
    with pytest.raises(BuildError,match='reserve'):_tar_directory(source,tmp_path/'blocked.tar.xz',0)
    archive=tmp_path/'payload.tar.xz'
    _tar_directory(source,archive,0,reserve_bytes=64*1024)
    destination=tmp_path/'unpacked';destination.mkdir()
    extract_payload(archive,destination,userspace=True,reserve_bytes=64*1024)
    assert (destination/'usr/bin/program').read_bytes()==b'payload'
    rpms=tmp_path/'rpms';rpms.mkdir();(rpms/'package.rpm').write_bytes(b'package')
    archive_rpms(rpms,tmp_path/'rpms.tar',0,reserve_bytes=64*1024)
    assert (tmp_path/'rpms.tar').is_file()


@pytest.mark.parametrize('script_path',['environments/run-bounded-podman.sh','src/quirkbench/run-bounded-podman.sh'])
def test_shell_launcher_uses_shared_budget_with_bounded_engine_arguments(tmp_path,script_path):
    import os,subprocess,sys
    from pathlib import Path
    engine=tmp_path/'podman';engine.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n');engine.chmod(0o755)
    env={**os.environ,'PATH':str(tmp_path)+':'+os.environ['PATH'],'PYTHON':sys.executable,
         'QUIRKBENCH_CPUS':'1','QUIRKBENCH_MEMORY_GIB':'1'}
    script=Path(__file__).parents[1]/script_path
    if script_path.startswith('src/'):
        import shutil
        installed=tmp_path/'installed/lib/quirkbench'
        shutil.copytree(script.parent,installed,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        script=installed/script.name
        env.pop('PYTHONDONTWRITEBYTECODE',None)
        env.pop('PYTHONPATH',None)
    result=subprocess.run(['bash',str(script),'--workload=preparation','sha256:'+'1'*64,'true'],
        env=env,text=True,capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert '--cpus=1' in result.stdout and f'--memory={GIB}' in result.stdout
    assert '--pids-limit=4096' in result.stdout and '--timeout=86400' in result.stdout
    if script_path.startswith('src/'):assert not list(installed.rglob('__pycache__'))


def test_selected_reserve_allows_managed_capture_and_cache_on_small_disk(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from quirkbench.build import sha256_file
    from quirkbench.build_cache import BuildStageCache
    from quirkbench.job_worker import capture
    monkeypatch.setattr('shutil.disk_usage',lambda _:SimpleNamespace(free=256*1024))
    source=tmp_path/'repo';source.write_bytes(b'[fedora]\n')
    raw={'fedora_repo_file':str(source),'fedora_repo_sha256':sha256_file(source),
         'artifact_paths':{},'evidence_paths':{}}
    stage=tmp_path/'stage';stage.mkdir()
    record=capture('compose',raw,stage,lambda:None,lambda *args:None,reserve_bytes=64*1024)
    captured=stage/record['files']['fedora_repo_file']['path']
    assert captured.read_bytes()==source.read_bytes()
    cache=BuildStageCache(tmp_path/'cache',reserve_bytes=64*1024)
    cache.publish('recipe','source',{}, {'inputs':captured.parent},{'captured':True})
    restored=tmp_path/'restored'
    assert cache.load('recipe','source',{}, {'inputs':restored})=={'captured':True}
    assert (restored/captured.name).read_bytes()==source.read_bytes()
    with pytest.raises(BuildError,match='reserve'):
        BuildStageCache(tmp_path/'default-cache').publish('recipe','source',{}, {'inputs':captured.parent},{})
