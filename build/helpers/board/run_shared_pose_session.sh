#!/usr/bin/env bash
# Explicit opt-in supervisor; never configures PL or runs the coach as root.
set -euo pipefail
if [ "$#" -lt 4 ] || [ "$3" != -- ]; then
    echo "用法: $0 运行包目录 匹配的posepriv_shared_dma.ko -- 命令 [参数...]" >&2
    exit 2
fi
package_root=$1
module=$2
shift 3
device=/dev/posepriv_shared_pose_dma
if [ "$(id -u)" = 0 ]; then
    echo "请以普通用户启动；仅模块及设备权限操作使用sudo -n。" >&2
    exit 2
fi
cmp -s "$package_root/runtime/unified_sd_candidate.json" /boot/firmware/unified_sd_candidate.json || {
    echo "运行包与现役SD候选不匹配；不加载模块、不启动应用。" >&2
    exit 2
}
python3 - "$package_root/runtime/unified_sd_candidate.json" "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)" <<'PY'
import json
from pathlib import Path
import sys
candidate = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8-sig"))
if not isinstance(candidate.get("shared_pose_dma"), dict):
    raise SystemExit("现役候选不是共享姿态DMA镜像。")
if candidate["shared_pose_dma"].get("compatible")=="posepriv,shared-pose-dma-roi-v1":
    sys.path.insert(0,sys.argv[2])
    from pps4_roi1_profile import roi1_manifest_matches
    if not roi1_manifest_matches(candidate):raise SystemExit("ROI1 cap/ABI manifest mismatch")
    tree=Path("/sys/firmware/devicetree/base/amba_pl")
    client=tree/"posepriv_shared_pose_dma@a0030000";roi=tree/"roi_sampler@a2040000"
    if (client/"compatible").read_bytes().rstrip(b"\0")!=b"posepriv,shared-pose-dma-roi-v1":raise SystemExit("ROI1 DT compatible mismatch")
    if (client/"posepriv,roi-sampler").read_bytes()!=(roi/"phandle").read_bytes():raise SystemExit("ROI1 DT owner phandle mismatch")
PY
test -f "$module"
test "$(modinfo -F name "$module")" = posepriv_shared_dma
read -r module_kernel _ <<< "$(modinfo -F vermagic "$module")"
test "$module_kernel" = "$(uname -r)"
command -v -- "$1" > /dev/null
command -v setsid > /dev/null
# Serialize this supervisor's permission changes. The driver still owns actual
# exclusive opens; no resets, retries or PL writes are introduced here.
exec 9>/tmp/posepriv_shared_pose_session.lock
flock -n 9 || { echo "另一个共享DMA会话正在运行。" >&2; exit 2; }
if [ ! -d /sys/module/posepriv_shared_dma ]; then
    sudo -n insmod "$module"
    echo POSEPRIV_SHARED_MODULE_LOADED
else
    echo POSEPRIV_SHARED_MODULE_ALREADY_LOADED
fi
if [ ! -c "$device" ]; then
    echo "模块已加载但共享DMA字符设备缺失；保留诊断，不卸载或重试。" >&2
    exit 2
fi
original=$(stat -L -c '%u:%g:%a:%t,%T' "$device")
IFS=: read -r owner group mode rdev <<< "$original"
[[ "$rdev" =~ ^[0-9a-fA-F]+,[0-9a-fA-F]+$ ]]
caller_group=$(id -g)
if [ "$owner" != 0 ] || { [ "$original" != "0:0:600:$rdev" ] && [ "$original" != "0:$caller_group:660:$rdev" ]; }; then
    echo "共享DMA设备权限不是项目既有0600/当前组0660状态；不覆盖。" >&2
    exit 2
fi
child=
permissions_touched=0
restore_permissions() {
    result=$?
    trap - EXIT
    if [ "$permissions_touched" = 1 ]; then
        if [ ! -c "$device" ] || [ "$(stat -L -c '%t,%T' "$device")" != "$rdev" ]; then
            echo "设备已更换，不能恢复原权限。" >&2
            result=1
        elif ! sudo -n chown "$owner:$group" "$device" || ! sudo -n chmod "$mode" "$device"; then
            echo "恢复共享DMA设备权限失败。" >&2
            result=1
        else
            echo "POSEPRIV_SHARED_PERMISSIONS_RESTORED $original"
        fi
    fi
    exit "$result"
}
stop_child() {
    signal_status=$1
    trap '' HUP INT TERM
    if [ -n "$child" ]; then
        if [ "$signal_status" = 130 ] && [ "${POSEPRIV_COOPERATIVE_PREVIEW_STOP:-0}" = 1 ]; then
            echo "POSEPRIV_SHARED_CHILD_GROUP_COOPERATIVE_INT $child" >&2
            kill -INT -- "-$child" 2>/dev/null || kill -INT "$child" 2>/dev/null || true
            for cooperative_attempt in {1..150}; do
                if ! kill -0 -- "-$child" 2>/dev/null; then break; fi
                sleep 0.1
            done
            if ! kill -0 -- "-$child" 2>/dev/null; then
                wait "$child" || true
                exit "$signal_status"
            fi
        fi
        # This child was started in its own session below; do not leave the
        # coach's native VART subprocess holding cameras/DMA after interruption.
        kill -TERM -- "-$child" 2>/dev/null || kill -TERM "$child" 2>/dev/null || true
        # setsid separates this group from an outer timeout's eventual KILL.
        # Check the group, not just its leader: the leader can exit while a
        # TERM-resistant descendant keeps running/holding the fd or pipes.
        for stop_attempt in {1..20}; do
            if ! kill -0 -- "-$child" 2>/dev/null; then break; fi
            sleep 0.1
        done
        if kill -0 -- "-$child" 2>/dev/null; then
            echo "POSEPRIV_SHARED_CHILD_GROUP_KILLED $child" >&2
            kill -KILL -- "-$child" 2>/dev/null || true
        fi
        # This can still block for an unkillable kernel task. Do not promise
        # trap/permission recovery after kernel failure, SIGKILL or power loss.
        wait "$child" || true
    fi
    exit "$signal_status"
}
trap restore_permissions EXIT
trap 'stop_child 129' HUP
trap 'stop_child 130' INT
trap 'stop_child 143' TERM
# Arm restoration before either privileged command, including partial failure.
permissions_touched=1
sudo -n chown "0:$caller_group" "$device"
sudo -n chmod 0660 "$device"
export POSEPRIV_PL_MODE=shared-pose
export POSEPRIV_SHARED_POSE_DMA_DEVICE="$device"
set +m
setsid --wait "$@" 9>&- &
child=$!
set +e
wait "$child"
result=$?
set -e
child=
exit "$result"
