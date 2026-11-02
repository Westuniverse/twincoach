"""Optional TensorFlow Lite runtime boundary for MoveNet SinglePose Lightning."""

from __future__ import annotations

from pathlib import Path
from typing import Any


class MoveNetTFLiteError(RuntimeError):
    """Report missing runtimes or an incompatible MoveNet tensor contract."""


class MoveNetTFLiteRunner:
    """Run an official uint8 or compatible int32 MoveNet TFLite model."""

    def __init__(self, interpreter: Any, *, numpy_module: Any | None = None) -> None:
        if numpy_module is None:
            try:
                import numpy as numpy_module  # type: ignore[import-not-found, no-redef]
            except ImportError as exc:
                raise MoveNetTFLiteError("MoveNet TFLite 推理需要 NumPy") from exc

        self._interpreter = interpreter
        self._numpy = numpy_module
        interpreter.allocate_tensors()
        inputs = interpreter.get_input_details()
        outputs = interpreter.get_output_details()
        if len(inputs) != 1 or len(outputs) != 1:
            raise MoveNetTFLiteError("MoveNet TFLite 模型必须恰好具有一个输入和一个输出")

        self._input = inputs[0]
        self._output = outputs[0]
        shape = tuple(int(value) for value in self._input["shape"])
        if len(shape) != 4 or shape[0] != 1 or shape[3] != 3:
            raise MoveNetTFLiteError(
                f"MoveNet 输入形状必须是 [1,H,W,3]，实际为 {shape}"
            )
        dtype_name = self._numpy.dtype(self._input["dtype"]).name
        if dtype_name not in ("uint8", "int32"):
            raise MoveNetTFLiteError(
                "MoveNet TFLite 输入必须是官方 uint8 或兼容的 int32，"
                f"实际为 {dtype_name}"
            )
        self.input_height = shape[1]
        self.input_width = shape[2]
        self.input_dtype = dtype_name
        self._input_shape = shape

    def __call__(self, pixels: Any) -> Any:
        array = self._numpy.asarray(pixels)
        expected = (self.input_height, self.input_width, 3)
        if tuple(array.shape) != expected:
            raise MoveNetTFLiteError(
                f"预处理图像形状必须是 {expected}，实际为 {tuple(array.shape)}"
            )
        tensor = array.astype(self._input["dtype"], copy=False).reshape(self._input_shape)
        self._interpreter.set_tensor(int(self._input["index"]), tensor)
        self._interpreter.invoke()
        output = self._interpreter.get_tensor(int(self._output["index"]))
        shape = tuple(int(value) for value in getattr(output, "shape", ()))
        if shape != (1, 1, 17, 3):
            raise MoveNetTFLiteError(
                f"MoveNet 输出形状必须是 [1,1,17,3]，实际为 {shape}"
            )
        return output


def load_movenet_tflite(
    model_path: str | Path,
    *,
    num_threads: int = 2,
    numpy_module: Any | None = None,
) -> MoveNetTFLiteRunner:
    """Load a model with LiteRT, tflite-runtime, or TensorFlow."""

    path = Path(model_path)
    if not path.is_file():
        raise MoveNetTFLiteError(f"找不到 MoveNet 模型：{path}")
    if num_threads < 1:
        raise ValueError("num_threads must be positive")

    interpreter_type: Any
    try:
        from ai_edge_litert.interpreter import Interpreter as interpreter_type
    except ImportError:
        try:
            from tflite_runtime.interpreter import Interpreter as interpreter_type
        except ImportError as exc:
            try:
                import tensorflow as tensorflow_module
            except ImportError:
                raise MoveNetTFLiteError(
                    "未找到 LiteRT/TFLite 运行时；开发主机可安装 ai-edge-litert，"
                    "板端也可使用镜像提供的 tflite-runtime 或 tensorflow"
                ) from exc
            interpreter_type = tensorflow_module.lite.Interpreter

    interpreter = interpreter_type(model_path=str(path), num_threads=num_threads)
    return MoveNetTFLiteRunner(interpreter, numpy_module=numpy_module)
