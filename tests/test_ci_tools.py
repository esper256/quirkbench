"""Selector and evidence primitives: no full-suite collection or native services."""
import json
from pathlib import Path
import subprocess
import pytest
from ci.select import ROOT, SUITES, changed_paths, select
from ci.evidence import FILE_LIMIT, ATTACHMENT_LIMIT, capture_stage, redact


@pytest.mark.parametrize('paths,expected', [
    (['src/quirkbench/distribution_source_worker.py'], {'source-workers'}),
    (['src/quirkbench/endpoint_probe.py'], {'endpoint-control', 'endpoint-retarget'}),
    (['tests/test_retarget_evidence.py'], {'endpoint-retarget', 'evidence'}),
    (['src/quirkbench/recovery_initramfs_audit.py'], {'release-preparation'}),
    (['tests/conftest.py'], set(SUITES)),
    (['schemas/operation.schema.json'], set(SUITES)),
    (['src/quirkbench/new_shared_module.py'], set(SUITES)),
    (['README.md', 'docs/testing-policy.md'], set()),
])
def test_selection_explains_changes(paths, expected):
    value = select(paths)
    assert set(value['selected']) == expected
    assert set(value['unselected']) == set(SUITES) - expected
    assert all(value['reasons'][name] for name in expected)
    assert len(value['tests']) == len(set(value['tests']))


def test_manual_names_and_fallback():
    assert select([], ['evidence'])['reasons'] == {'evidence': ['explicit request']}
    assert select(['unknown'])['unmapped'] == ['unknown']
    with pytest.raises(ValueError, match='Unknown suites'):
        select([], ['evidence; touch /tmp/unsafe'])
    for suite in SUITES.values():
        assert all((ROOT / test.split('::')[0]).is_file() for test in suite['tests'])


def test_recovery_boot_change_selects_direct_and_joined_regressions():
    value=select(['src/quirkbench/recovery_initramfs_audit.py',
                  'src/quirkbench/recovery_storage.py', 'tests/test_stock_recovery_flow.py'])
    assert not value['unmapped']
    assert set(value['selected'])=={'filesystem','release-preparation'}
    assert {'tests/test_recovery_storage.py','tests/test_recovery_initramfs_audit.py',
            'tests/test_recovery_image_plan.py','tests/test_stock_recovery_flow.py'}<=set(value['tests'])


def test_actual_merge_base_includes_renames_deletions_not_base_changes(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(tmp_path), *args], text=True).strip()
    git('init', '-q'); git('config', 'user.name', 'fixture'); git('config', 'user.email', 'fixture@example.invalid')
    (tmp_path / 'old source.py').write_text('old')
    (tmp_path / 'deleted.py').write_text('deleted')
    git('add', '.'); git('commit', '-qm', 'base')
    base = git('rev-parse', 'HEAD')
    git('checkout', '-qb', 'topic')
    git('mv', 'old source.py', 'new source.py'); git('rm', 'deleted.py'); git('commit', '-qm', 'topic')
    head = git('rev-parse', 'HEAD')
    git('checkout', '-qb', 'base-advance', base)
    (tmp_path / 'base-only.py').write_text('base only')
    git('add', '.'); git('commit', '-qm', 'base advance')
    monkeypatch.chdir(tmp_path)
    paths, merge_base = changed_paths('base-advance', head)
    assert merge_base == base
    assert set(paths) == {'old source.py', 'new source.py', 'deleted.py'}


def test_allowlist_redaction_links_partial_and_budget(tmp_path):
    bundle = tmp_path / 'bundle'; bundle.mkdir()
    stage = tmp_path / 'stage'; diagnostics = stage / 'diagnostics'; diagnostics.mkdir(parents=True)
    target = diagnostics / 'stage-result.json'
    target.write_text(json.dumps({'state': 'FAILED', 'error': 'worker cause token=SECRET',
                                 'result': {'private_key': 'NEVER COPY'}, 'credentials': 'NEVER COPY'}))
    (diagnostics / 'key.pem').write_text('DO NOT COPY')
    result = capture_stage(bundle, stage)
    assert result['stage-result']['error'] == 'worker cause token=[REDACTED]'
    assert 'result' not in result['stage-result']
    target.unlink(); target.symlink_to(diagnostics / 'key.pem')
    assert capture_stage(bundle, stage)['status'] == 'unavailable'
    target.unlink(); target.write_text('x' * (FILE_LIMIT + 1))
    assert capture_stage(bundle, stage)['status'] == 'unavailable'
    target.write_text('{partial')
    assert capture_stage(bundle, stage)['status'] == 'unavailable'
    target.unlink()
    assert capture_stage(bundle, stage)['status'] == 'missing'
    for _ in range(150):
        capture_stage(bundle, stage)
    files = list((bundle / 'workers').iterdir())
    assert len(files) <= 128 and sum(p.stat().st_size for p in files) <= ATTACHMENT_LIMIT
    assert (bundle / 'workers-truncated.txt').exists()
    assert 'NEVER COPY' not in ''.join(p.read_text() for p in files)


def test_redaction_across_lines_and_truncated_private_keys():
    text = '-----BEGIN PRIVATE KEY-----\nsecret bytes\n-----END PRIVATE KEY-----\nAuthorization: Bearer ABC\npassword="long secret"\nhttps://user:password@host'
    result = redact(text)
    assert all(s not in result for s in ['secret bytes', 'ABC', 'long secret', 'user:password'])
    assert 'unfinished' not in redact('-----BEGIN RSA PRIVATE KEY-----\nunfinished')
