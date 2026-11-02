"""VART/XIR contract inspection and execution for a deployed MoveNet xmodel."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from functools import lru_cache
import math
from pathlib import Path
import re
from time import monotonic_ns
from typing import Any, Mapping, Sequence


class MoveNetVartError(RuntimeError):
    """Report an unavailable VART stack or incompatible deployed model."""


@dataclass(frozen=True, slots=True)
class VartTensorContract:
    name: str
    shape: tuple[int, ...]
    fix_point: int
    dtype: str = "int8"


@dataclass(frozen=True, slots=True)
class MoveNetVartContract:
    input: VartTensorContract
    output: VartTensorContract


@dataclass(frozen=True, slots=True)
class MoveNetNtdVartContract:
    """四输出 MoveNet NTD 模型在 VART 边界上的固定契约。"""

    input: VartTensorContract
    heatmap: VartTensorContract
    center: VartTensorContract
    reg: VartTensorContract
    offset: VartTensorContract
    outputs: tuple[VartTensorContract, ...]


@dataclass(frozen=True, slots=True)
class MoveNetNtdPostprocessConfig:
    """AMD Vitis AI MoveNet 后处理配置。"""

    confidence_threshold: float = 0.1
    center_weights: tuple[float, ...] = ()


@dataclass(frozen=True, slots=True)
class VartInferenceStats:
    """VART execute_async 到 wait 返回之间的稳定态耗时摘要。"""

    frames: int
    total_ns: int
    max_ns: int
    latency_window_size: int = 0
    latency_sample_count: int = 0
    p95_ns: int | None = None

    @property
    def mean_ms(self) -> float | None:
        return None if self.frames == 0 else self.total_ns / self.frames / 1_000_000

    @property
    def p95_ms(self) -> float | None:
        return None if self.p95_ns is None else self.p95_ns / 1_000_000


class VartMoveNetRunner:
    """Execute one MoveNet DPU runner using explicit pixel-domain semantics."""

    def __init__(
        self,
        runner: Any,
        contract: MoveNetVartContract,
        *,
        input_value_scale: float,
        input_value_offset: float = 0.0,
        numpy_module: Any | None = None,
        clock_ns: Any = monotonic_ns,
        latency_window_size: int = 1024,
    ) -> None:
        if not math.isfinite(input_value_scale) or input_value_scale <= 0.0:
            raise ValueError("input_value_scale must be finite and positive")
        if not math.isfinite(input_value_offset):
            raise ValueError("input_value_offset must be finite")
        if latency_window_size <= 0:
            raise ValueError("latency_window_size must be positive")
        if numpy_module is None:
            try:
                import numpy as numpy_module  # type: ignore[import-not-found, no-redef]
            except ImportError as exc:
                raise MoveNetVartError("MoveNet VART 推理需要 NumPy") from exc
        self._runner = runner
        self._numpy = numpy_module
        self.contract = contract
        self.input_width = contract.input.shape[2]
        self.input_height = contract.input.shape[1]
        self.input_value_scale = input_value_scale
        self.input_value_offset = input_value_offset
        endpoint_values = (
            input_value_offset,
            255.0 * input_value_scale + input_value_offset,
        )
        fixed_scale = 2.0**contract.input.fix_point
        self.input_quantized_range = (
            min(endpoint_values) * fixed_scale,
            max(endpoint_values) * fixed_scale,
        )
        self.input_clipping_expected = (
            self.input_quantized_range[0] < -128.0
            or self.input_quantized_range[1] > 127.0
        )
        self._clock_ns = clock_ns
        self._frames = 0
        self._total_ns = 0
        self._max_ns = 0
        self._latency_samples_ns: deque[int] = deque(maxlen=latency_window_size)

    def __call__(self, pixels: Any) -> Any:
        array = self._numpy.asarray(pixels)
        expected = (self.input_height, self.input_width, 3)
        if tuple(array.shape) != expected:
            raise MoveNetVartError(
                f"预处理图像形状必须是 {expected}，实际为 {tuple(array.shape)}"
            )

        model_values = array * self.input_value_scale + self.input_value_offset
        fixed_scale = 2.0**self.contract.input.fix_point
        quantized = self._numpy.clip(
            self._numpy.rint(model_values * fixed_scale),
            -128,
            127,
        ).astype(self._numpy.int8)
        input_data = quantized.reshape(self.contract.input.shape)
        output_data = self._numpy.empty(
            self.contract.output.shape,
            dtype=self._numpy.int8,
            order="C",
        )

        infer_start_ns = self._clock_ns()
        submitted = self._runner.execute_async([input_data], [output_data])
        if isinstance(submitted, tuple):
            job_id = submitted[0]
            if len(submitted) > 1 and submitted[1] not in (None, 0):
                raise MoveNetVartError(f"VART 提交失败，状态 {submitted[1]}")
        else:
            job_id = submitted
        wait_status = self._runner.wait(job_id)
        if wait_status not in (None, 0):
            raise MoveNetVartError(f"VART 推理失败，状态 {wait_status}")
        infer_end_ns = self._clock_ns()
        elapsed_ns = infer_end_ns - infer_start_ns
        if elapsed_ns < 0:
            raise MoveNetVartError("VART clock must be monotonic")
        self._frames += 1
        self._total_ns += elapsed_ns
        self._max_ns = max(self._max_ns, elapsed_ns)
        self._latency_samples_ns.append(elapsed_ns)

        output_scale = 2.0 ** (-self.contract.output.fix_point)
        return output_data.astype(self._numpy.float32) * output_scale

    def stats(self) -> VartInferenceStats:
        """返回不包含预处理和模型冷启动的 VART 稳态计时摘要。"""

        return VartInferenceStats(
            self._frames,
            self._total_ns,
            self._max_ns,
            latency_window_size=self._latency_samples_ns.maxlen or 0,
            latency_sample_count=len(self._latency_samples_ns),
            p95_ns=_nearest_rank_percentile(self._latency_samples_ns, 0.95),
        )


class VartMoveNetNtdRunner:
    """运行 B3136 专用 MoveNet NTD，并返回通用 MoveNet `[y,x,score]`。"""

    def __init__(
        self,
        runner: Any,
        contract: MoveNetNtdVartContract,
        *,
        input_value_scale: float,
        input_value_offset: float = 0.0,
        postprocess: MoveNetNtdPostprocessConfig | None = None,
        postprocess_implementation: str = "scalar",
        numpy_module: Any | None = None,
        clock_ns: Any = monotonic_ns,
        latency_window_size: int = 1024,
    ) -> None:
        if not math.isfinite(input_value_scale) or input_value_scale <= 0.0:
            raise ValueError("input_value_scale must be finite and positive")
        if not math.isfinite(input_value_offset):
            raise ValueError("input_value_offset must be finite")
        if latency_window_size <= 0:
            raise ValueError("latency_window_size must be positive")
        _validate_ntd_decode_implementation(postprocess_implementation)
        if postprocess is None:
            postprocess = MoveNetNtdPostprocessConfig()
        if not 0.0 <= postprocess.confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold must be within [0, 1]")
        if numpy_module is None:
            try:
                import numpy as numpy_module  # type: ignore[import-not-found, no-redef]
            except ImportError as exc:
                raise MoveNetVartError("MoveNet VART 推理需要 NumPy") from exc
        self._runner = runner
        self._numpy = numpy_module
        self.contract = contract
        self.input_width = contract.input.shape[2]
        self.input_height = contract.input.shape[1]
        self.input_value_scale = input_value_scale
        self.input_value_offset = input_value_offset
        endpoint_values = (
            input_value_offset,
            255.0 * input_value_scale + input_value_offset,
        )
        fixed_scale = 2.0**contract.input.fix_point
        self.input_quantized_range = (
            min(endpoint_values) * fixed_scale,
            max(endpoint_values) * fixed_scale,
        )
        self.input_clipping_expected = (
            self.input_quantized_range[0] < -128.0
            or self.input_quantized_range[1] > 127.0
        )
        self.postprocess = postprocess
        self.postprocess_implementation = postprocess_implementation
        self._clock_ns = clock_ns
        self._frames = 0
        self._total_ns = 0
        self._max_ns = 0
        self._latency_samples_ns: deque[int] = deque(maxlen=latency_window_size)

    def __call__(self, pixels: Any) -> list[list[float]]:
        array = self._numpy.asarray(pixels)
        expected = (self.input_height, self.input_width, 3)
        if tuple(array.shape) != expected:
            raise MoveNetVartError(
                f"预处理图像形状必须是 {expected}，实际为 {tuple(array.shape)}"
            )
        model_values = array * self.input_value_scale + self.input_value_offset
        fixed_scale = 2.0**self.contract.input.fix_point
        quantized = self._numpy.clip(
            self._numpy.rint(model_values * fixed_scale),
            -128,
            127,
        ).astype(self._numpy.int8)
        input_data = quantized.reshape(self.contract.input.shape)
        output_data = {
            tensor.name: self._numpy.empty(
                tensor.shape,
                dtype=self._numpy.int8,
                order="C",
            )
            for tensor in self.contract.outputs
        }
        output_buffers = [output_data[tensor.name] for tensor in self.contract.outputs]

        infer_start_ns = self._clock_ns()
        submitted = self._runner.execute_async([input_data], output_buffers)
        if isinstance(submitted, tuple):
            job_id = submitted[0]
            if len(submitted) > 1 and submitted[1] not in (None, 0):
                raise MoveNetVartError(f"VART 提交失败，状态 {submitted[1]}")
        else:
            job_id = submitted
        wait_status = self._runner.wait(job_id)
        if wait_status not in (None, 0):
            raise MoveNetVartError(f"VART 推理失败，状态 {wait_status}")
        infer_end_ns = self._clock_ns()
        elapsed_ns = infer_end_ns - infer_start_ns
        if elapsed_ns < 0:
            raise MoveNetVartError("VART clock must be monotonic")
        self._frames += 1
        self._total_ns += elapsed_ns
        self._max_ns = max(self._max_ns, elapsed_ns)
        self._latency_samples_ns.append(elapsed_ns)

        semantic_outputs: dict[str, Any] = {}
        fix_points: dict[str, int] = {}
        for tensor in self.contract.outputs:
            kind = _movenet_ntd_output_kind(tensor.name)
            if kind is None:
                raise MoveNetVartError(f"无法识别 MoveNet NTD 输出：{tensor.name!r}")
            semantic_outputs[kind] = output_data[tensor.name]
            fix_points[kind] = tensor.fix_point
        return decode_movenet_ntd_outputs(
            semantic_outputs,
            fix_points=fix_points,
            confidence_threshold=self.postprocess.confidence_threshold,
            center_weights=self.postprocess.center_weights,
            implementation=self.postprocess_implementation,
            numpy_module=self._numpy,
        )

    def stats(self) -> VartInferenceStats:
        return VartInferenceStats(
            self._frames,
            self._total_ns,
            self._max_ns,
            latency_window_size=self._latency_samples_ns.maxlen or 0,
            latency_sample_count=len(self._latency_samples_ns),
            p95_ns=_nearest_rank_percentile(self._latency_samples_ns, 0.95),
        )


def _nearest_rank_percentile(samples: deque[int], percentile: float) -> int | None:
    if not samples:
        return None
    ordered = sorted(samples)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def inspect_movenet_vart_runner(runner: Any) -> MoveNetVartContract:
    """Validate the tensor boundary exposed by one already-created DPU runner."""

    inputs = runner.get_input_tensors()
    outputs = runner.get_output_tensors()
    if len(inputs) != 1 or len(outputs) != 1:
        raise MoveNetVartError("MoveNet VART Runner 必须恰好具有一个输入和一个输出")

    input_contract = _tensor_contract(inputs[0])
    output_contract = _tensor_contract(outputs[0])
    if input_contract.dtype != "int8" or output_contract.dtype != "int8":
        raise MoveNetVartError(
            "MoveNet DPU 当前适配器只支持 int8 输入和输出，"
            f"实际为 {input_contract.dtype}/{output_contract.dtype}"
        )
    if (
        len(input_contract.shape) != 4
        or input_contract.shape[0] != 1
        or input_contract.shape[3] != 3
    ):
        raise MoveNetVartError(
            f"MoveNet DPU 输入形状必须是 [1,H,W,3]，实际为 {input_contract.shape}"
        )
    if output_contract.shape != (1, 1, 17, 3):
        raise MoveNetVartError(
            "MoveNet DPU 输出形状必须是 [1,1,17,3]，"
            f"实际为 {output_contract.shape}"
        )
    return MoveNetVartContract(input_contract, output_contract)


def inspect_movenet_ntd_vart_runner(runner: Any) -> MoveNetNtdVartContract:
    """检查 B3136 MoveNet NTD 的一个输入、四个 HWC 输出。"""

    inputs = runner.get_input_tensors()
    outputs = runner.get_output_tensors()
    if len(inputs) != 1 or len(outputs) != 4:
        raise MoveNetVartError(
            "MoveNet NTD VART Runner 必须具有一个输入和四个输出，"
            f"实际为 {len(inputs)}/{len(outputs)}"
        )
    input_contract = _tensor_contract(inputs[0])
    if input_contract.dtype != "int8":
        raise MoveNetVartError(f"MoveNet NTD 输入必须是 int8，实际为 {input_contract.dtype}")
    if input_contract.shape != (1, 192, 192, 3):
        raise MoveNetVartError(
            "MoveNet NTD 输入形状必须是 [1,192,192,3]，"
            f"实际为 {input_contract.shape}"
        )

    contracts = tuple(_tensor_contract(tensor) for tensor in outputs)
    if any(contract.dtype != "int8" for contract in contracts):
        raise MoveNetVartError("MoveNet NTD 当前只支持 int8 输出")
    by_kind: dict[str, VartTensorContract] = {}
    expected_shapes = {
        "heatmap": (1, 48, 48, 17),
        "center": (1, 48, 48, 1),
        "reg": (1, 48, 48, 34),
        "offset": (1, 48, 48, 34),
    }
    for contract in contracts:
        kind = _movenet_ntd_output_kind(contract.name)
        if kind is None:
            raise MoveNetVartError(f"无法识别 MoveNet NTD 输出：{contract.name!r}")
        if kind in by_kind:
            raise MoveNetVartError(f"MoveNet NTD 输出重复：{kind}")
        if contract.shape != expected_shapes[kind]:
            raise MoveNetVartError(
                f"MoveNet NTD {kind} 输出形状必须是 {expected_shapes[kind]}，"
                f"实际为 {contract.shape}"
            )
        by_kind[kind] = contract
    return MoveNetNtdVartContract(
        input=input_contract,
        heatmap=by_kind["heatmap"],
        center=by_kind["center"],
        reg=by_kind["reg"],
        offset=by_kind["offset"],
        outputs=contracts,
    )


def load_movenet_vart_contract(
    model_path: str | Path,
    *,
    xir_module: Any | None = None,
    vart_module: Any | None = None,
) -> MoveNetVartContract:
    """Deserialize an xmodel, create its sole DPU runner and inspect tensors."""

    path = Path(model_path)
    if not path.is_file():
        raise MoveNetVartError(f"找不到 XModel：{path}")
    if xir_module is None or vart_module is None:
        try:
            import vart as imported_vart  # type: ignore[import-not-found]
            import xir as imported_xir  # type: ignore[import-not-found]
        except ImportError as exc:
            raise MoveNetVartError("当前 Python 环境没有可用的 VART/XIR 模块") from exc
        xir_module = imported_xir
        vart_module = imported_vart

    runner = _create_movenet_runner(path, xir_module, vart_module)
    return inspect_movenet_vart_runner(runner)


def load_movenet_vart(
    model_path: str | Path,
    *,
    input_value_scale: float,
    input_value_offset: float = 0.0,
    xir_module: Any | None = None,
    vart_module: Any | None = None,
    numpy_module: Any | None = None,
) -> VartMoveNetRunner:
    """Load a deployed MoveNet runner with an explicit RGB pixel mapping."""

    path = Path(model_path)
    if not path.is_file():
        raise MoveNetVartError(f"找不到 XModel：{path}")
    if xir_module is None or vart_module is None:
        try:
            import vart as imported_vart  # type: ignore[import-not-found]
            import xir as imported_xir  # type: ignore[import-not-found]
        except ImportError as exc:
            raise MoveNetVartError("当前 Python 环境没有可用的 VART/XIR 模块") from exc
        xir_module = imported_xir
        vart_module = imported_vart
    runner = _create_movenet_runner(path, xir_module, vart_module)
    contract = inspect_movenet_vart_runner(runner)
    return VartMoveNetRunner(
        runner,
        contract,
        input_value_scale=input_value_scale,
        input_value_offset=input_value_offset,
        numpy_module=numpy_module,
    )


def load_movenet_ntd_postprocess_config(
    config_path: str | Path,
) -> MoveNetNtdPostprocessConfig:
    """读取官方 `movenet_ntd_pt.prototxt` 中的阈值和中心权重。"""

    path = Path(config_path)
    if not path.is_file():
        raise MoveNetVartError(f"找不到 MoveNet NTD 配置：{path}")
    text = path.read_text(encoding="utf-8")
    threshold_match = re.search(
        r"\bconf_threshold\s*:\s*([-+0-9.eE]+)",
        text,
    )
    threshold = float(threshold_match.group(1)) if threshold_match else 0.1
    weights = tuple(
        float(value)
        for value in re.findall(r"\bcenter_weight\s*:\s*([-+0-9.eE]+)", text)
    )
    return MoveNetNtdPostprocessConfig(
        confidence_threshold=threshold,
        center_weights=weights,
    )


def decode_movenet_ntd_outputs(
    outputs: Mapping[str, Any],
    *,
    fix_points: Mapping[str, int],
    confidence_threshold: float = 0.1,
    center_weights: Sequence[float] = (),
    implementation: str = "scalar",
    numpy_module: Any | None = None,
) -> list[list[float]]:
    """按 AMD Vitis AI Library 的官方算法解码 MoveNet NTD 四个输出。

    返回与 `decode_movenet_singlepose` 相同的 17 项 `[y, x, confidence]`。
    `outputs` 的键名为 `heatmap`、`center`、`reg`、`offset`，数组可以带一个
    batch 维；输出量化比例严格使用 `2**(-fix_point)`。
    默认保留独立标量参考；可选 `numpy` 只支持 int8 和非负的 heat/center
    fix_point，使用相同 math sigmoid，并标量复核近并列候选。
    """

    _validate_ntd_decode_implementation(implementation)
    if not 0.0 <= confidence_threshold <= 1.0:
        raise ValueError("confidence_threshold must be within [0, 1]")
    required = ("heatmap", "center", "reg", "offset")
    if set(outputs) != set(required) or set(fix_points) != set(required):
        raise MoveNetVartError("MoveNet NTD 输出键必须恰好包含 heatmap/center/reg/offset")
    if numpy_module is None:
        try:
            import numpy as numpy_module  # type: ignore[import-not-found, no-redef]
        except ImportError as exc:
            raise MoveNetVartError("MoveNet NTD 后处理需要 NumPy") from exc

    arrays = {name: numpy_module.asarray(outputs[name]) for name in required}
    arrays = {name: _strip_single_batch(array, name) for name, array in arrays.items()}
    heatmap = arrays["heatmap"]
    center = arrays["center"]
    reg = arrays["reg"]
    offset = arrays["offset"]
    if len(heatmap.shape) != 3 or heatmap.shape[2] != 17:
        raise MoveNetVartError(f"MoveNet NTD heatmap 形状非法：{tuple(heatmap.shape)}")
    height, width, _ = (int(value) for value in heatmap.shape)
    expected_shapes = {
        "center": (height, width, 1),
        "reg": (height, width, 34),
        "offset": (height, width, 34),
    }
    for name, expected in expected_shapes.items():
        if tuple(int(value) for value in arrays[name].shape) != expected:
            raise MoveNetVartError(
                f"MoveNet NTD {name} 形状应为 {expected}，实际为 {tuple(arrays[name].shape)}"
            )
    size = width * height
    if center_weights:
        if len(center_weights) != size:
            raise MoveNetVartError(
                f"MoveNet NTD center_weight 必须有 {size} 项，实际为 {len(center_weights)}"
            )
        weights = tuple(float(value) for value in center_weights)
    else:
        weights = (1.0,) * size
    if any(not math.isfinite(value) for value in weights):
        raise MoveNetVartError("MoveNet NTD center_weight 不能包含非有限数")

    scales = {name: 2.0 ** (-int(fix_points[name])) for name in required}
    if implementation == "numpy":
        if height < 1 or width < 1:
            raise MoveNetVartError("MoveNet NTD numpy 解码需要非空空间维")
        if any(array.dtype != numpy_module.dtype("int8") for array in arrays.values()):
            raise MoveNetVartError("MoveNet NTD numpy 解码只支持 int8 四头")
        if any(int(fix_points[name]) < 0 for name in ("heatmap", "center")):
            raise MoveNetVartError("MoveNet NTD numpy heat/center fix_point 必须非负")
        return _decode_movenet_ntd_numpy(
            arrays, scales, weights, confidence_threshold, numpy_module
        )
    center_x = 0
    center_y = 0
    center_value = -math.inf
    for y in range(height):
        for x in range(width):
            index = y * width + x
            value = _sigmoid(float(center[y, x, 0]) * scales["center"]) * weights[index]
            if value > center_value:
                center_value = value
                center_x = x
                center_y = y

    decoded: list[list[float]] = []
    for joint in range(17):
        reg_x = (
            float(reg[center_y, center_x, 2 * joint]) * scales["reg"] + 0.5 + center_x
        )
        reg_y = (
            float(reg[center_y, center_x, 2 * joint + 1]) * scales["reg"] + 0.5 + center_y
        )
        best_value = -math.inf
        best_x = 0
        best_y = 0
        for y in range(height):
            for x in range(width):
                distance = math.sqrt((x - reg_x) ** 2 + (y - reg_y) ** 2) + 1.8
                heat = _sigmoid(float(heatmap[y, x, joint]) * scales["heatmap"])
                value = heat / distance
                if value > best_value:
                    best_value = value
                    best_x = x
                    best_y = y
        score = _sigmoid(float(heatmap[best_y, best_x, joint]) * scales["heatmap"])
        if score < confidence_threshold:
            decoded.append([0.0, 0.0, 0.0])
            continue
        offset_x = float(offset[best_y, best_x, 2 * joint]) * scales["offset"]
        offset_y = float(offset[best_y, best_x, 2 * joint + 1]) * scales["offset"]
        decoded.append(
            [
                (best_y + offset_y) / height,
                (best_x + offset_x) / width,
                score,
            ]
        )
    return decoded


def _validate_ntd_decode_implementation(implementation: str) -> None:
    if implementation not in ("scalar", "numpy"):
        raise ValueError("postprocess implementation must be scalar or numpy")


@lru_cache(maxsize=8)
def _int8_sigmoid_table(scale: float) -> tuple[float, ...]:
    # Python math, not np.exp: confidence equality at the threshold is contractual.
    return tuple(_sigmoid(float(code) * scale) for code in range(-128, 128))


def _decode_movenet_ntd_numpy(arrays, scales, weights, threshold, np):
    heatmap, center, reg, offset = (arrays[name] for name in ("heatmap", "center", "reg", "offset"))
    height, width, _ = heatmap.shape
    heat_table = np.asarray(_int8_sigmoid_table(scales["heatmap"]), dtype=np.float64)
    center_table = np.asarray(_int8_sigmoid_table(scales["center"]), dtype=np.float64)
    center_values = center_table[center[:, :, 0].astype(np.int16) + 128].reshape(-1)
    center_index = int(np.argmax(center_values * np.asarray(weights, dtype=np.float64)))
    center_y, center_x = divmod(center_index, width)
    heat_values = heat_table[heatmap.astype(np.int16) + 128]
    xs = np.arange(width, dtype=np.float64)[None, :]
    ys = np.arange(height, dtype=np.float64)[:, None]
    decoded = []
    for joint in range(17):
        reg_x = float(reg[center_y, center_x, 2 * joint]) * scales["reg"] + 0.5 + center_x
        reg_y = float(reg[center_y, center_x, 2 * joint + 1]) * scales["reg"] + 0.5 + center_y
        values = heat_values[:, :, joint] / (np.sqrt((xs - reg_x) ** 2 + (ys - reg_y) ** 2) + 1.8)
        maximum = float(np.max(values))
        # NumPy square/sqrt need not have the same last bit as Python pow/math.
        # Refine near maxima with the independent scalar expression, in row order.
        candidates = np.flatnonzero(values.reshape(-1) >= maximum * (1.0 - 32 * np.finfo(np.float64).eps))
        best_value, best_x, best_y = -math.inf, 0, 0
        for index in candidates:
            y, x = divmod(int(index), width)
            distance = math.sqrt((x - reg_x) ** 2 + (y - reg_y) ** 2) + 1.8
            value = float(heat_values[y, x, joint]) / distance
            if value > best_value:
                best_value, best_x, best_y = value, x, y
        score = float(heat_values[best_y, best_x, joint])
        if score < threshold:
            decoded.append([0.0, 0.0, 0.0])
        else:
            offset_x = float(offset[best_y, best_x, 2 * joint]) * scales["offset"]
            offset_y = float(offset[best_y, best_x, 2 * joint + 1]) * scales["offset"]
            decoded.append([(best_y + offset_y) / height, (best_x + offset_x) / width, score])
    return decoded


def load_movenet_vart_ntd(
    model_path: str | Path,
    *,
    input_value_scale: float = 1.0 / 127.5,
    input_value_offset: float = -1.0,
    model_config_path: str | Path | None = None,
    postprocess_implementation: str = "scalar",
    xir_module: Any | None = None,
    vart_module: Any | None = None,
    numpy_module: Any | None = None,
) -> VartMoveNetNtdRunner:
    """加载官方 B3136 MoveNet NTD XModel 及其同目录 prototxt。"""

    _validate_ntd_decode_implementation(postprocess_implementation)
    path = Path(model_path)
    if not path.is_file():
        raise MoveNetVartError(f"找不到 XModel：{path}")
    if xir_module is None or vart_module is None:
        try:
            import vart as imported_vart  # type: ignore[import-not-found]
            import xir as imported_xir  # type: ignore[import-not-found]
        except ImportError as exc:
            raise MoveNetVartError("当前 Python 环境没有可用的 VART/XIR 模块") from exc
        xir_module = imported_xir
        vart_module = imported_vart
    runner = _create_movenet_runner(path, xir_module, vart_module)
    contract = inspect_movenet_ntd_vart_runner(runner)
    config_path = Path(model_config_path) if model_config_path is not None else path.with_suffix(
        ".prototxt"
    )
    postprocess = (
        load_movenet_ntd_postprocess_config(config_path)
        if config_path.is_file()
        else MoveNetNtdPostprocessConfig()
    )
    return VartMoveNetNtdRunner(
        runner,
        contract,
        input_value_scale=input_value_scale,
        input_value_offset=input_value_offset,
        postprocess=postprocess,
        postprocess_implementation=postprocess_implementation,
        numpy_module=numpy_module,
    )


def _create_movenet_runner(path: Path, xir_module: Any, vart_module: Any) -> Any:
    graph = xir_module.Graph.deserialize(str(path))
    root = graph.get_root_subgraph()
    if root is None:
        raise MoveNetVartError("XModel 没有根子图")
    children = () if root.is_leaf else tuple(root.toposort_child_subgraph())
    dpu_subgraphs = tuple(
        child
        for child in children
        if child.has_attr("device") and str(child.get_attr("device")).upper() == "DPU"
    )
    if len(dpu_subgraphs) != 1:
        raise MoveNetVartError(
            f"MoveNet XModel 必须恰好包含一个 DPU 子图，实际为 {len(dpu_subgraphs)}"
        )
    return vart_module.Runner.create_runner(dpu_subgraphs[0], "run")


def _tensor_contract(tensor: Any) -> VartTensorContract:
    shape = tuple(int(value) for value in tensor.dims)
    if not shape or any(value < 1 for value in shape):
        raise MoveNetVartError(f"VART 张量形状非法：{shape}")
    try:
        fix_point = int(tensor.get_attr("fix_point"))
    except Exception as exc:
        raise MoveNetVartError(f"VART 张量 {tensor.name!r} 缺少 fix_point") from exc
    try:
        raw_dtype = tensor.dtype
    except Exception as exc:
        raise MoveNetVartError(f"VART 张量 {tensor.name!r} 缺少 dtype") from exc
    dtype = _canonical_tensor_dtype(raw_dtype)
    return VartTensorContract(str(tensor.name), shape, fix_point, dtype)


def _movenet_ntd_output_kind(name: str) -> str | None:
    lowered = name.lower()
    if "heatmap" in lowered:
        return "heatmap"
    if "center" in lowered:
        return "center"
    if "offset" in lowered:
        return "offset"
    if "reg" in lowered:
        return "reg"
    return None


def _strip_single_batch(array: Any, name: str) -> Any:
    if len(array.shape) == 4:
        if int(array.shape[0]) != 1:
            raise MoveNetVartError(f"MoveNet NTD {name} 只支持 batch=1")
        return array[0]
    return array


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))


def _canonical_tensor_dtype(raw_dtype: Any) -> str:
    """把不同 VART/XIR Python 绑定的 dtype 显示形式归一为稳定名称。"""

    text = str(raw_dtype).upper().replace(" ", "")
    if "UINT8" in text:
        return "uint8"
    if "INT8" in text or ("XINT" in text and "8" in text):
        return "int8"
    return str(raw_dtype)
