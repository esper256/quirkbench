"""Reviewed platform implementations; general design is not platform qualification."""
from dataclasses import dataclass

@dataclass(frozen=True)
class X86UefiUsbAdapter:
    """Reviewed backend data; selection never establishes build/boot qualification."""

    adapter_id: str = "x86_64-uefi-usb-v1"
    target_architecture: str = "x86_64"
    controller_architecture: str = "x86_64"
    boot_method: str = "uefi"
    boot_transport: str = "usb"
    profile_names: tuple[str, ...] = ("generic-x86_64-uefi-usb.v1.json",)
    rpm_architectures: tuple[str, ...] = ("x86_64", "noarch")
    source_rpm_architectures: tuple[str, ...] = ("src", "x86_64")
    grub_image_format: str = "x86_64-efi"
    removable_efi_filename: str = "BOOTX64.EFI"

    def supports(self, *, target_architecture: str, boot_method: str,
                 controller_architecture: str) -> bool:
        return (target_architecture == self.target_architecture
                and boot_method == self.boot_method
                and controller_architecture == self.controller_architecture)


    kernel_arch_arg: str = "ARCH=x86_64"
    kernel_default_config: str = "x86_64_defconfig"
    kernel_image_relative: str = "arch/x86/boot/bzImage"


X86_UEFI_USB = X86UefiUsbAdapter()
ADAPTERS = {X86_UEFI_USB.adapter_id: X86_UEFI_USB}
# This whitelist is part of the frozen hardware-profile/baseline v1 contracts.
# Registering a future backend must not silently broaden those schema meanings.
V1_ADAPTER_IDS = (X86_UEFI_USB.adapter_id,)


def planning_adapters():
    return tuple(selected_adapter(name) for name in V1_ADAPTER_IDS)


def profile_adapter(adapter_id):
    if adapter_id not in V1_ADAPTER_IDS:
        raise ValueError('unsupported v1 profile platform adapter')
    return selected_adapter(adapter_id)


def selected_adapter(adapter_id):
    """Only source-reviewed implementations can be selected; reports cannot add one."""
    try:
        return ADAPTERS[adapter_id]
    except KeyError as exc:
        raise ValueError('unsupported platform adapter') from exc
