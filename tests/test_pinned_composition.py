"""Offline composition cannot expand the pinned distribution or recipe set."""
import json
from pathlib import Path
import pytest
import jsonschema
from quirkbench import pinned_composition as pinned
from quirkbench.contracts import ContractError


def test_pinned_result_strict_schema():
    root=Path(__file__).resolve().parents[1]
    value=json.loads((root/'examples/pinned-composition-result.json').read_bytes())
    jsonschema.validate(value,json.loads((root/'schemas/pinned-composition-result.v1.schema.json').read_bytes()))
    assert pinned.validate_result(value)==value
    with pytest.raises(ContractError):pinned.validate_result({**value,'execution_authorized':True})


def test_pinned_archive_exact_bytes_and_no_special_members(tmp_path):
    import io,tarfile
    from quirkbench.store import ArtifactStore
    from quirkbench.contracts import digest
    store=ArtifactStore(tmp_path/'artifacts',reserve_bytes=0)
    raw=b'pinned RPM bytes';identity=store.put(raw).sha256
    packages=[{'name':'rpm','nevra':'rpm-0:1-1.fc44.x86_64','sha256':identity}]
    path=tmp_path/'rpms.tar';pinned.pack(store,packages,path,lambda:None)
    pinned.package_archive(path,packages,destination=tmp_path/'extracted')
    assert (tmp_path/'extracted'/(identity+'.rpm')).read_bytes()==raw
    wrong=[{**packages[0],'sha256':'f'*64}]
    with pytest.raises(ContractError):pinned.package_archive(path,wrong)
    malicious=tmp_path/'malicious.tar'
    with tarfile.open(malicious,'w') as archive:
        member=tarfile.TarInfo(identity+'.rpm');member.type=tarfile.SYMTYPE;member.linkname='/private/key';archive.addfile(member)
    with pytest.raises(ContractError):pinned.package_archive(malicious,packages)


def test_archive_replacement_between_bounds_and_parser_is_rejected(tmp_path,monkeypatch):
    from quirkbench.store import ArtifactStore
    import tarfile
    from quirkbench.contracts import Conflict
    store=ArtifactStore(tmp_path/'artifacts',reserve_bytes=0)
    identity=store.put(b'original').sha256
    packages=[{'name':'rpm','nevra':'rpm-0:1-1.fc44.x86_64','sha256':identity}]
    path=tmp_path/'rpms.tar';pinned.pack(store,packages,path,lambda:None)
    original=tarfile.open
    def replace(*args,**kwargs):
        replacement=tmp_path/'replacement';replacement.write_bytes(b'\0'*10240);replacement.replace(path)
        return original(*args,**kwargs)
    monkeypatch.setattr(tarfile,'open',replace)
    with pytest.raises(Conflict,match='archive changed'):
        pinned.package_archive(path,packages,destination=tmp_path/'out')
    assert not list((tmp_path/'out').glob('*.rpm'))


def test_lock_requires_exact_package_version_and_full_rpm_digest():
    from quirkbench.contracts import Conflict
    packages=[{'name':'rpm','nevra':'rpm-0:1-1.fc44.x86_64','sha256':'a'*64}]
    value={'packages':{'rpm':{'evra':'1-1.fc44.x86_64','digest':'sha256:'+'a'*64}}}
    assert pinned.validate_lock(value,packages)==value
    with pytest.raises(ContractError):pinned.validate_lock({'packages':{}},packages)
    with pytest.raises(ContractError):pinned.validate_lock({'packages':{'rpm':{'evra':'2-1.fc44.x86_64','digest':'sha256:'+'a'*64}}},packages)
    with pytest.raises(ContractError):pinned.validate_lock({'packages':{'rpm':{'evra':'1-1.fc44.x86_64','digest':'sha256:'+'b'*64}}},packages)


def test_archive_truncation_between_passes_and_second_pass_completeness(tmp_path,monkeypatch):
    from quirkbench.store import ArtifactStore
    from quirkbench.contracts import Conflict
    import tarfile
    store=ArtifactStore(tmp_path/'artifacts',reserve_bytes=0)
    packages=[{'name':name,'nevra':name+'-0:1-1.fc44.x86_64','sha256':store.put(name.encode()).sha256} for name in ('rpm','systemd')]
    path=tmp_path/'rpms.tar';pinned.pack(store,packages,path,lambda:None)
    original=tarfile.open
    def truncate(*args,**kw):
        with path.open('r+b') as file:file.truncate(1024)
        return original(*args,**kw)
    with monkeypatch.context() as patch:
        patch.setattr(tarfile,'open',truncate)
        with pytest.raises(Conflict,match='archive changed'):pinned.package_archive(path,packages)
    path.unlink();pinned.pack(store,packages,path,lambda:None)
    class EarlyEnd:
        def __init__(self,archive):self.archive=archive
        def __enter__(self):return self
        def __exit__(self,*a):self.archive.close()
        def __iter__(self):return iter([next(iter(self.archive))])
        def extractfile(self,member):return self.archive.extractfile(member)
    monkeypatch.setattr(tarfile,'open',lambda *a,**kw:EarlyEnd(original(*a,**kw)))
    with pytest.raises(ContractError,match='became incomplete'):pinned.package_archive(path,packages)


def test_manual_job_cannot_opt_into_joined_publication_proof():
    from quirkbench.job_operations import manifest
    with pytest.raises(ContractError,match='versioned investigation inputs'):
        manifest('compose',{'pinned_baseline':{'untrusted':'input'}})
