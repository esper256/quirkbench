"""Historical recovery evidence remains readable, never executable."""
from pathlib import Path
import pytest
from quirkbench.build import BuildError
from quirkbench.build_pipeline import ResourceLimits
from quirkbench.contracts import canonical
from quirkbench.recovery_recipe import load_recipe
from quirkbench.recovery_release import load_release_candidate
from quirkbench import recovery_synthesis as synthesis

HISTORICAL=Path(__file__).parent/'fixtures/historical'


@pytest.mark.parametrize('kind,reader',[('recipe',load_recipe),('release',load_release_candidate)])
def test_historical_canonical_records_keep_their_original_identity(kind,reader):
    raw=(HISTORICAL/f'recovery-{kind}-v1.json').read_bytes()
    assert canonical(reader(raw))==raw
    assert reader(raw)['schema_version']==1


@pytest.mark.parametrize('entry',['base','runtime','initramfs','prepare','inputs','assemble'])
def test_retired_recipe_rejected_before_any_callback_or_filesystem_work(tmp_path,entry):
    recipe=load_recipe((HISTORICAL/'recovery-recipe-v1.json').read_bytes())
    stage=tmp_path/'stage';output=tmp_path/'image.raw'
    limits=ResourceLimits(1,4*1024**3,1)
    def unexpected(*args,**kwargs):pytest.fail('retired recipe reached execution')
    with pytest.raises(BuildError,match='execution is retired.*schema-v2'):
        if entry=='base':synthesis.run_recovery_base_stage(recipe,None,None,stage,runner=None,limits=limits,rootfs_installer=unexpected)
        elif entry=='runtime':synthesis.run_recovery_runtime_stage(recipe,None,None,stage,{},runtime_installer=unexpected)
        elif entry=='initramfs':synthesis.run_recovery_initramfs_from_recipe(recipe,None,None,stage,{},{},runner=None,limits=limits)
        elif entry=='prepare':synthesis.prepare_recovery_image_stage(recipe,None,None,stage,output,runner=None,limits=limits,rootfs_installer=unexpected,cache=object())
        elif entry=='inputs':
            from quirkbench.recovery_image_plan import prepare_recovery_image_inputs
            prepare_recovery_image_inputs(recipe,None,None,stage,{},output)
        else:synthesis.assemble_recovery_image(recipe,None,None,{},None,image_builder=unexpected)
    assert list(tmp_path.iterdir())==[]
