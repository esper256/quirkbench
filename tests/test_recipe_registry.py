"""P7a installed recipe identity and eligibility, with no privileged dispatch."""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from quirkbench.build import sha256_file
from quirkbench.contracts import CapabilityReport, Experiment, Outcome, canonical, digest
from quirkbench.recipe_registry import RecipeRegistry, RecipeUnavailable, load_manifest
from quirkbench.target import RecipeOutput, TargetAgent
from test_target import FakeClient


def fixture_recipe(experiment):
    return RecipeOutput(Outcome.INCONCLUSIVE, 'Bounded fixture observation.')


def manifest():
    return {'schema_version': 1, 'recipe_id': 'fixture', 'version': 1,
            'entrypoint': f'{fixture_recipe.__module__}:{fixture_recipe.__name__}',
            'code_sha256': sha256_file(Path(__file__)),
            'modes': ['experiment', 'simulation'], 'architectures': ['x86_64'],
            'required_capabilities': ['fixture.observe'], 'required_privileges': [],
            'parameter_specs': {'count': {'type': 'integer', 'minimum': 1, 'maximum': 3},
                                'level': {'type': 'enum', 'choices': ['low', 'normal']}},
            'runtime_limit_s': 120, 'physical_observations': [], 'risk_class': 'observation'}


def registry(tmp_path, document=None):
    directory = tmp_path / 'recipes'
    directory.mkdir()
    raw = canonical(document or manifest())
    (directory / 'fixture.v1.json').write_bytes(raw)
    return RecipeRegistry(directory, {'fixture': fixture_recipe}), digest(raw)


def experiment(manifest_digest, **changes):
    value = {'experiment_id': 'one', 'hypothesis': 'Observe fixture', 'recipe': 'fixture',
             'artifacts': {'recipe_manifest': manifest_digest},
             'parameters': {'count': 2, 'level': 'normal'}, 'timeout_s': 90}
    value.update(changes)
    return Experiment(**value)


def report(mode='simulation', architecture='x86_64', capabilities=None):
    return CapabilityReport('target-1', 'boot-1', capabilities or ['fixture.observe'],
                            mode=mode, inventory={'architecture': architecture})


def test_manifest_schema_and_packaged_observation_identity(tmp_path):
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / 'schemas/recipe-manifest.v1.schema.json').read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(manifest())
    assert load_manifest(canonical(manifest())) == manifest()
    from quirkbench import runtime
    from quirkbench.recipe_registry import installed_registry
    installed = installed_registry(Path(runtime.__file__).with_name('recipes'))
    assert 'system-observation' in installed.records
    Draft202012Validator(schema).validate(installed.records['system-observation'][0])


def test_exact_installed_identity_and_bounded_eligibility(tmp_path):
    installed, value = registry(tmp_path)
    assert installed.resolve(experiment(value), report()) is fixture_recipe
    bad = [
        experiment('0' * 64),
        experiment(value, timeout_s=121),
        experiment(value, parameters={'count': 4, 'level': 'normal'}),
        experiment(value, parameters={'count': True, 'level': 'normal'}),
        experiment(value, parameters={'count': 2, 'level': 'normal', 'command': 'sh'}),
    ]
    for request in bad:
        with pytest.raises(RecipeUnavailable):
            installed.resolve(request, report())
    for target in (report('recovery'), report(architecture='aarch64'), report(capabilities=['other'])):
        with pytest.raises(RecipeUnavailable):
            installed.resolve(experiment(value), target)


@pytest.mark.parametrize('change', [
    lambda value: value.update(version=2),
    lambda value: value.update(entrypoint='os:system'),
    lambda value: value.update(code_sha256='0' * 64),
    lambda value: value.update(required_privileges=['write_firmware']),
    lambda value: value.update(risk_class='fault'),
    lambda value: value.update(physical_observations=['listen']),
    lambda value: value.update(parameter_specs={'path': {'type': 'path'}}),
])
def test_unknown_version_code_privilege_or_parameter_is_not_installed(tmp_path, change):
    value = copy.deepcopy(manifest())
    change(value)
    directory = tmp_path / 'recipes'
    directory.mkdir()
    (directory / 'fixture.v1.json').write_bytes(canonical(value))
    with pytest.raises(RecipeUnavailable):
        RecipeRegistry(directory, {'fixture': fixture_recipe})


def test_mutated_installed_manifest_refused_at_dispatch(tmp_path):
    installed, value = registry(tmp_path)
    (tmp_path / 'recipes/fixture.v1.json').write_bytes(canonical({**manifest(), 'runtime_limit_s': 121}))
    with pytest.raises(RecipeUnavailable, match='changed'):
        installed.resolve(experiment(value), report())


def test_target_dispatch_requires_authorized_installed_manifest(tmp_path):
    installed, value = registry(tmp_path)
    client = FakeClient()
    request = {'attempt_id': 'attempt-1', 'token': 'attempt-secret', 'device_id': 'target-1',
               'boot_id': 'boot-1', 'campaign_id': 'campaign-1', 'generation': 1,
               'experiment': experiment(value).to_dict()}
    client.claims.append(request)
    target = TargetAgent(client, tmp_path / 'target', report(), recipe_registry=installed)
    assert target.step() == 'completed'
    assert client.results[0].outcome == Outcome.INCONCLUSIVE
    bad_client = FakeClient()
    bad_client.claims.append({**request, 'attempt_id': 'attempt-2',
                              'experiment': experiment('0' * 64).to_dict()})
    target = TargetAgent(bad_client, tmp_path / 'another-target', report(), recipe_registry=installed)
    assert target.step() == 'completed'
    assert bad_client.results[0].outcome == Outcome.NEEDS_HUMAN
    assert 'manifest' in bad_client.results[0].summary


def test_current_mode_capabilities_exclude_unavailable_recipes():
    from quirkbench import runtime
    from quirkbench.recipe_registry import installed_registry
    root=Path(runtime.__file__).with_name('recipes')
    recovery=installed_registry(root)
    assert recovery.eligible(mode='recovery',architecture='x86_64')==['system-observation']
    candidate=installed_registry(root,candidate=True)
    assert candidate.eligible(mode='experiment',architecture='x86_64')==['audio-observation','system-observation']
    assert candidate.eligible(mode='experiment',architecture='aarch64')==[]


def test_eligibility_does_not_invent_external_capabilities(tmp_path):
    installed,_=registry(tmp_path)
    assert installed.eligible(mode='simulation',architecture='x86_64')==[]
    assert installed.eligible(mode='simulation',architecture='x86_64',capabilities=['fixture.observe'])==['fixture']
