#!/bin/bash
set -euo pipefail
output=${1:?Output directory required}
cat > "$output/grub-initial.cfg" <<'GRUB'
search --no-floppy --label QB-BOOT --set=qbboot
# Keep the standalone prefix: GRUB modules live in its embedded filesystem.
configfile ($qbboot)/grub.cfg
GRUB
grub2-mkstandalone -O x86_64-efi --modules='part_gpt fat ext2 normal linux search search_fs_uuid search_label loadenv blscfg' \
  -o "$output/BOOTX64.EFI" "boot/grub/grub.cfg=$output/grub-initial.cfg"
