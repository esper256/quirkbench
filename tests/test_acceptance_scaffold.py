import importlib.util
import json
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('check_report',ROOT/'acceptance/check_report.py')
checker=importlib.util.module_from_spec(spec);spec.loader.exec_module(checker)

@pytest.mark.parametrize('name,kind',[('hardware-endurance','hardware-endurance'),('patch-bundle','patch-bundle')])
def test_unperformed_hardware_and_patch_fixtures_cannot_pass(name,kind):
    with pytest.raises(ValueError):checker.verify(ROOT/'acceptance'/f'{name}.template.json',kind)

def test_endurance_cannot_be_replaced_by_accelerated_clock_report(tmp_path):
    from quirkbench.contracts import digest
    template=json.loads((ROOT/'acceptance/hardware-endurance.template.json').read_text())
    template.update(operator='Test fixture only',status='qualified',duration_seconds=7200, duration_objective={'seconds':7200,'rationale':'Fixture','declared_before_run':'test-plan'},observed_events=template['required_events'])
    template['capabilities']={key:'supported' for key in template['capabilities']}
    template['evidence']=[{'path':'missing.bin','sha256':digest(b'not observed')}]
    report=tmp_path/'report.json';report.write_text(json.dumps(template))
    with pytest.raises(ValueError):checker.verify(report,'hardware-endurance')


@pytest.mark.parametrize('seconds,actual,valid', [(60,60,True),(60,59,False),(0,60,False),(True,60,False),(60,True,False)])
def test_endurance_uses_declared_release_objective(tmp_path, seconds, actual, valid):
    from quirkbench.contracts import digest
    report=json.loads((ROOT/'acceptance/hardware-endurance.template.json').read_text())
    report.update(operator='Fixture',status='qualified',duration_seconds=actual,
                  duration_objective={'seconds':seconds,'rationale':'Fixture objective','declared_before_run':'release-plan-1'},
                  observed_events=report['required_events'])
    report['capabilities']={key:'supported' for key in report['capabilities']}
    (tmp_path/'evidence').write_bytes(b'fixture')
    report['evidence']=[{'path':'evidence','sha256':digest(b'fixture')}]
    path=tmp_path/'report.json';path.write_text(json.dumps(report))
    if valid:
        assert checker.verify(path,'hardware-endurance') == report
    else:
        with pytest.raises(ValueError,match='duration objective'):
            checker.verify(path,'hardware-endurance')
