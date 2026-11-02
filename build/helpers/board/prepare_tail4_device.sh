#!/usr/bin/env bash
# Prepare the existing matched Tail4 driver after a cold boot; never load PL.
set -euo pipefail
root=${1:?package root required}
cmp -s "$root/runtime/unified_sd_candidate.json" /boot/firmware/unified_sd_candidate.json || {
    echo 'Tail4 preparation refused: boot firmware does not match package.' >&2
    exit 2
}
module="$root/runtime/posepriv_tail4.ko"
test "$(modinfo -F name "$module")" = posepriv_tail4
read -r kernel _ <<< "$(modinfo -F vermagic "$module")"
test "$kernel" = "$(uname -r)"
if [ ! -d /sys/module/posepriv_tail4 ]; then
    sudo -n insmod "$module"
fi
test -c /dev/posepriv_tail4
# Explicit access for the ordinary user; no world-writable device or root coach.
if [ ! -r /dev/posepriv_tail4 ] || [ ! -w /dev/posepriv_tail4 ]; then
    sudo -n setfacl -m "u:$(id -u):rw" /dev/posepriv_tail4
fi
echo POSEPRIV_TAIL4_READY
