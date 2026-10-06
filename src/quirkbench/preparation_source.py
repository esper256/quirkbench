"""Current recovery artifact admission through the existing manifest/signature readers."""
from pathlib import Path
import subprocess

from .build import BuildError
from .contracts import canonical,digest
from .recovery_release import _load_json,_image_identity,load_release_candidate
from .recovery_distribution import (inspect_signed_recovery_bundle,
    _validate_factory_manifest,MAX_IMAGE_MANIFEST_BYTES,MAX_RECORD_BYTES,_read_bundle_file)
from .prepared_factory import validate as validate_factory


def inspect(image, *, public_key=None,fingerprint=None,unsigned_development=False,run=subprocess.run):
    """Explicit unsigned development never acquires publisher identity.

    The prepared plan is only an observation. Apply must call this again, compare
    its derived factory identity, then verify retained source bytes during copy.
    """
    image=Path(image).expanduser().resolve(strict=True)
    if not image.is_absolute() or image.is_symlink() or not image.is_file() or image.suffix!='.img':
        raise BuildError('select an absolute regular recovery .img artifact')
    if type(unsigned_development) is not bool:
        raise BuildError('unsigned development selection must be explicit')
    if unsigned_development:
        if public_key is not None or fingerprint is not None:
            raise BuildError('unsigned development input cannot also claim publisher trust')
        candidate_path=Path(str(image)+'.release-candidate.json')
        if candidate_path.exists() or candidate_path.is_symlink():
            raw=_read_bundle_file(candidate_path,MAX_RECORD_BYTES)
            candidate=load_release_candidate(raw)
        else:
            # The existing foreground builder returns its candidate in this
            # fixed output. It is not a production trust bundle or signature.
            result,_=_load_json(image.parent/'image-result.json',limit=MAX_IMAGE_MANIFEST_BYTES,
                                label='unsigned development image result')
            if not isinstance(result,dict) or result.get('signed') is not False:
                raise BuildError('unsigned development build result is missing or inconsistent')
            candidate=load_release_candidate(canonical(result.get('candidate')))
        checksum,size=_image_identity(image)
        if candidate['image_sha256']!=checksum or candidate['image_size_bytes']!=size:
            raise BuildError('unsigned development image differs from its retained candidate')
        authentication={'mode':'unsigned-development','trust_sha256':None,'fingerprint':None}
    else:
        if public_key is None or fingerprint is None:
            raise BuildError('independent publisher public key and full fingerprint required; unsigned development requires explicit selection')
        public_key=Path(public_key).expanduser().resolve(strict=True)
        key_raw=_read_bundle_file(public_key,65536)
        verified=inspect_signed_recovery_bundle(image,public_key,fingerprint,run=run)
        checksum=verified['image_sha256'];size=verified['image_size_bytes']
        if _read_bundle_file(public_key,65536)!=key_raw:
            raise BuildError('independent publisher key changed during preparation admission')
        authentication={'mode':'signed','trust_sha256':digest(key_raw),
                        'fingerprint':fingerprint.upper()}
        candidate_raw=_read_bundle_file(Path(str(image)+'.release-candidate.json'),MAX_RECORD_BYTES)
        if digest(candidate_raw)!=verified['release_candidate_sha256']:
            raise BuildError('selected candidate changed from signed publisher identity')
        candidate=load_release_candidate(candidate_raw)
    manifest,raw=_load_json(Path(str(image)+'.json'),limit=MAX_IMAGE_MANIFEST_BYTES,label='recovery manifest')
    _validate_factory_manifest(manifest,candidate)
    if digest(raw)!=candidate['image_manifest_sha256']:
        raise BuildError('selected recovery manifest differs from retained candidate')
    if not unsigned_development and digest(raw)!=verified['image_manifest_sha256']:
        raise BuildError('selected manifest changed from signed publisher identity')
    if manifest['schema_version']!=3:
        raise BuildError('this historical image requires a fresh controller-prepared recovery build; no in-place target migration')
    factory=manifest['commissioning'];validate_factory(factory)
    checksum_raw=_read_bundle_file(Path(str(image)+'.sha256'),256)
    if checksum_raw!=f'{checksum}  {image.name}\n'.encode():
        raise BuildError('selected recovery checksum sidecar differs')
    # Repeat independent byte verification at the return boundary, not mtime.
    if (_image_identity(image)!=(checksum,size)
            or _load_json(Path(str(image)+'.json'),limit=MAX_IMAGE_MANIFEST_BYTES,label='recovery manifest')[1]!=raw):
        raise BuildError('selected recovery artifact changed during preparation admission')
    if not unsigned_development and (
            _read_bundle_file(public_key,65536)!=key_raw
            or _read_bundle_file(Path(str(image)+'.release-candidate.json'),MAX_RECORD_BYTES)!=candidate_raw):
        raise BuildError('signed preparation trust or candidate changed at the return boundary')
    return {'source':{'sha256':checksum,'size_bytes':size,
                      'manifest_sha256':digest(raw),'authentication':authentication},'factory':factory}
