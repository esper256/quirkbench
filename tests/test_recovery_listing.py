"""Read-only released-image discovery; small CAS fixtures, no image build."""
import json
from pathlib import Path

import pytest

from quirkbench import recovery_download as acquisition
from quirkbench.contracts import ContractError,canonical,digest
from quirkbench.controller import Controller
from quirkbench.job_coordinator import JobCoordinator
from quirkbench.recovery_listing import list_images,render_images
from quirkbench.released_recovery import load_acquisition
from quirkbench.state_reader import StateReader
from test_recovery_download import signed_factory,admitted,execute,fixture,asset_set,inputs,fake_gpg,Workers,BOOT

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture
def published(signed_factory,tmp_path,monkeypatch):
    c=Controller(tmp_path/'state',reserve_bytes=0,boot_id_reader=lambda:BOOT)
    native=acquisition.consume
    monkeypatch.setattr(acquisition,'consume',lambda coordinator,claim,intent,data:native(coordinator,claim,intent,data,run=fake_gpg))
    with c.lifecycle() as owner:
        row=admitted(c,signed_factory);services=Workers();coordinator=JobCoordinator(owner,services)
        claim=coordinator.tick();assert execute(c,claim,signed_factory[-1],monkeypatch)==0
        services.done=True;assert coordinator.tick()['state']=='SUCCEEDED'
    with c.transaction() as db:
        final=db.execute('SELECT final_output_digest FROM operations WHERE id=?',(row['id'],)).fetchone()[0]
    return c,row,final,load_acquisition(c.store.get(final))


def test_listing_exposes_retained_publisher_evidence_without_writes_or_authority(published,monkeypatch):
    c,row,final,receipt=published
    before=(c.root/'controller.sqlite').read_bytes()
    files={path.relative_to(c.root) for path in c.root.rglob('*')}
    monkeypatch.setattr(Controller,'__init__',lambda *a,**kw:pytest.fail('listing must not initialize controller'))
    answer=list_images(StateReader(c.root));item,=answer['data']['items']
    assert item['id']==row['id'] and item['availability']=='retained'
    assert Path(item['image_path']).read_bytes()==b'synthetic image placeholder'
    assert digest(Path(item['checksums_path']).read_bytes())==receipt['statement_sha256']
    assert digest(Path(item['signature_path']).read_bytes())==receipt['assets']['release.sig']['sha256']
    assert item['qualification_status']=='unqualified'
    assert 'does not rehash image or reverify current publisher trust' in item['verification']
    assert 'Publisher fingerprint: '+receipt['publisher_fingerprint'] in render_images(answer)
    assert (c.root/'controller.sqlite').read_bytes()==before
    assert {path.relative_to(c.root) for path in c.root.rglob('*')}==files


@pytest.mark.parametrize('change',['missing','image-size','image-link','receipt-bytes','statement-bytes','asset-ref','final-ref','statement-linkage'])
def test_missing_or_inconsistent_published_objects_are_unavailable(published,change):
    c,row,final,receipt=published;assets=receipt['assets']
    image=c.root/'artifacts/objects'/assets['recovery_image']['sha256']
    if change=='missing':(c.root/'artifacts/objects'/assets['release.sig']['sha256']).unlink()
    elif change=='image-size':image.write_bytes(b'short')
    elif change=='image-link':
        other=c.root/'image';image.rename(other);image.symlink_to(other)
    elif change=='receipt-bytes':(c.root/'artifacts/objects'/final).write_bytes(b'{}')
    elif change=='statement-bytes':(c.root/'artifacts/objects'/receipt['statement_sha256']).write_bytes(b'{}')
    elif change in ('asset-ref','final-ref'):
        with c.transaction() as db:
            db.execute("DELETE FROM operation_refs WHERE operation=? AND role='output' AND digest=?",
                (row['id'],final if change=='final-ref' else assets['release.sig']['sha256']))
    else:
        statement=json.loads(c.store.get(receipt['statement_sha256']));statement['recovery_image_sha256']='f'*64
        raw=canonical(statement)+b'\n';new=c.store.put(raw).sha256
        receipt['statement_sha256']=new;receipt['assets']['release.json']={'sha256':new,'size_bytes':len(raw)}
        changed=c.store.put(canonical(receipt)).sha256
        with c.transaction() as db:
            db.execute('UPDATE operations SET final_output_digest=? WHERE id=?',(changed,row['id']))
            for value in (new,changed):
                db.execute('INSERT INTO refs(owner,digest) VALUES(?,?)',(row['id'],value))
                db.execute("INSERT INTO operation_refs(operation,role,digest) VALUES(?,'output',?)",(row['id'],value))
    item,=list_images(StateReader(c.root))['data']['items']
    assert item['availability']=='unavailable' and item['image_path'] is None


def test_listing_explicitly_does_not_hash_large_image_bytes(published):
    c,row,final,receipt=published
    image=c.root/'artifacts/objects'/receipt['assets']['recovery_image']['sha256']
    image.write_bytes(b'x'*image.stat().st_size)
    item,=list_images(StateReader(c.root))['data']['items']
    assert item['availability']=='retained' and 'does not rehash image' in item['verification']


def test_cursor_mixes_legacy_signed_images_and_pending_acquisition(published,signed_factory):
    c,row,final,receipt=published
    old=c.admit_operation('old-image','image_prepare',{'fixture':True})
    assets=receipt['assets'];image=assets['recovery_image']
    checksum=c.store.put(f"{image['sha256']}  old.img\n".encode()).sha256
    statement={'schema_version':1,'record_type':'recovery-checksum-statement','image_name':'old.img',
        'qualification_status':'unqualified','image_size_bytes':image['size_bytes'],'image_sha256':image['sha256'],
        'image_manifest_sha256':assets['recovery_manifest']['sha256'],'image_checksum_sha256':checksum,
        'release_candidate_sha256':assets['recovery_candidate']['sha256']}
    final_old=c.store.put(canonical(statement)+b'\n').sha256
    with c.transaction() as db:
        db.execute("UPDATE operations SET state='SUCCEEDED',final_output_digest=? WHERE id=?",(final_old,old['id']))
        for value in (image['sha256'],statement['image_manifest_sha256'],checksum,statement['release_candidate_sha256'],final_old):
            db.execute('INSERT INTO refs(owner,digest) VALUES(?,?)',(old['id'],value))
            db.execute("INSERT INTO operation_refs(operation,role,digest) VALUES(?,'output',?)",(old['id'],value))
    pending=admitted(c,signed_factory,'next')
    reader=StateReader(c.root);first=list_images(reader,limit=1)['data'];item,=first['items']
    assert item['id']==pending['id'] and item['availability']=='not_published'
    second=list_images(reader,limit=1,before=first['next_cursor'])['data'];item,=second['items']
    assert item['id']==old['id'] and item['availability']=='retained' and 'signature_path' not in item
    assert 'Checksums: ' in render_images({'data':second})
    third=list_images(reader,limit=1,before=second['next_cursor'])['data']
    assert third['items'][0]['id']==row['id'] and third['next_cursor'] is None


def test_no_state_initialization_and_cursor_bounds(tmp_path):
    absent=tmp_path/'absent'
    with pytest.raises(ContractError):list_images(StateReader(absent))
    assert not absent.exists()
    for options in ({'limit':True},{'limit':101},{'before':-1},{'before':True}):
        with pytest.raises(ContractError):list_images(StateReader(absent),**options)


def test_published_schema_and_cli_versions_match_strict_reader():
    from jsonschema import Draft202012Validator
    from quirkbench.cli import parser
    value=json.loads((ROOT/'examples/released-recovery-acquisition.json').read_bytes())
    schema=json.loads((ROOT/'schemas/released-recovery-acquisition.v1.schema.json').read_bytes())
    Draft202012Validator(schema).validate(value);assert load_acquisition(canonical(value))==value
    for version in (1,2):
        for case in json.loads((ROOT/f'examples/recovery-cli.v{version}.json').read_bytes())['cases']:
            actual=vars(parser().parse_args(case['argv']))
            assert {key:str(actual[key]) if isinstance(actual[key],Path) else actual[key] for key in case['expected']}==case['expected']


@pytest.mark.parametrize('change',['version','extra','role','size','flag','statement','canonical','duplicate'])
def test_acquisition_reader_rejects_ambiguous_or_authority_bearing_records(change):
    value=json.loads((ROOT/'examples/released-recovery-acquisition.json').read_bytes())
    if change=='version':value['schema_version']=True
    elif change=='extra':value['boot_authorized']=True
    elif change=='role':value['assets'].pop('release.sig')
    elif change=='size':value['assets']['recovery_image']['size_bytes']=True
    elif change=='flag':value['qualified']=True
    elif change=='statement':value['statement_sha256']='f'*64
    raw=canonical(value)
    if change=='canonical':raw+=b'\n'
    elif change=='duplicate':raw=raw.replace(b'"schema_version":1',b'"schema_version":1,"schema_version":1')
    with pytest.raises(ContractError):load_acquisition(raw)
