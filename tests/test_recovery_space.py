"""Stock artifacts honor their chosen reserve while still rejecting disk exhaustion."""
from types import SimpleNamespace
import pytest
from quirkbench.build import BuildError
from quirkbench.build_pipeline import BoundedRunner, DISK_RESERVE
from quirkbench.image import _check_image_space, ImageError

GIB=1024**3


def test_stock_space_reserve_is_configurable_without_losing_low_space_checks(tmp_path, monkeypatch):
    available=[19*GIB]
    monkeypatch.setattr('shutil.disk_usage', lambda _: SimpleNamespace(free=available[0]))
    with pytest.raises(BuildError, match='reserve'):
        BoundedRunner(tmp_path).check_space()
    runner=BoundedRunner(tmp_path, reserve_bytes=2*GIB)
    runner.check_space()
    _check_image_space(tmp_path,4096,2*GIB)
    available[0]=9*GIB
    with pytest.raises(ImageError,match='needs'):
        _check_image_space(tmp_path,4096,2*GIB)
    available[0]=GIB
    with pytest.raises(BuildError,match='reserve'):
        runner.check_space()


@pytest.mark.parametrize('reserve',[-1,True,1.5])
def test_invalid_reserve_cannot_disable_build_protection(tmp_path,reserve):
    with pytest.raises(BuildError):BoundedRunner(tmp_path,reserve_bytes=reserve)
    with pytest.raises(ImageError):_check_image_space(tmp_path,4096,reserve)
