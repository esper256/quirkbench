"""Installed, reviewed recipe identities and bounded target eligibility.

Manifests name only callables already wired into the target runtime. They cannot
import modules, select device paths, grant privileges, or supply shell commands.
"""
from __future__ import annotations

import inspect
import hashlib
import json
from pathlib import Path
import re
from typing import Callable

from .contracts import ContractError, Experiment, CapabilityReport, digest, identifier, sha256
from .product_contracts import _pairs

MAX_MANIFEST_BYTES = 64 * 1024
ALLOWED_PRIVILEGES = frozenset({'read_kernel_log','audio_playback'})
ALLOWED_MODES = frozenset({'recovery', 'experiment', 'simulation'})
ALLOWED_ARCHITECTURES = frozenset({'x86_64'})
FIELDS = {'schema_version', 'recipe_id', 'version', 'entrypoint', 'code_sha256',
          'modes', 'architectures', 'required_capabilities', 'required_privileges',
          'parameter_specs', 'runtime_limit_s', 'physical_observations', 'risk_class'}


class RecipeUnavailable(ContractError):
    pass


def _file_sha256(path: Path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _exact(value, names, label):
    if not isinstance(value, dict) or set(value) != names:
        raise RecipeUnavailable(f'invalid {label} fields')
    return value


def _unique_ids(value, allowed=None):
    if not isinstance(value, list) or len(value) > 64:
        raise RecipeUnavailable('invalid recipe eligibility list')
    for item in value:
        identifier(item)
        if allowed is not None and item not in allowed:
            raise RecipeUnavailable('unsupported recipe eligibility or privilege')
    if value != sorted(set(value)):
        raise RecipeUnavailable('recipe eligibility lists must be unique and sorted')


def validate_manifest(value):
    _exact(value, FIELDS, 'recipe manifest')
    if type(value['schema_version']) is not int or value['schema_version'] not in (1,2):
        raise RecipeUnavailable('unsupported recipe manifest schema')
    identifier(value['recipe_id'])
    if type(value['version']) is not int or not 1 <= value['version'] <= 65535:
        raise RecipeUnavailable('unsupported recipe version')
    if not isinstance(value['entrypoint'], str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*', value['entrypoint']):
        raise RecipeUnavailable('invalid installed recipe entrypoint')
    sha256(value['code_sha256'])
    for name, allowed in (('modes', ALLOWED_MODES), ('architectures', ALLOWED_ARCHITECTURES),
                          ('required_capabilities', None), ('required_privileges', ALLOWED_PRIVILEGES if value['schema_version']==2 else frozenset({'read_kernel_log'})),
                          ('physical_observations', None)):
        _unique_ids(value[name], allowed)
    if not value['modes'] or not value['architectures']:
        raise RecipeUnavailable('recipe requires mode and architecture eligibility')
    if value['risk_class'] != 'observation':
        raise RecipeUnavailable('fault and suspend recipes require separate review')
    if value['physical_observations']:
        raise RecipeUnavailable('physical observation dispatch awaits the durable human protocol')
    if type(value['runtime_limit_s']) is not int or not 1 <= value['runtime_limit_s'] <= 3600:
        raise RecipeUnavailable('invalid recipe runtime limit')
    specs = value['parameter_specs']
    if not isinstance(specs, dict) or len(specs) > 32:
        raise RecipeUnavailable('invalid parameter specifications')
    for name, spec in specs.items():
        identifier(name)
        if not isinstance(spec, dict) or spec.get('type') not in ('integer', 'boolean', 'enum'):
            raise RecipeUnavailable('unsupported parameter type')
        if spec['type'] == 'integer':
            _exact(spec, {'type', 'minimum', 'maximum'}, 'integer parameter')
            if (type(spec['minimum']) is not int or type(spec['maximum']) is not int
                    or not -1_000_000 <= spec['minimum'] <= spec['maximum'] <= 1_000_000):
                raise RecipeUnavailable('invalid integer parameter bounds')
        elif spec['type'] == 'boolean':
            _exact(spec, {'type'}, 'boolean parameter')
        else:
            _exact(spec, {'type', 'choices'}, 'enum parameter')
            choices = spec['choices']
            if (not isinstance(choices, list) or not 1 <= len(choices) <= 32
                    or any(not isinstance(choice, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', choice) for choice in choices)
                    or choices != sorted(set(choices))):
                raise RecipeUnavailable('invalid enum parameter choices')
    return value


def load_manifest(raw: bytes):
    if len(raw) > MAX_MANIFEST_BYTES:
        raise RecipeUnavailable('recipe manifest exceeds 64 KiB')
    try:
        value = json.loads(raw, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(RecipeUnavailable('nonfinite recipe number')))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ContractError) as exc:
        raise RecipeUnavailable('invalid recipe manifest JSON') from exc
    try:
        return validate_manifest(value)
    except ContractError as exc:
        raise RecipeUnavailable(str(exc)) from exc


def _validate_parameters(specs, values):
    if not isinstance(values, dict) or set(values) != set(specs):
        raise RecipeUnavailable('recipe parameters differ from installed manifest')
    for name, spec in specs.items():
        item = values[name]
        if spec['type'] == 'integer':
            valid = type(item) is int and spec['minimum'] <= item <= spec['maximum']
        elif spec['type'] == 'boolean':
            valid = type(item) is bool
        else:
            valid = type(item) is str and item in spec['choices']
        if not valid:
            raise RecipeUnavailable(f'invalid bounded recipe parameter: {name}')


class RecipeRegistry:
    def __init__(self, directory: Path, installed: dict[str, Callable], *, granted_privileges=()):
        self.directory = Path(directory)
        if not self.directory.is_absolute() or self.directory.is_symlink() or not self.directory.is_dir():
            raise RecipeUnavailable('installed recipe directory must be an absolute regular directory')
        self.installed = dict(installed)
        self.granted_privileges = frozenset(granted_privileges)
        if not self.granted_privileges <= ALLOWED_PRIVILEGES:
            raise RecipeUnavailable('unreviewed recipe privilege')
        self.records = {}
        entries = sorted(self.directory.iterdir())
        if len(entries) > 64:
            raise RecipeUnavailable('too many installed recipes')
        for path in entries:
            if path.suffix != '.json' or path.is_symlink() or not path.is_file():
                raise RecipeUnavailable('invalid installed recipe file')
            if path.stat().st_size > MAX_MANIFEST_BYTES:
                raise RecipeUnavailable('recipe manifest exceeds 64 KiB')
            raw = path.read_bytes()
            manifest = load_manifest(raw)
            recipe_id = manifest['recipe_id']
            if path.name != recipe_id + '.v' + str(manifest['version']) + '.json' or recipe_id in self.records:
                raise RecipeUnavailable('duplicate or misnamed installed recipe')
            self.records[recipe_id] = (manifest, digest(raw), path)
        for recipe_id in self.records:
            self._verify_binding(recipe_id)

    def _verify_binding(self, recipe_id):
        manifest, _, _ = self.records[recipe_id]
        recipe = self.installed.get(recipe_id)
        if recipe is None:
            raise RecipeUnavailable('installed recipe has no reviewed callable binding')
        if manifest['entrypoint'] != f'{recipe.__module__}:{recipe.__name__}':
            raise RecipeUnavailable('installed recipe entrypoint differs from manifest')
        try:
            source = inspect.getsourcefile(recipe)
            match = source is not None and not Path(source).is_symlink() and _file_sha256(Path(source)) == manifest['code_sha256']
        except (OSError, TypeError):
            match = False
        if not match:
            raise RecipeUnavailable('installed recipe code differs from manifest')
        return recipe

    def resolve(self, experiment: Experiment, report: CapabilityReport):
        record = self.records.get(experiment.recipe)
        if record is None:
            raise RecipeUnavailable('requested recipe is not installed')
        manifest, installed_digest, path = record
        try:
            unchanged = (not path.is_symlink() and path.is_file() and path.stat().st_size <= MAX_MANIFEST_BYTES
                         and digest(path.read_bytes()) == installed_digest)
        except OSError:
            unchanged = False
        if not unchanged:
            raise RecipeUnavailable('installed recipe manifest changed after registry load')
        if experiment.artifacts.get('recipe_manifest') != installed_digest:
            raise RecipeUnavailable('authorized recipe manifest differs from installed bytes')
        recipe = self._verify_binding(experiment.recipe)
        if report.mode not in manifest['modes'] or report.inventory.get('architecture') not in manifest['architectures']:
            raise RecipeUnavailable('target mode or architecture is ineligible')
        if not set(manifest['required_capabilities']) <= set(report.capabilities):
            raise RecipeUnavailable('target lacks required recipe capability')
        if not set(manifest['required_privileges']) <= self.granted_privileges:
            raise RecipeUnavailable('target service lacks reviewed recipe privilege')
        if experiment.timeout_s > manifest['runtime_limit_s']:
            raise RecipeUnavailable('attempt timeout exceeds recipe runtime bound')
        _validate_parameters(manifest['parameter_specs'], experiment.parameters)
        return recipe


class UnavailableRegistry:
    """Keep target evidence upload alive when installed recipe metadata is unusable."""

    def resolve(self, experiment, report):
        raise RecipeUnavailable('installed recipe registry unavailable')
