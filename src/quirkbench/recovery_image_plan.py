"""Bind audited stock recovery to the regular-file image adapter."""
from pathlib import Path
from .target_install import _check_recovery_unit_links
from .build import BuildError
from .image import ImageInputs
from .recovery_recipe import require_executable_recipe


def prepare_recovery_image_inputs(recipe: dict, catalog: dict, store,
                                  stage: Path, stage_record: dict,
                                  output: Path) -> ImageInputs:
    require_executable_recipe(recipe)
    from .recovery_stock_pipeline import prepare_image
    return prepare_image(recipe, store, Path(stage), stage_record, Path(output))


def audit_factory_root(rootfs, checked):
    """Shared publication boundary: no extra units, secrets or enrolled factory state."""
    _check_recovery_unit_links(rootfs, strict_direct_links=True)
    units = rootfs / "etc/systemd/system"
    installed_units = sorted(path.name for path in units.iterdir()
                             if not path.is_symlink() and path.is_file()
                             and path.suffix in {".service", ".mount", ".socket"})
    if installed_units != checked["unit_allowlist"]:
        raise BuildError("factory recovery units differ from reviewed allowlist")
    settings = rootfs / "etc/quirkbench"
    if settings.is_symlink() or not settings.is_dir() or any(settings.iterdir()):
        raise BuildError("factory recovery rootfs contains enrolled or image-specific state")
    for relative in ("etc/NetworkManager/system-connections",
                     "usr/lib/NetworkManager/system-connections"):
        network_profiles = rootfs / relative
        if (network_profiles.is_symlink()
                or (network_profiles.exists()
                    and (not network_profiles.is_dir() or any(network_profiles.iterdir())))):
            raise BuildError("factory recovery rootfs contains saved network profiles")
    machine_id = rootfs / "etc/machine-id"
    dbus_id = rootfs / "var/lib/dbus/machine-id"
    if (machine_id.is_symlink() or not machine_id.is_file()
            or machine_id.stat().st_size != 0
            or dbus_id.exists() or dbus_id.is_symlink()):
        raise BuildError("factory recovery rootfs contains a persistent machine identity")
    private_state = rootfs / "var/lib/quirkbench"
    if (private_state.is_symlink()
            or (private_state.exists()
                and (not private_state.is_dir() or any(private_state.iterdir())))):
        raise BuildError("factory recovery rootfs contains enrolled state")
    ssh = rootfs / "etc/ssh"
    if ((rootfs / "etc/wireguard").exists() or (rootfs / "etc/wireguard").is_symlink()
            or ssh.is_symlink() or (ssh.is_dir() and any(ssh.glob("ssh_host_*_key")))):
        raise BuildError("factory recovery rootfs contains private credentials")
