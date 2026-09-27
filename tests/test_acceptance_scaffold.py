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
    template.update(operator='Test fixture only',status='qualified',duration_seconds=31*3600,observed_events=template['required_events'])
    template['capabilities']={key:'supported' for key in template['capabilities']}
    template['evidence']=[{'path':'missing.bin','sha256':digest(b'not observed')}]
    report=tmp_path/'report.json';report.write_text(json.dumps(template))
    with pytest.raises(ValueError):checker.verify(report,'hardware-endurance')
