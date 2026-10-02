"""Issue #8 pytest-only integration probe; explicitly force its timer in cloud.

Use -o faulthandler_timeout=0.1 -o faulthandler_exit_on_timeout=false. The
standalone probe arms the stdlib timer itself; this test leaves that to pytest.
"""
import json
from faulthandler_probe import path_workload,runtime_identity


def test_pytest_delayed_dump_during_pathlib_work(tmp_path):
    root=tmp_path
    for index in range(8):
        root=root/('pathlib-diagnostic-level-'+str(index));root.mkdir()
    print(json.dumps(runtime_identity(),sort_keys=True),flush=True)
    assert path_workload(root,.5)>0
