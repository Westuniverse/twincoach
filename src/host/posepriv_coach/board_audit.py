"""Parse the stable summary emitted by the read-only KV260 audit script."""

from __future__ import annotations

from dataclasses import asdict, dataclass


_SECTIONS = ("[POSEPRIV SUMMARY]", "[POSEPRIV 摘要]")
_STATUS_VALUES = {"missing", "present", "pass", "fail"}
_BASE_REQUIRED_KEYS = (
    "audit_schema",
    "dpu_query_status",
    "python_pose_runtime_status",
    "xmutil_status",
    "xrt_status",
    "video_node_count",
)
_SCHEMA2_CAMERA_KEYS = (
    "v4l2_device_group_count",
    "stable_uvc_index0_count",
)
_TRAILING_REQUIRED_KEYS = (
    "uio_node_count",
    "udmabuf_node_count",
    "drm_node_count",
    "framebuffer_node_count",
    "connected_display_count",
)


@dataclass(frozen=True, slots=True)
class BoardAuditSummary:
    audit_schema: int
    dpu_query_status: str
    python_pose_runtime_status: str
    xmutil_status: str
    xrt_status: str
    video_node_count: int
    v4l2_device_group_count: int
    stable_uvc_index0_count: int
    uio_node_count: int
    udmabuf_node_count: int
    drm_node_count: int
    framebuffer_node_count: int
    connected_display_count: int
    usb_uvc_device_count: int = 0
    usb_uvc_root_bus_count: int = 0

    @property
    def dpu_runtime_ready(self) -> bool:
        return (
            self.dpu_query_status == "pass"
            and self.python_pose_runtime_status == "pass"
        )

    @property
    def multiple_video_nodes_present(self) -> bool:
        return self.video_node_count >= 2

    @property
    def multiple_v4l2_device_groups_present(self) -> bool:
        return self.v4l2_device_group_count >= 2

    @property
    def stable_dual_uvc_candidates_present(self) -> bool:
        return self.stable_uvc_index0_count >= 2

    @property
    def dma_userspace_candidate_present(self) -> bool:
        return self.uio_node_count > 0 or self.udmabuf_node_count > 0

    @property
    def display_candidate_present(self) -> bool:
        return (
            self.connected_display_count > 0
            and (self.drm_node_count > 0 or self.framebuffer_node_count > 0)
        )

    @property
    def dual_uvc_root_buses_observed(self) -> bool:
        """Return true only when two UVC devices are observed on two root buses."""

        return self.usb_uvc_device_count >= 2 and self.usb_uvc_root_bus_count >= 2

    def next_actions_zh(self) -> tuple[str, ...]:
        actions = []
        if not self.dpu_runtime_ready:
            actions.append("先解决 DPU 查询或 VART/XIR 导入，再编译和测试 XModel。")
        if not self.multiple_v4l2_device_groups_present:
            actions.append(
                "清点尚未显示两组 V4L2 设备；/dev/video* 节点数不能单独证明双摄，"
                "连接摄像头后用设备组、稳定路径和遮挡测试确认身份。"
            )
        if (
            self.usb_uvc_device_count >= 2
            and self.usb_uvc_root_bus_count < 2
        ):
            actions.append(
                "两路 UVC 已枚举但仍在同一个 USB 根总线；应把摄像头从同一 Hub 拆开，"
                "分别接到 USB0/U44 与 USB1/U46 后重新检查 lsusb -t。"
            )
        if not self.dma_userspace_candidate_present:
            actions.append("当前未发现 UIO/udmabuf 候选接口，先确定 overlay 与正式 DMA 驱动。")
        if not self.display_candidate_present:
            actions.append("连接 HDMI 并确认 DRM/帧缓冲节点与连接器状态。")
        if not actions:
            actions.append("候选条件齐全，依次执行静态图像、DMA、双摄和 HDMI 实测。")
        return tuple(actions)

    def to_record(self) -> dict[str, object]:
        record: dict[str, object] = asdict(self)
        record["derived"] = {
            "dpu_runtime_ready": self.dpu_runtime_ready,
            "multiple_video_nodes_present": self.multiple_video_nodes_present,
            "multiple_v4l2_device_groups_present": (
                self.multiple_v4l2_device_groups_present
            ),
            "stable_dual_uvc_candidates_present": (
                self.stable_dual_uvc_candidates_present
            ),
            "dma_userspace_candidate_present": self.dma_userspace_candidate_present,
            "display_candidate_present": self.display_candidate_present,
            "dual_uvc_root_buses_observed": self.dual_uvc_root_buses_observed,
        }
        record["next_actions_zh"] = self.next_actions_zh()
        return record


def parse_board_audit_log(text: str) -> BoardAuditSummary:
    """Parse only the final stable summary, leaving verbose tool output opaque."""

    present_sections = tuple(
        (text.rfind(section), section) for section in _SECTIONS if section in text
    )
    if not present_sections:
        raise ValueError("board audit summary section not found")
    _, final_section = max(present_sections)
    summary_text = text.rsplit(final_section, 1)[1]
    values: dict[str, str] = {}
    for raw_line in summary_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("["):
            break
        if "=" not in line:
            raise ValueError(f"invalid board audit summary line: {line}")
        key, value = line.split("=", 1)
        values[key] = value
    schema = values.get("audit_schema")
    if schema not in {"1", "2"}:
        raise ValueError("unsupported board audit schema")
    required_keys = _BASE_REQUIRED_KEYS + _TRAILING_REQUIRED_KEYS
    if schema == "2":
        required_keys += _SCHEMA2_CAMERA_KEYS
    missing = [key for key in required_keys if key not in values]
    if missing:
        raise ValueError("board audit summary is incomplete: " + ", ".join(missing))

    for key in (
        "dpu_query_status",
        "python_pose_runtime_status",
        "xmutil_status",
        "xrt_status",
    ):
        if values[key] not in _STATUS_VALUES:
            raise ValueError(f"invalid board audit status: {key}={values[key]}")

    counts: dict[str, int] = {}
    count_keys = (
        "video_node_count",
        "uio_node_count",
        "udmabuf_node_count",
        "drm_node_count",
        "framebuffer_node_count",
        "connected_display_count",
    )
    if schema == "2":
        count_keys += _SCHEMA2_CAMERA_KEYS
    for key in count_keys:
        try:
            count = int(values[key])
        except ValueError as exc:
            raise ValueError(f"invalid board audit count: {key}={values[key]}") from exc
        if count < 0:
            raise ValueError(f"board audit count must not be negative: {key}")
        counts[key] = count

    optional_usb_counts: dict[str, int] = {}
    for key in ("usb_uvc_device_count", "usb_uvc_root_bus_count"):
        raw_value = values.get(key)
        if raw_value is None:
            optional_usb_counts[key] = 0
            continue
        try:
            count = int(raw_value)
        except ValueError as exc:
            raise ValueError(f"invalid board audit count: {key}={raw_value}") from exc
        if count < 0:
            raise ValueError(f"board audit count must not be negative: {key}")
        optional_usb_counts[key] = count

    return BoardAuditSummary(
        audit_schema=int(schema),
        dpu_query_status=values["dpu_query_status"],
        python_pose_runtime_status=values["python_pose_runtime_status"],
        xmutil_status=values["xmutil_status"],
        xrt_status=values["xrt_status"],
        video_node_count=counts["video_node_count"],
        v4l2_device_group_count=counts.get("v4l2_device_group_count", 0),
        stable_uvc_index0_count=counts.get("stable_uvc_index0_count", 0),
        uio_node_count=counts["uio_node_count"],
        udmabuf_node_count=counts["udmabuf_node_count"],
        drm_node_count=counts["drm_node_count"],
        framebuffer_node_count=counts["framebuffer_node_count"],
        connected_display_count=counts["connected_display_count"],
        usb_uvc_device_count=optional_usb_counts["usb_uvc_device_count"],
        usb_uvc_root_bus_count=optional_usb_counts["usb_uvc_root_bus_count"],
    )
