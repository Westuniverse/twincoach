"""KV260 联合验收的可复现命令计划。"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path

from .avatar_pipeline import TRIANGULATION_BACKENDS
from .avatar_setup import RECTIFIED_ACCELERATOR_BACKENDS
from .camera_mode_selection import CameraModeCandidate, load_camera_mode_plan
from .deployment_readiness import DEPLOYMENT_READINESS_PROFILES
from .motion_backend import MOTION_BACKEND_NAMES, MOTION_STATS_BACKEND_NAMES


@dataclass(frozen=True, slots=True)
class UnifiedAcceptanceConfig:
    python_executable: str
    repository_root: Path
    front_camera: Path
    side_camera: Path
    xmodel: Path
    vart_input_scale: float
    calibration: Path
    output_directory: Path
    profile: str = "unified"
    camera_mode_plan: Path | None = None
    triangulation_backend: str = "general"
    rectified_baseline: float | None = None
    rectified_focal: float | None = None
    rectified_minimum_disparity: float = 64.0 / (1 << 14)
    rectified_maximum_vertical_disparity: float = 0.02
    rectified_accelerator_backend: str = "reference"
    rectified_verilator_executable: Path | None = None
    rectified_verilator_work_dir: Path | None = None
    rectified_dma_device: Path | None = None
    rectified_dma_proxy_tx: Path | None = None
    rectified_dma_proxy_rx: Path | None = None
    vart_input_offset: float = 0.0
    duration_seconds: float = 30.0
    width: int = 1280
    height: int = 720
    fps: float = 30.0
    camera_fourcc: str = "MJPG"
    repetitions: int = 8
    joint_tx: Path = Path("/dev/posepriv_joint_tx")
    joint_rx: Path = Path("/dev/posepriv_joint_rx")
    avatar_tx: Path = Path("/dev/posepriv_avatar_tx")
    avatar_rx: Path = Path("/dev/posepriv_avatar_rx")
    framebuffer_device: Path | None = None
    display_render_width: int | None = None
    display_render_height: int | None = None
    avatar_input_device: Path | None = None
    avatar_mesh: Path | None = None
    cjk_font: Path | None = None
    native_vart_probe: Path | None = None
    parallel_dpu: bool = False
    parallel_capture: bool = False
    pipeline_capture: bool = False
    async_avatar: bool = False
    async_display: bool = True
    show_camera_preview: bool = False
    capture_fallback_fps: float = 0.0
    capture_buffer_size: int = 0
    pose_probe_image: Path | None = None
    pose_probe_baseline: Path | None = None
    pose_probe_maximum_distance: float = 0.03
    pose_probe_minimum_keypoints: int = 8
    motion_backend: str = "reference"
    motion_dma_device: Path | None = None
    motion_tx: Path | None = None
    motion_rx: Path | None = None
    motion_stats_backend: str = "none"
    motion_stats_dma_device: Path | None = None
    motion_stats_tx: Path | None = None
    motion_stats_rx: Path | None = None

    def validate(self) -> None:
        if self.profile not in DEPLOYMENT_READINESS_PROFILES:
            raise ValueError(f"unknown acceptance profile: {self.profile}")
        if self.front_camera == self.side_camera:
            raise ValueError("正面与侧面必须使用不同摄像头设备")
        if self.duration_seconds <= 0:
            raise ValueError("联合会话时长必须大于 0 秒")
        if self.width <= 0 or self.height <= 0 or self.fps <= 0:
            raise ValueError("摄像头宽、高和帧率必须为正数")
        if (self.display_render_width is None) != (
            self.display_render_height is None
        ):
            raise ValueError("显示绘制宽度和高度必须同时提供")
        if self.display_render_width is not None and (
            self.display_render_width < 1 or self.display_render_height < 1
        ):
            raise ValueError("显示绘制宽度和高度必须为正数")
        selected_mode, selected_fourcc = _load_camera_mode_metadata(
            self.camera_mode_plan
        )
        effective_fps = selected_mode.fps if selected_mode is not None else self.fps
        if not math.isfinite(self.vart_input_scale) or self.vart_input_scale <= 0.0:
            raise ValueError("VART 输入 scale 必须是有限正数")
        if not math.isfinite(self.vart_input_offset):
            raise ValueError("VART 输入 offset 必须是有限数")
        if self.triangulation_backend not in TRIANGULATION_BACKENDS:
            raise ValueError(f"unknown triangulation backend: {self.triangulation_backend}")
        if self.rectified_accelerator_backend not in RECTIFIED_ACCELERATOR_BACKENDS:
            raise ValueError(
                "unknown rectified accelerator backend: "
                f"{self.rectified_accelerator_backend}"
            )
        if (
            self.rectified_accelerator_backend != "reference"
            and self.triangulation_backend != "rectified-reference"
        ):
            raise ValueError(
                "非 reference 校正三角测量后端需要 triangulation_backend=rectified-reference"
            )
        if self.triangulation_backend == "rectified-reference":
            if self.rectified_baseline is None or self.rectified_focal is None:
                raise ValueError("校正三角测量后端需要 baseline 和 focal")
            if not math.isfinite(self.rectified_baseline) or self.rectified_baseline <= 0.0:
                raise ValueError("校正三角测量 baseline 必须为有限正数")
            if not math.isfinite(self.rectified_focal) or self.rectified_focal <= 0.0:
                raise ValueError("校正三角测量 focal 必须为有限正数")
        if (
            not math.isfinite(self.rectified_minimum_disparity)
            or self.rectified_minimum_disparity <= 0.0
        ):
            raise ValueError("校正三角测量最小视差必须为正数")
        if (
            not math.isfinite(self.rectified_maximum_vertical_disparity)
            or self.rectified_maximum_vertical_disparity < 0.0
        ):
            raise ValueError("校正三角测量最大垂直视差不能为负数")
        if (
            self.rectified_accelerator_backend == "verilator"
            and self.rectified_verilator_executable is None
        ):
            raise ValueError("校正三角测量 verilator 需要 bridge 可执行文件")
        if self.rectified_accelerator_backend == "linux-dma" and self.rectified_dma_device is None:
            raise ValueError("校正三角测量 linux-dma 需要设备路径")
        if self.rectified_accelerator_backend == "linux-dma-proxy" and (
            self.rectified_dma_proxy_tx is None or self.rectified_dma_proxy_rx is None
        ):
            raise ValueError("校正三角测量 linux-dma-proxy 需要 TX/RX 设备路径")
        if self.repetitions < 2:
            raise ValueError("PL 黄金对拍至少需要 2 次，以检查跨帧状态")
        effective_fourcc = selected_fourcc or self.camera_fourcc
        if not effective_fourcc or len(effective_fourcc) != 4:
            raise ValueError("摄像头 FOURCC 必须是 4 个字符")
        if not math.isfinite(self.capture_fallback_fps) or self.capture_fallback_fps < 0.0:
            raise ValueError("摄像头回退 FPS 必须是有限非负数")
        if (
            self.capture_fallback_fps >= effective_fps
            and self.capture_fallback_fps != 0.0
        ):
            raise ValueError("摄像头回退 FPS 必须小于目标 FPS")
        if self.capture_buffer_size < 0:
            raise ValueError("摄像头缓冲提示必须为非负整数")
        if self.native_vart_probe is None and (
            self.parallel_dpu
            or self.parallel_capture
            or self.pipeline_capture
            or self.capture_fallback_fps != 0.0
        ):
            raise ValueError("原生 VART 并行/预取/回退选项需要 --native-vart-probe")
        if (self.pose_probe_image is None) != (self.pose_probe_baseline is None):
            raise ValueError("DPU 静态语义对照必须同时提供测试图片和 CPU 基线")
        if (
            not math.isfinite(self.pose_probe_maximum_distance)
            or self.pose_probe_maximum_distance < 0.0
        ):
            raise ValueError("DPU 静态语义最大位置误差不能为负数")
        if self.pose_probe_minimum_keypoints < 1:
            raise ValueError("DPU 静态语义对照关键点数必须为正数")


        if self.motion_backend not in MOTION_BACKEND_NAMES:
            raise ValueError(f"unknown motion backend: {self.motion_backend}")
        if self.motion_backend == "linux-dma" and self.motion_dma_device is None:
            raise ValueError("motion linux-dma requires a device path")
        if self.motion_backend == "linux-dma-proxy" and (
            self.motion_tx is None or self.motion_rx is None
        ):
            raise ValueError("motion linux-dma-proxy requires TX and RX devices")
        if self.motion_stats_backend not in ("none",) + MOTION_STATS_BACKEND_NAMES:
            raise ValueError(f"unknown motion stats backend: {self.motion_stats_backend}")
        if (
            self.motion_stats_backend == "linux-dma"
            and self.motion_stats_dma_device is None
        ):
            raise ValueError("motion stats linux-dma requires a device path")
        if self.motion_stats_backend == "linux-dma-proxy" and (
            self.motion_stats_tx is None or self.motion_stats_rx is None
        ):
            raise ValueError("motion stats linux-dma-proxy requires TX and RX devices")
        if self.profile == "core-joint" and (
            self.rectified_accelerator_backend != "reference"
            or self.motion_backend != "reference"
            or self.motion_stats_backend != "none"
        ):
            raise ValueError(
                "core-joint 只允许 reference 三角测量加速器/运动后端，并关闭 motion stats"
            )


@dataclass(frozen=True, slots=True)
class UnifiedAcceptanceStep:
    name: str
    description_zh: str
    command: tuple[str, ...]


def build_unified_acceptance_plan(
    config: UnifiedAcceptanceConfig,
) -> tuple[UnifiedAcceptanceStep, ...]:
    """生成不经 shell 拼接的五阶段分档验收命令。"""

    config.validate()
    selected_camera_mode, selected_camera_fourcc = _load_camera_mode_metadata(
        config.camera_mode_plan
    )
    capture_fourcc = selected_camera_fourcc or config.camera_fourcc
    capture_width = (
        selected_camera_mode.width
        if selected_camera_mode is not None
        else config.width
    )
    capture_height = (
        selected_camera_mode.height
        if selected_camera_mode is not None
        else config.height
    )
    capture_fps = (
        selected_camera_mode.fps
        if selected_camera_mode is not None
        else config.fps
    )
    root = config.repository_root
    output = config.output_directory
    core_joint = config.profile == "core-joint"
    readiness = output / "deployment_readiness.json"
    if core_joint:
        golden = output / "joint_pl_dma_golden.json"
    else:
        golden = output / (
            "triple_pl_dma_golden.json"
            if config.rectified_accelerator_backend == "linux-dma-proxy"
            else "dual_pl_dma_golden.json"
        )
    joint_first_golden = output / "joint_pl_dma_golden.json"
    session = output / "live_session.jsonl"
    evidence = output / "accelerated_session_evidence.json"
    performance = output / "performance_diagnosis.md"
    dpu_probe = output / "dpu_pose_probe.json"
    dpu_comparison = output / "dpu_pose_comparison.json"

    readiness_probe = (
        config.python_executable,
        str(root / "scripts" / "probe_deployment_readiness.py"),
        "--profile",
        config.profile,
        "--xmodel",
        str(config.xmodel),
        "--output",
        str(readiness),
    )
    if config.native_vart_probe is not None:
        readiness_probe = readiness_probe + (
            "--native-vart-probe",
            str(config.native_vart_probe),
        )
    verify_parts = [
        config.python_executable,
        str(root / "scripts" / "verify_linux_pl_dma.py"),
        "--joint-tx",
        str(config.joint_tx),
        "--joint-rx",
        str(config.joint_rx),
        "--repetitions",
        str(config.repetitions),
    ]
    if core_joint:
        verify_parts.append("--joint-only")
    else:
        verify_parts.extend(
            (
                "--avatar-tx",
                str(config.avatar_tx),
                "--avatar-rx",
                str(config.avatar_rx),
                "--joint-first-output",
                str(joint_first_golden),
            )
        )
    verify_parts.extend(("--output", str(golden)))
    verify_pl = tuple(verify_parts)
    if not core_joint and config.rectified_accelerator_backend == "linux-dma-proxy":
        verify_pl = verify_pl + (
            "--rectified-tx",
            str(config.rectified_dma_proxy_tx),
            "--rectified-rx",
            str(config.rectified_dma_proxy_rx),
        )
    if not core_joint and config.motion_stats_backend == "linux-dma-proxy":
        verify_pl = verify_pl + (
            "--motion-stats-tx",
            str(config.motion_stats_tx),
            "--motion-stats-rx",
            str(config.motion_stats_rx),
        )
    avatar_backend = "reference" if core_joint else "linux-dma-proxy"
    if config.native_vart_probe is None:
        live = [
            config.python_executable,
            str(root / "scripts" / "run_live_coach.py"),
            "--xmodel",
            str(config.xmodel),
            "--vart-input-scale",
            str(config.vart_input_scale),
            "--vart-input-offset",
            str(config.vart_input_offset),
            "--front",
            str(config.front_camera),
            "--side",
            str(config.side_camera),
            "--camera-backend",
            "v4l2",
            "--width",
            str(capture_width),
            "--height",
            str(capture_height),
            "--fps",
            str(capture_fps),
            "--camera-fourcc",
            capture_fourcc,
            "--joint-backend",
            "linux-dma-proxy",
            "--joint-dma-proxy-tx",
            str(config.joint_tx),
            "--joint-dma-proxy-rx",
            str(config.joint_rx),
            "--display-layout",
            "avatar3d",
            "--avatar-calibration",
            str(config.calibration),
            "--avatar-detail",
            "detailed",
            "--avatar-skinning-backend",
            avatar_backend,
            "--framebuffer-display",
            "--duration",
            str(config.duration_seconds),
            "--report",
            str(session),
        ]
    else:
        native_frames = max(1, math.ceil(config.duration_seconds * capture_fps))
        live = [
            config.python_executable,
            str(root / "scripts" / "run_board_vart_coach.py"),
            "--probe",
            str(config.native_vart_probe),
            "--xmodel-ntd",
            str(config.xmodel),
            "--front",
            str(config.front_camera),
            "--side",
            str(config.side_camera),
            "--frames",
            str(native_frames),
            "--capture-width",
            str(capture_width),
            "--capture-height",
            str(capture_height),
            "--capture-fps",
            str(capture_fps),
            "--capture-fourcc",
            capture_fourcc,
            "--capture-fallback-fps",
            str(config.capture_fallback_fps),
            "--capture-buffer-size",
            str(config.capture_buffer_size),
            "--joint-backend",
            "linux-dma-proxy",
            "--joint-dma-proxy-tx",
            str(config.joint_tx),
            "--joint-dma-proxy-rx",
            str(config.joint_rx),
            "--avatar-calibration",
            str(config.calibration),
            "--avatar-detail",
            "detailed",
            "--avatar-skinning-backend",
            avatar_backend,
            "--display-layout",
            "avatar3d",
            "--framebuffer-display",
            "--report",
            str(session),
        ]
        if config.parallel_dpu:
            live.append("--parallel-dpu")
        if config.parallel_capture:
            live.append("--parallel-capture")
        if config.pipeline_capture:
            live.append("--pipeline-capture")
    if not core_joint:
        live.extend(
            (
                "--avatar-dma-proxy-tx",
                str(config.avatar_tx),
                "--avatar-dma-proxy-rx",
                str(config.avatar_rx),
            )
        )
    if config.camera_mode_plan is not None:
        live.extend(("--mode-plan", str(config.camera_mode_plan)))
    if config.show_camera_preview:
        live.append("--show-camera-preview")
    if config.async_avatar:
        live.append("--async-avatar")
    if config.async_display:
        live.append("--async-display")
    if config.motion_backend != "reference":
        live.extend(("--motion-backend", config.motion_backend))
        if config.motion_backend == "linux-dma":
            live.extend(("--motion-dma-device", str(config.motion_dma_device)))
        elif config.motion_backend == "linux-dma-proxy":
            live.extend(
                (
                    "--motion-dma-proxy-tx",
                    str(config.motion_tx),
                    "--motion-dma-proxy-rx",
                    str(config.motion_rx),
                )
            )
    if config.motion_stats_backend != "none":
        live.extend(("--motion-stats-backend", config.motion_stats_backend))
        if config.motion_stats_backend == "linux-dma":
            live.extend(("--motion-stats-dma-device", str(config.motion_stats_dma_device)))
        elif config.motion_stats_backend == "linux-dma-proxy":
            live.extend(
                (
                    "--motion-stats-dma-proxy-tx",
                    str(config.motion_stats_tx),
                    "--motion-stats-dma-proxy-rx",
                    str(config.motion_stats_rx),
                )
            )
    if config.framebuffer_device is not None:
        live.extend(("--framebuffer-device", str(config.framebuffer_device)))
    if config.display_render_width is not None:
        live.extend(
            (
                "--display-render-width",
                str(config.display_render_width),
                "--display-render-height",
                str(config.display_render_height),
            )
        )
    if config.avatar_input_device is not None:
        live.extend(("--avatar-input-device", str(config.avatar_input_device)))
    if config.avatar_mesh is not None:
        live.extend(("--avatar-mesh", str(config.avatar_mesh)))
    if config.cjk_font is not None:
        live.extend(("--cjk-font", str(config.cjk_font)))
    live.extend(("--avatar-triangulation-backend", config.triangulation_backend))
    live.extend(
        ("--avatar-triangulation-accelerator", config.rectified_accelerator_backend)
    )
    if config.triangulation_backend == "rectified-reference":
        live.extend(
            (
                "--avatar-rectified-baseline",
                str(config.rectified_baseline),
                "--avatar-rectified-focal",
                str(config.rectified_focal),
                "--avatar-rectified-min-disparity",
                str(config.rectified_minimum_disparity),
                "--avatar-rectified-max-vertical-disparity",
                str(config.rectified_maximum_vertical_disparity),
            )
        )
    if config.rectified_accelerator_backend == "linux-dma":
        live.extend(("--avatar-triangulation-dma-device", str(config.rectified_dma_device)))
    elif config.rectified_accelerator_backend == "linux-dma-proxy":
        live.extend(
            (
                "--avatar-triangulation-dma-proxy-tx",
                str(config.rectified_dma_proxy_tx),
                "--avatar-triangulation-dma-proxy-rx",
                str(config.rectified_dma_proxy_rx),
            )
        )
    elif config.rectified_accelerator_backend == "verilator":
        live.extend(
            (
                "--avatar-triangulation-verilator-executable",
                str(config.rectified_verilator_executable),
            )
        )
        if config.rectified_verilator_work_dir is not None:
            live.extend(
                (
                    "--avatar-triangulation-verilator-work-dir",
                    str(config.rectified_verilator_work_dir),
                )
            )
    assess_parts = [
        config.python_executable,
        str(root / "scripts" / "assess_accelerated_session.py"),
        str(session),
        "--profile",
        config.profile,
        "--pl-golden-evidence",
        str(golden),
        "--deployment-readiness",
        str(readiness),
    ]
    optional_steps: list[UnifiedAcceptanceStep] = []
    if config.pose_probe_image is not None and config.pose_probe_baseline is not None:
        dpu_probe_command = (
            config.python_executable,
            str(root / "scripts" / "probe_movenet_image.py"),
            "--xmodel",
            str(config.xmodel),
            "--image",
            str(config.pose_probe_image),
            "--vart-input-scale",
            str(config.vart_input_scale),
            "--vart-input-offset",
            str(config.vart_input_offset),
            "--output",
            str(dpu_probe),
        )
        compare = (
            config.python_executable,
            str(root / "scripts" / "compare_pose_probes.py"),
            str(config.pose_probe_baseline),
            str(dpu_probe),
            "--maximum-position-distance",
            str(config.pose_probe_maximum_distance),
            "--minimum-keypoints",
            str(config.pose_probe_minimum_keypoints),
            "--output",
            str(dpu_comparison),
        )
        optional_steps.extend(
            (
                UnifiedAcceptanceStep(
                    "dpu_probe", "运行 DPU 静态图像语义探针", dpu_probe_command
                ),
                UnifiedAcceptanceStep("dpu_compare", "与 CPU 基线核对关键点语义", compare),
            )
        )
        assess_parts.extend(("--dpu-pose-comparison", str(dpu_comparison)))
    assess_parts.extend(("--output", str(evidence)))
    assess = tuple(assess_parts)
    diagnose = (
        config.python_executable,
        str(root / "scripts" / "diagnose_session_performance.py"),
        str(session),
        "--target-fps",
        str(capture_fps),
        "--output",
        str(performance),
    )
    return (
        UnifiedAcceptanceStep(
            "readiness",
            (
                "只读清点双摄、DPU、Joint PL 与 HDMI"
                if core_joint
                else "只读清点双摄、DPU、双 PL 与 HDMI"
            ),
            readiness_probe,
        ),
        *optional_steps,
        UnifiedAcceptanceStep(
            "pl_golden",
            (
                "仅打开 Joint PL 并完成多事务逐字节黄金对拍"
                if core_joint
                else "先隔离关节 PL 并保存检查点，再扩展为多通道逐字节核对"
            ),
            verify_pl,
        ),
        UnifiedAcceptanceStep(
            "live_session",
            (
                "运行 DPU+Joint PL+reference 数字人真实双摄实时会话"
                if core_joint
                else "运行真实双摄联合实时会话"
            ),
            tuple(live),
        ),
        UnifiedAcceptanceStep("performance", "生成不设硬门限的中文性能诊断", diagnose),
        UnifiedAcceptanceStep("evidence", "汇总同一次联合验收证据", assess),
    )


def _load_camera_mode_metadata(
    path: Path | None,
) -> tuple[CameraModeCandidate | None, str | None]:
    """Load one selected mode for every command derived from the plan.

    The CLI also loads the plan for an early user-facing message, but the
    library builder must repeat the small metadata read so callers that build
    plans directly cannot accidentally retain the default 720p/30 budget.
    """

    if path is None:
        return None, None
    # A plan may be a target-board path when a Windows host only constructs
    # the command list.  The CLI has already validated local plans before it
    # reaches this builder; an inaccessible target path must therefore remain
    # pass-through metadata instead of breaking dry-run command generation.
    candidate = Path(path)
    try:
        if not candidate.is_file():
            return None, None
    except OSError:
        return None, None
    try:
        return load_camera_mode_plan(candidate)
    except ValueError as exc:
        raise ValueError(f"摄像头模式清单无效：{exc}") from exc


def _load_camera_mode_plan(path: Path | None) -> CameraModeCandidate | None:
    """Compatibility wrapper returning only the selected mode."""

    mode, _ = _load_camera_mode_metadata(path)
    return mode


__all__ = [
    "UnifiedAcceptanceConfig",
    "UnifiedAcceptanceStep",
    "build_unified_acceptance_plan",
]
