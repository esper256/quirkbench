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
