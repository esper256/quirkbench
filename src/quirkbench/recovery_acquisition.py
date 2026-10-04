"""Reviewed Fedora acquisition adapter and immutable repository/trust inputs."""
from pathlib import Path
import json
import re

from .build import BuildError
from .contracts import canonical, digest
from .platform_adapters import selected_adapter
from .product_contracts import _pairs
from .recovery_rootfs import validate_snapshot
from .store import atomic_write

MAX_SPEC = 2 * 1024**2
FIELDS = {'schema_version','candidate_id','platform_adapter_id','fedora_release',
          'kernel_release','rpm_key_fingerprint','packages','repositories'}


def legacy_candidate():
    """Readable historical selection; new CLI acquisition requires an explicit spec."""
    path = Path(__file__).with_name('profiles')/'legacy-stock-acquisition.v1.json'
    return json.loads(path.read_bytes())



def stock_candidate_spec(candidate, repository, repository_ids):
    """Select reviewed pairing inputs; bind explicitly supplied repository bytes."""
    if candidate != 'fedora44-pairing-v1':
        raise BuildError('unknown stock acquisition candidate')
    from .filesystem import read_file
    path = Path(repository).expanduser().absolute()
    content = read_file(path.parent.resolve(strict=True), path.name, limit=65536).decode()
    profile = Path(__file__).with_name('profiles')/'stock-fedora44-pairing-rpm-candidate.v1.json'
    snapshot = validate_snapshot(json.loads(profile.read_bytes()))
    return load_spec(canonical({
        'schema_version':1, 'candidate_id':candidate,
        'platform_adapter_id':'x86_64-uefi-usb-v1', 'fedora_release':'44',
        'kernel_release':'7.2.7-200.fc44.x86_64',
        'rpm_key_fingerprint':'36F612DCF27F7D1A48A835E4DBFCF71C6D9F90A6',
        'packages':snapshot['packages'],
        'repositories':[{'name':'selected.repo','content':content,
                         'sha256':digest(content.encode()),'ids':list(repository_ids)}]}))

def load_spec(raw):
    if len(raw) > MAX_SPEC: raise BuildError('acquisition specification exceeds budget')
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
        if not isinstance(value,dict) or set(value) != FIELDS or type(value['schema_version']) is not int or value['schema_version'] != 1:
            raise ValueError('unsupported acquisition specification')
        from .contracts import identifier
        identifier(value['candidate_id'])
        adapter = selected_adapter(value['platform_adapter_id'])
        if not re.fullmatch(r'[0-9]{2}',value['fedora_release']): raise ValueError('invalid release')
        kernel = value['kernel_release']
        if not re.fullmatch(r'[A-Za-z0-9._+-]{1,128}',kernel) or not kernel.endswith('.fc'+value['fedora_release']+'.'+adapter.target_architecture):
            raise ValueError('kernel differs from selected platform/release')
        if not re.fullmatch(r'[A-F0-9]{40,64}',value['rpm_key_fingerprint']): raise ValueError('invalid RPM trust identity')
        validate_snapshot({'schema_version':1,'packages':value['packages']})
        repositories = value['repositories']
        if not isinstance(repositories,list) or not 1 <= len(repositories) <= 16: raise ValueError('repositories required')
        seen = set(); ids = set()
        for repository in repositories:
            if set(repository) != {'name','content','sha256','ids'}: raise ValueError('invalid repository record')
            name = repository['name']
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}\.repo',name) or name in seen: raise ValueError('invalid repository filename')
            seen.add(name)
            if not isinstance(repository['content'],str) or len(repository['content']) > 65536 or digest(repository['content'].encode()) != repository['sha256']:
                raise ValueError('repository bytes differ from pinned identity')
            if not isinstance(repository['ids'],list) or not repository['ids']: raise ValueError('repository IDs required')
            for repo_id in repository['ids']:
                if not re.fullmatch('[A-Za-z0-9_-]{1,64}',repo_id) or repo_id in ids: raise ValueError('invalid repository ID')
                if '['+repo_id+']' not in repository['content']: raise ValueError('repository ID missing from pinned bytes')
                ids.add(repo_id)
        return value
    except (KeyError,TypeError,ValueError) as exc:
        raise BuildError('invalid acquisition specification: '+str(exc)) from exc


def stage_spec(spec, generation):
    spec = load_spec(canonical(spec))
    generation = Path(generation)
    repositories = generation/'repositories'
    repositories.mkdir(mode=0o700)
    for record in spec['repositories']:
        atomic_write(repositories/record['name'],record['content'].encode())
    atomic_write(generation/'acquisition-spec.v1.json',canonical(spec))
    return spec


def acquisition_command(destination, spec):
    spec = load_spec(canonical(spec))
    destination = Path(destination)
    if not destination.is_absolute() or destination.resolve() != destination:
        raise BuildError('acquisition destination must be canonical')
    adapter = selected_adapter(spec['platform_adapter_id'])
    repositories = destination.parent/'repositories'
    if repositories.is_symlink() or repositories.resolve()!=repositories or not repositories.is_dir():
        raise BuildError('acquisition repositories must be a canonical directory')
    if {p.name for p in repositories.iterdir()} != {r['name'] for r in spec['repositories']}:
        raise BuildError('repository directory differs from pinned specification')
    for record in spec['repositories']:
        path = repositories/record['name']
        if path.is_symlink() or path.resolve() != path or not path.is_file() or digest(path.read_bytes()) != record['sha256']:
            raise BuildError('staged repository differs from acquisition specification')
    return ('dnf5','--installroot='+str(destination.parent/'dnf-root'),
            '--setopt=reposdir='+str(repositories),'--setopt=use_host_config=False',
            '--releasever='+spec['fedora_release'],'--setopt=install_weak_deps=False',
            '--setopt=disable_excludes=all','--disablerepo=*',
            *('--enablerepo='+repo_id for repo in spec['repositories'] for repo_id in repo['ids']),
            'download','--resolve','--alldeps','--arch='+adapter.target_architecture,'--arch=noarch',
            '--destdir='+str(destination),*(p['nevra'] for p in spec['packages']),
            *(name+'-'+spec['kernel_release'] for name in ('kernel-core','kernel-modules-core','kernel-modules','kernel-modules-extra')))


def bound_spec(root, owner, generation):
    """Versioned owner identity binds new plans; missing bytes cannot select legacy."""
    from .filesystem import read_file
    match = re.fullmatch(r'storage:acquisition-v1:([0-9a-f]{64}):([0-9a-f]{32})', owner)
    path = Path(generation)/'acquisition-spec.v1.json'
    if match is None:
        if path.exists(): raise BuildError('legacy acquisition owner has unbound specification')
        return None
    raw = read_file(generation, path.name, limit=MAX_SPEC)
    spec = load_spec(raw)
    expected = match.group(1)
    if digest(canonical(spec)) != expected:
        raise BuildError('acquisition specification differs from planned identity')
    retained = read_file(root, 'artifacts/objects/'+expected, limit=MAX_SPEC)
    if retained != canonical(spec): raise BuildError('retained acquisition specification differs')
    return spec


def completed_spec(root, directory):
    """Find the completed registered owner, then verify its original input identity."""
    from .retention import connection
    with connection(root) as db:
        for row in db.execute("SELECT * FROM storage_groups WHERE kind='input' AND state='WAITING'"):
            if json.loads(row['paths']) == [str(Path(directory).parent)] and row['stop_proof'] and json.loads(row['stop_proof']).get('download_complete'):
                return bound_spec(root,row['owner'],Path(directory).parent)
    raise BuildError('completed acquisition owner unavailable')


def freeze_legacy_spec(repository_dir=Path('/etc/yum.repos.d')):
    """Compatibility entrypoint freezes observed repository bytes before planning.

    Legacy command syntax selects the named historical candidate, never a new
    package revision. Missing repository inputs require an explicit --spec.
    """
    import configparser
    legacy = legacy_candidate()
    from .recovery_inputs import recorded_packages
    selected = {'schema_version':1,'candidate_id':'legacy-fedora44-stock',
                'platform_adapter_id':'x86_64-uefi-usb-v1',
                'fedora_release':legacy['fedora_release'],'kernel_release':legacy['kernel_release'],
                'rpm_key_fingerprint':legacy['rpm_key_fingerprint'],
                'packages':recorded_packages(),'repositories':[]}
    found = set()
    for path in sorted(Path(repository_dir).glob('*.repo')):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536: continue
        raw = path.read_bytes();content = raw.decode()
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(content)
        ids = sorted(set(parser.sections()) & {'fedora','updates'})
        if ids:
            selected['repositories'].append({'name':path.name,'content':content,'sha256':digest(raw),'ids':ids})
            found.update(ids)
    if found != {'fedora','updates'}:
        raise BuildError('historical candidate repositories unavailable; provide --spec with pinned repository and trust identities')
    return load_spec(canonical(selected))
