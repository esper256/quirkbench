from pathlib import Path
import re
import subprocess

import pytest

from quirkbench.binding import BindingError, read_system_uuid, verify_binding
from quirkbench.image import grub_config

UUID = '01234567-89ab-cdef-0123-456789abcdef'
OTHER = '11234567-89ab-cdef-0123-456789abcdef'


def test_binding_reads_metadata_and_rejects_missing_or_changed_identity(tmp_path):
    path = tmp_path/'product_uuid'
    path.write_text(UUID.upper()+'\n')
    binding = {'schema_version': 1, 'system_uuid': UUID}
    assert verify_binding(binding, reader=lambda:read_system_uuid(path)) == UUID
    path.write_text(OTHER)
    with pytest.raises(BindingError, match='changed'):
        verify_binding(binding, reader=lambda:read_system_uuid(path))
    path.unlink()
    with pytest.raises(BindingError, match='unavailable'):
        read_system_uuid(path)


@pytest.mark.parametrize('value', [None, '', '00000000-0000-0000-0000-000000000000',
                                   'ffffffff-ffff-ffff-ffff-ffffffffffff', 'model-name', UUID+';reboot'])
def test_binding_never_substitutes_a_model_or_default_uuid(value):
    with pytest.raises(BindingError):
        verify_binding({'schema_version': 1, 'system_uuid': value}, reader=lambda:UUID)


@pytest.mark.parametrize('observed,expected,clear_ok,selected', [
    (UUID, UUID, True, '1'), (OTHER, UUID, True, '0'),
    ('', UUID, True, '0'), (UUID, '', True, '0'),
    ('0'*8+'-'+'0'*4+'-'+'0'*4+'-'+'0'*4+'-'+'0'*12,
     '0'*8+'-'+'0'*4+'-'+'0'*4+'-'+'0'*4+'-'+'0'*12, True, '0'),
    (UUID, UUID, False, '0'),
])
def test_generated_grub_selection_fails_to_recovery_before_kernel_load(tmp_path, observed, expected, clear_ok, selected):
    # Execute the generated selection conditions with stand-in GRUB primitives.
    # This checks behavior; actual GRUB/SMBIOS remains a separate release gate.
    for name in ('esp', 'state', 'data'):
        (tmp_path/name).mkdir()
    (tmp_path/'esp'/('quirkbench-'+UUID)).touch()
    (tmp_path/'state'/('quirkbench-'+UUID)).touch()
    fragments=tmp_path/'data/quirkbench/boot';fragments.mkdir(parents=True)
    (fragments/('a'*64+'.cfg')).touch()
    selection=grub_config(UUID).split("menuentry 'Quirkbench recovery'", 1)[0]
    def assignment(match):
        key, value=match.group(1),match.group(2)
        if key in ('esp','state','data'):
            value=str(tmp_path/key)
        return f'export {key}="{value}"'
    selection=re.sub(r'\bset ([a-z_]+)=(.*)', assignment, selection)
    prefix='''
serial() { :; }
terminal_input() { :; }
terminal_output() { :; }
halt() { exit 99; }
regexp() {
  if [[ "$1" == --set=1:boot_disk ]]; then export boot_disk=hd0; return 0; fi
  [[ "$2" =~ $1 ]]
}
smbios() { export current_target="$observed"; [[ -n "$observed" ]]; }
load_env() {
  export next_entry="$saved_next" candidate_id="$saved_candidate" target_uuid="$saved_target"
}
save_env() {
  if [[ "$clear_ok" != yes ]]; then return 1; fi
  saved_next="$next_entry"; saved_candidate="$candidate_id"; saved_target="$target_uuid"
}
root=hd0,gpt1
'''
    # Inputs are fixture UUIDs/booleans, never shell text from a device.
    prefix+=f"observed='{observed}'\nsaved_target='{expected}'\nclear_ok={'yes' if clear_ok else 'no'}\nsaved_next=candidate\nsaved_candidate={'a'*64}\n"
    result=subprocess.run(['bash','-c',prefix+selection+'printf "SELECTED=%s STATE=%s,%s,%s\\n" "$default" "$saved_next" "$saved_candidate" "$saved_target"'],
                          check=True,capture_output=True,text=True,timeout=5)
    assert f'SELECTED={selected}' in result.stdout
    if clear_ok:
        assert 'STATE=,,' in result.stdout
