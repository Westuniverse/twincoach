"""为 AMD/Xilinx dma-proxy 生成双加速核客户端 overlay。"""

from __future__ import annotations

import json
from pathlib import Path
import re


_DTS_LABEL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def render_dma_proxy_overlay(
    contract: dict[str, object],
    *,
    avatar_dma_label: str = "axi_dma_avatar",
    joint_dma_label: str = "axi_dma_joint",
    rectified_dma_label: str | None = None,
    motion_stats_dma_label: str | None = None,
    dma_coherent: bool = False,
) -> str:
    """Render client nodes that bind to DMA nodes generated from the same XSA."""

    for label in (avatar_dma_label, joint_dma_label):
        if not _DTS_LABEL.fullmatch(label):
            raise ValueError(f"无效 DTS 标签：{label}")
    avatar = _dma_record(contract, "avatar_dma")
    joint = _dma_record(contract, "joint_dma")
    rectified_record = contract.get("rectified_dma")
    if rectified_record is not None:
        if rectified_dma_label is None:
            rectified_dma_label = "axi_dma_rectified"
        if not _DTS_LABEL.fullmatch(rectified_dma_label):
            raise ValueError(f"invalid DTS label: {rectified_dma_label}")
        rectified = _dma_record(contract, "rectified_dma")
    else:
        rectified = None
    motion_stats_record = contract.get("motion_stats_dma")
    if motion_stats_record is not None:
        if motion_stats_dma_label is None:
            motion_stats_dma_label = "axi_dma_motion_stats"
        if not _DTS_LABEL.fullmatch(motion_stats_dma_label):
            raise ValueError(f"invalid DTS label: {motion_stats_dma_label}")
        motion_stats = _dma_record(contract, "motion_stats_dma")
    else:
        motion_stats = None
    coherent = "\n            dma-coherent;" if dma_coherent else ""
    rectified_node = ""
    address_comment = (
        f"avatar={_hex_address(avatar)}, joint={_hex_address(joint)}"
    )
    if rectified is not None and rectified_dma_label is not None:
        rectified_node = f"""

            posepriv_rectified_proxy {{
                compatible = "xlnx,dma_proxy";
                dmas = <&{rectified_dma_label} 0>, <&{rectified_dma_label} 1>;
                dma-names = "posepriv_rectified_tx", "posepriv_rectified_rx";{coherent}
                status = "okay";
            }};"""
        address_comment += f", rectified={_hex_address(rectified)}"
    motion_stats_node = ""
    if motion_stats is not None and motion_stats_dma_label is not None:
        motion_stats_node = f"""

            posepriv_motion_stats_proxy {{
                compatible = "xlnx,dma_proxy";
                dmas = <&{motion_stats_dma_label} 0>, <&{motion_stats_dma_label} 1>;
                dma-names = "posepriv_motion_stats_tx", "posepriv_motion_stats_rx";{coherent}
                status = "okay";
            }};"""
        address_comment += f", motion_stats={_hex_address(motion_stats)}"
    source_hwh = str(contract.get("source_hwh", "same-XSA"))
    return f"""/dts-v1/;
/plugin/;

/*
 * 由 config/kv260_dual_accel_hardware.json 生成。
 * DMA 控制器节点和 GIC 中断号必须由与 XSA 匹配的 AMD 设备树生成器提供。
 */
/ {{
    fragment@0 {{
        target-path = "/";
        __overlay__ {{
            posepriv_avatar_proxy {{
                compatible = "xlnx,dma_proxy";
                dmas = <&{avatar_dma_label} 0>, <&{avatar_dma_label} 1>;
                dma-names = "posepriv_avatar_tx", "posepriv_avatar_rx";{coherent}
                status = "okay";
            }};

            posepriv_joint_proxy {{
                compatible = "xlnx,dma_proxy";
                dmas = <&{joint_dma_label} 0>, <&{joint_dma_label} 1>;
                dma-names = "posepriv_joint_tx", "posepriv_joint_rx";{coherent}
                status = "okay";
            }};{rectified_node}{motion_stats_node}
        }};
    }};
}};

/* 硬件核对：avatar={_hex_address(avatar)}, joint={_hex_address(joint)} */
/* hardware_contract={address_comment} */
/* source_hwh={source_hwh} */
"""


def load_and_render_dma_proxy_overlay(
    manifest: str | Path,
    **options: object,
) -> str:
    contract = json.loads(Path(manifest).read_text(encoding="utf-8"))
    if not isinstance(contract, dict):
        raise ValueError("硬件清单顶层必须是 JSON 对象")
    return render_dma_proxy_overlay(contract, **options)  # type: ignore[arg-type]


def _dma_record(contract: dict[str, object], name: str) -> dict[str, object]:
    record = contract.get(name)
    if not isinstance(record, dict):
        raise ValueError(f"硬件清单缺少 {name}")
    for field in ("instance", "base_address", "high_address"):
        if field not in record:
            raise ValueError(f"硬件清单 {name} 缺少 {field}")
    return record


def _hex_address(record: dict[str, object]) -> str:
    return f"0x{int(record['base_address']):08X}"


__all__ = ["load_and_render_dma_proxy_overlay", "render_dma_proxy_overlay"]
