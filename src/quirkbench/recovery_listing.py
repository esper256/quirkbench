"""Published recovery discovery: bounded read-only SQL and small CAS records."""
import hashlib
import json
from pathlib import Path
import stat

from .contracts import ContractError,sha256
from .build import BuildError
from .operations import operation_response
from .state_reader import bounded_items, safe_text
from .filesystem import read_file


def _size(root,value):
    path=Path(root)/'artifacts/objects'/sha256(value)
    if path.resolve()!=path: raise ContractError('published artifact is linked')
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode): raise ContractError('published artifact is not regular')
    return info.st_size


def list_images(reader,*,before=0,limit=20):
    if type(before) is not int or before<0 or type(limit) is not int or not 1<=limit<=100:
        raise ContractError('invalid recovery image cursor/limit')
    with reader.connection() as db:
        rows=[dict(row) for row in db.execute(
            "SELECT rowid AS cursor,id,state,updated,kind,final_output_digest FROM operations WHERE kind IN ('image_prepare','recovery_download') "
            'AND (?=0 OR rowid<?) ORDER BY rowid DESC LIMIT ?',(before,before,limit+1))]
        references={row['id']:[value[0] for value in db.execute(
            "SELECT digest FROM operation_refs WHERE operation=? AND role='output' LIMIT 17",(row['id'],))]
            for row in rows[:limit] if row['state']=='SUCCEEDED'}
    items=[]
    for row in rows[:limit]:
        item={key:value for key,value in row.items() if key not in ('kind','final_output_digest')}
        item.update(image_path=None,availability='not_published')
        refs=references.get(row['id'],[])
        if row['kind']=='recovery_download':
            if row['state']=='SUCCEEDED':
                item['availability']='unavailable'
                try:
                    final=sha256(row['final_output_digest'])
                    if final not in refs:raise ContractError('released recovery receipt is not retained')
                    from .released_recovery import load_acquisition
                    raw=read_file(reader.root,'artifacts/objects/'+final,limit=4096)
                    if hashlib.sha256(raw).hexdigest()!=final:raise ContractError('released recovery receipt digest differs')
                    receipt=load_acquisition(raw);assets=receipt['assets']
                    if not {asset['sha256'] for asset in assets.values()}<=set(refs):
                        raise ContractError('released recovery asset reference is missing')
                    from .controller_release import load_statement
                    statement_raw=read_file(reader.root,'artifacts/objects/'+receipt['statement_sha256'],limit=16384)
                    if hashlib.sha256(statement_raw).hexdigest()!=receipt['statement_sha256']:
                        raise ContractError('retained publisher statement differs')
                    statement=load_statement(statement_raw)
                    if (statement['schema_version']!=2 or any(statement[role+'_sha256']!=assets[role]['sha256']
                            for role in ('recovery_image','recovery_manifest','recovery_candidate'))):
                        raise ContractError('released recovery receipt differs from publisher assets')
                    item.update(image_sha256=assets['recovery_image']['sha256'],size_bytes=assets['recovery_image']['size_bytes'],
                        image_name='factory.img',qualification_status='unqualified',publisher_fingerprint=receipt['publisher_fingerprint'],
                        checksums_path=str(Path(reader.root)/'artifacts/objects'/receipt['statement_sha256']),
                        signature_path=str(Path(reader.root)/'artifacts/objects'/assets['release.sig']['sha256']),
                        verification='publisher authentication and compatibility verified at publication; listing does not rehash image or reverify current publisher trust')
                    if all(_size(reader.root,asset['sha256'])==asset['size_bytes'] for asset in assets.values()):
                        item.update(availability='retained',image_path=str(Path(reader.root)/'artifacts/objects'/assets['recovery_image']['sha256']))
                except (OSError,ValueError,BuildError):pass
            items.append(item)
            continue
        if row['state']=='SUCCEEDED':
            item['availability']='unavailable'
            # Rootfs-only legacy successes are not completed images. Identify the
            # published checksum statement, never a worker's unsigned candidate.
            for value in refs if len(refs)<=16 else []:
                try:
                    if _size(reader.root,value)>4096: continue
                    raw=read_file(reader.root,'artifacts/objects/'+value,limit=4096)
                    if hashlib.sha256(raw).hexdigest()!=value: continue
                    statement=json.loads(raw)
                    if not isinstance(statement,dict) or statement.get('record_type')!='recovery-checksum-statement': continue
                    from .recovery_distribution import load_recovery_checksum_statement
                    statement=load_recovery_checksum_statement(raw)
                    if not {statement[k] for k in ('image_sha256','image_manifest_sha256','image_checksum_sha256','release_candidate_sha256')}<=set(refs): continue
                    item.update(image_sha256=statement['image_sha256'],size_bytes=statement['image_size_bytes'],
                        image_name=statement['image_name'],qualification_status=statement['qualification_status'],
                        checksums_path=str(Path(reader.root)/'artifacts/objects'/value),
                        verification='verified at publication; image bytes not rehashed by listing')
                    if _size(reader.root,statement['image_sha256'])==statement['image_size_bytes']:
                        item.update(availability='retained',image_path=str(Path(reader.root)/'artifacts/objects'/statement['image_sha256']))
                    break
                except (OSError,ValueError,BuildError): continue
        items.append(item)
    items=bounded_items(items)
    return operation_response(data={'items':items,'next_cursor':items[-1]['cursor'] if items and len(rows)>len(items) else None})


def render_images(answer):
    data=answer['data'];lines=[]
    if not any(item['image_path'] for item in data['items']): lines.append('No retained, published recovery images in this page.')
    for item in data['items']:
        lines.append(f"{item['id']}  {item['state']}  {item['availability']}")
        if 'image_sha256' in item:
            lines.append(f"  {item['image_name']}  {item['size_bytes']/1024**3:.2f} GiB  {item['qualification_status']}")
            lines.append('  Image: '+(item['image_path'] or 'not available locally'))
            lines.append('  SHA256: '+item['image_sha256'])
            if 'signature_path' in item:
                lines.append('  Publisher statement: '+item['checksums_path'])
                lines.append('  Publisher signature: '+item['signature_path'])
                lines.append('  Publisher fingerprint: '+item['publisher_fingerprint'])
            else:lines.append('  Checksums: '+item['checksums_path'])
    if not data['items']: lines.append('No recovery image operation recorded. Input preparation does not create an image until it succeeds.')
    if data['next_cursor'] is not None: lines.append('Older entries: quirkbench recovery-images --before '+str(data['next_cursor']))
    if any(item['image_path'] for item in data['items']): lines.append('Listing does not rehash image bytes. Verify checksums/signature before flashing the confirmed external drive.')
    return safe_text('\n'.join(lines))
