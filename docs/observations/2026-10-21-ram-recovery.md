# RAM-resident recovery check

Image `b6c626eb2a4dee97d6f96a8c` uses Fedora dracut live-image support and a
690 MiB compressed recovery OS. The build uses the selected kernel's explicit
module directory. Recovery OS files remain read-only; shared data stays writable.

Manual QEMU checks used 4 GiB RAM and disposable USB disk files. Both boots
connected to a temporary controller. Dracut reported copying the OS to RAM;
the first copy took about nine seconds in this VM.

After removing the entire virtual USB device, both checks successfully ran
hardware diagnostics, NetworkManager inspection and journal reads, maintained
the controller connection, and uploaded a fresh emergency report from the TUI.
Force power off and force reboot each worked after removal. The kernel reported
real storage I/O errors; recovery programs remained available. This does not
establish physical firmware, Wi-Fi or survival through a stuck kernel.

Local results are under `.quirkbench/ram-recovery/ram-shutdown-rmat8evl/` and
`.quirkbench/ram-recovery/ram-restart-rb2m86im/`. Large disposable disk files were
removed after success. The manual check is not part of routine tests. No physical
device was flashed and no running owner controller was restarted.
