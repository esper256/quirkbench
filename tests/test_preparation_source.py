"""Real byte/manifest admission with explicit test-only signing-tool responses."""
import json
from pathlib import Path

import pytest

from quirkbench import preparation_source as source,prepared_factory,image as assembly
from quirkbench.build import BuildError,sha256_file
from quirkbench.contracts import canonical,digest
from quirkbench.recovery_distribution import recovery_checksum_statement
from test_recovery_distribution import inputs,fake_public_gpg,FINGERPRINT,trusted_key


@pytest.fixture
def artifact(tmp_path,monkeypatch):
    candidate,path,_=inputs(tmp_path,monkeypatch)
    # Sparse parser fixture at minimum supported fixed-ESP geometry. No kernel,
    # filesystem/image build or physical readiness is asserted by this fixture.
    size=308*1024**2
    with path.open('wb') as stream:
        stream.write(b'nonbootable preparation admission fixture');stream.truncate(size)
    checksum=sha256_file(path)
    candidate.update(image_sha256=checksum,image_size_bytes=size,
        rootfs_file_bytes=4096,rootfs_required_bytes=1024**2)
    candidate['layout'].update(root_mib=1,factory_size_mib=308)
    manifest=json.loads(Path(str(path)+'.json').read_bytes())
    old=manifest['commissioning']
    parts=assembly.partition_layout(308,1,controller_prepared=True)
    for part,previous in zip(parts,manifest['partitions']):part['partuuid']=previous['partuuid']
    factory=prepared_factory.record(old['disk_guid'],old['partition_uuids'],parts)
    manifest.update(schema_version=3,layout_version=3,commissioning=factory,
                    partitions=parts,image_sha256=checksum,size_bytes=size)
    raw=canonical(manifest)
    candidate['image_manifest_sha256']=digest(raw)
    Path(str(path)+'.json').write_bytes(raw)
    Path(str(path)+'.sha256').write_text(f'{checksum}  {path.name}\n')
    Path(str(path)+'.release-candidate.json').write_bytes(canonical(candidate))
    # Earlier metadata fixture mocks sizing. Here the actual byte reader is used
    # at every signed/unsigned boundary, including the source return fence.
    from quirkbench import recovery_distribution as distribution
    monkeypatch.setattr(distribution,'_image_identity',source._image_identity)
    statement=recovery_checksum_statement(candidate,path)
    Path(str(path)+'.checksums.json').write_bytes(statement)
    Path(str(path)+'.checksums.json.sig').write_bytes(digest(statement).encode())
    return path,manifest,candidate,trusted_key(tmp_path)


def test_unsigned_explicit_real_byte_admission_preserves_zero_library_and_factory(artifact):
    path,manifest,candidate,key=artifact
    with pytest.raises(BuildError,match='independent publisher'):
        source.inspect(path)
    checked=source.inspect(path,unsigned_development=True)
    assert checked['factory']==manifest['commissioning']
    assert checked['source']['sha256']==candidate['image_sha256']
    assert checked['source']['authentication']=={'mode':'unsigned-development','trust_sha256':None,'fingerprint':None}
    with pytest.raises(BuildError,match='cannot also claim'):
        source.inspect(path,public_key=key,fingerprint=FINGERPRINT,unsigned_development=True)
    with path.open('r+b') as stream:stream.write(b'changed')
    with pytest.raises(BuildError,match='differs from its retained candidate'):
        source.inspect(path,unsigned_development=True)


def test_signed_admission_calls_existing_verifier_and_binds_exact_test_trust(artifact):
    path,manifest,candidate,key=artifact
    checked=source.inspect(path,public_key=key,fingerprint=FINGERPRINT,run=fake_public_gpg)
    assert checked['source']['authentication']=={'mode':'signed','trust_sha256':digest(key.read_bytes()),'fingerprint':FINGERPRINT}
    assert checked['factory']==manifest['commissioning']


@pytest.mark.parametrize('fault',['candidate','key'])
def test_substitution_after_signature_verification_cannot_change_admission(artifact,monkeypatch,fault):
    path,manifest,candidate,key=artifact
    original=source.inspect_signed_recovery_bundle
    def mutate(*a,**kw):
        verified=original(*a,**kw)
        if fault=='candidate':
            candidate['layout']['log_budget_mib']+=1
            Path(str(path)+'.release-candidate.json').write_bytes(canonical(candidate))
        else:key.write_bytes(b'other test publisher bytes')
        return verified
    monkeypatch.setattr(source,'inspect_signed_recovery_bundle',mutate)
    with pytest.raises(BuildError,match='changed'):
        source.inspect(path,public_key=key,fingerprint=FINGERPRINT,run=fake_public_gpg)


def test_unsigned_foreground_result_is_the_existing_handoff_not_new_trust(artifact):
    path,manifest,candidate,key=artifact
    Path(str(path)+'.release-candidate.json').unlink()
    (path.parent/'image-result.json').write_bytes(canonical({'candidate':candidate,'signed':False}))
    assert source.inspect(path,unsigned_development=True)['factory']==manifest['commissioning']
