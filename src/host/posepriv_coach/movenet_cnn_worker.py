"""Opt-in Linux native four-head CNN worker plus pinned Builtin float post graph."""
from __future__ import annotations

import json
from contextlib import contextmanager
import queue
import math
import os
from pathlib import Path
import selectors
import signal
import struct
import subprocess
import sys
import tempfile
from time import monotonic, monotonic_ns
from typing import Any

from . import cnn_worker_protocol as wire
from .capture import CapturedFrame
from .movenet_preprocess import MoveNetInference, OpenCVMoveNetPreprocessor


class MoveNetCnnWorkerError(RuntimeError):
    """A model, protocol, inference or bounded child-lifecycle failure."""


_POST_ROLES = {
    'center': 'center_logits', 'heatmap': 'keypoint_heatmap_logits',
    'offset': 'keypoint_offsets_yx', 'regression': 'keypoint_regression_yx',
}
_SOURCE_IDS = {'front': 1, 'side': 2}

# Complete RGB-byte -> signed Q7 map, validated against the actual FixNeuron.
# Every channel shares this map; 255 saturates at127. No NTD Q6 conversion.
_Q7_RGB_LUT = (
    -128, -127, -126, -125, -124, -123, -122, -121, -120, -119, -118, -117, -116, -115, -114, -113,
    -112, -111, -110, -109, -108, -107, -106, -105, -104, -103, -102, -101, -100, -99, -98, -97,
    -96, -95, -94, -93, -92, -91, -90, -89, -88, -87, -86, -85, -84, -83, -82, -81,
    -80, -79, -78, -77, -76, -75, -74, -73, -72, -71, -70, -69, -68, -67, -66, -65,
    -64, -63, -62, -61, -60, -59, -58, -57, -56, -55, -54, -53, -52, -51, -50, -49,
    -48, -47, -46, -45, -44, -43, -42, -41, -40, -39, -38, -37, -36, -35, -34, -33,
    -32, -31, -30, -29, -28, -27, -26, -25, -24, -23, -22, -21, -20, -19, -18, -17,
    -16, -15, -14, -13, -12, -11, -10, -9, -8, -7, -6, -5, -4, -3, -2, -1,
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16,
    17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32,
    33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48,
    49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64,
    65, 66, 67, 68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 78, 79, 80,
    81, 82, 83, 84, 85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 95, 96,
    97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112,
    113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 126, 127, 127,
)


def _validate_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get('input'), dict):
        raise MoveNetCnnWorkerError('CNN mapping must contain input and exact head roles')
    inp = value['input']
    if not isinstance(inp.get('name'), str) or not inp['name'] or type(inp.get('fix_point')) is not int or inp['fix_point'] != 7:
        raise MoveNetCnnWorkerError('CNN input mapping must name the proven Q7 input')
    heads = value.get('heads')
    if not isinstance(heads, dict) or set(heads) != set(wire.ROLES):
        raise MoveNetCnnWorkerError('CNN mapping must contain exactly four named head roles')
    names = set()
    for role in wire.ROLES:
        head = heads[role]
        if not isinstance(head, dict):
            raise MoveNetCnnWorkerError('Invalid CNN head mapping')
        name, fix = head.get('name'), head.get('fix_point')
        if not isinstance(name, str) or not 0 < len(name.encode('utf-8')) <= 512 or name in names:
            raise MoveNetCnnWorkerError('CNN head mapping exact names must be unique')
        names.add(name)
        if type(fix) is not int or not -31 <= fix <= 31:
            raise MoveNetCnnWorkerError('CNN head mapping fix_point must be in [-31,31]')
    return value


class _WorkerProcess:
    """One outstanding request, nonblocking bounded I/O, only its own process group."""

    def __init__(self, command: list[str], mapping: dict[str, Any], timeout: float, *, on_process_started=None) -> None:
        self.command = command
        self._on_process_started=on_process_started
        self.mapping = _validate_mapping(mapping)
        self.timeout = timeout
        self.process: subprocess.Popen[bytes] | None = None
        self.metadata: dict[str, Any] | None = None
        self.frames = 0
        self.closed = False
        self.complete = False
        self.error: str | None = None
        self._stderr: Any = None
        self._pgid: int | None = None
        self._stderr_tail = ''

    def _wait(self, fd: int, event: int, deadline: float) -> None:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError('CNN worker I/O deadline expired')
        with selectors.DefaultSelector() as selector:
            selector.register(fd, event)
            if not selector.select(remaining):
                raise TimeoutError('CNN worker I/O deadline expired')

    def _read(self, size: int, deadline: float) -> bytes:
        assert self.process is not None and self.process.stdout is not None
        data = bytearray()
        fd = self.process.stdout.fileno()
        while len(data) < size:
            if monotonic() >= deadline:
                raise TimeoutError('CNN worker read deadline expired')
            try:
                chunk = os.read(fd, size - len(data))
            except BlockingIOError:
                self._wait(fd, selectors.EVENT_READ, deadline)
                continue
            except InterruptedError:
                continue
            if not chunk:
                raise ValueError('truncated CNN worker stream')
            data.extend(chunk)
        return bytes(data)

    def _write(self, payload: bytes, deadline: float) -> None:
        assert self.process is not None and self.process.stdin is not None
        fd = self.process.stdin.fileno()
        pending = memoryview(payload)
        while pending:
            if monotonic() >= deadline:
                raise TimeoutError('CNN worker write deadline expired')
            try:
                count = os.write(fd, pending)
            except BlockingIOError:
                self._wait(fd, selectors.EVENT_WRITE, deadline)
                continue
            except InterruptedError:
                continue
            if count <= 0:
                raise ValueError('CNN worker write made no progress')
            pending = pending[count:]

    def _record(self, deadline: float) -> tuple[int, bytes]:
        kind, length = wire.parse_header(self._read(wire.HEADER.size, deadline))
        return kind, self._read(length, deadline)

    def _start(self, deadline: float) -> None:
        self._stderr = tempfile.TemporaryFile(mode='w+b')
        self.process = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self._stderr, bufsize=0, start_new_session=True,
        )
        self._pgid = self.process.pid
        if self._on_process_started is not None:self._on_process_started(self.process.pid)
        assert self.process.stdin is not None and self.process.stdout is not None
        os.set_blocking(self.process.stdin.fileno(), False)
        os.set_blocking(self.process.stdout.fileno(), False)
        kind, payload = self._record(deadline)
        if kind != 3:
            raise ValueError('CNN worker must send metadata first')
        meta = wire.metadata(payload)
        # Worker v1 omits input.name. The static XIR mapping is external provenance.
        if 'name' in meta['input'] and meta['input']['name'] != self.mapping['input']['name']:
            raise ValueError('CNN worker input name mismatch')
        for head in meta['heads']:
            expected = self.mapping['heads'][head['role']]
            if head['name'] != expected['name'] or head['fix_point'] != expected['fix_point']:
                raise ValueError('CNN worker exact head name/fix_point mismatch')
        self.metadata = meta

    def _signal(self, sig: int) -> None:
        # start_new_session made this PID the new PGID; never signal caller groups.
        if self._pgid is not None and self._pgid != os.getpgrp():
            try:
                os.killpg(self._pgid, sig)
            except ProcessLookupError:
                pass

    def _cleanup(self) -> None:
        if self.process is not None:
            self._signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                pass
            self._signal(signal.SIGKILL)
            try:
                self.process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                pass  # D-state recovery cannot be promised.
            for stream in (self.process.stdin, self.process.stdout):
                if stream is not None:
                    stream.close()
            self._pgid = None
        if self._stderr is not None:
            self._stderr.seek(0, os.SEEK_END)
            length = self._stderr.tell()
            self._stderr.seek(max(0, length - 16384))
            self._stderr_tail = self._stderr.read().decode('utf-8', errors='replace')
            self._stderr.close()
            self._stderr = None

    def _fail(self, exc: BaseException) -> MoveNetCnnWorkerError:
        self._cleanup()
        self.closed = True
        self.error = str(exc)
        suffix = '\nworker stderr (tail):\n' + self._stderr_tail if self._stderr_tail else ''
        return MoveNetCnnWorkerError(self.error + suffix)

    def infer(self, identity: tuple[int, int, int, int, int], pixels: bytes) -> dict[str, bytes]:
        if self.closed:
            raise MoveNetCnnWorkerError(self.error or 'CNN worker is closed')
        deadline = monotonic() + self.timeout
        try:
            payload = wire.request(*identity, pixels)
            if self.process is None:
                self._start(deadline)
            self._write(payload, deadline)
            kind, response = self._record(deadline)
            if kind != 2:
                raise ValueError('CNN worker expected one response frame')
            actual, heads = wire.response(response)
            if actual != identity:
                raise ValueError('CNN worker source ID/frame/ns/geometry mismatch')
            self.frames += 1
            return heads
        except BaseException as exc:
            error = self._fail(exc)
            if not isinstance(exc, Exception):
                raise
            raise error from exc

    @contextmanager
    def pipelined(self,identities,pixels,executor):
        """Bounded full-duplex I/O; caller consumes responses while postprocessing."""
        if self.closed:raise MoveNetCnnWorkerError(self.error or 'CNN worker is closed')
        identities=tuple(identities);deadline=monotonic()+self.timeout;writer=None;read_count=0
        writer_failures=queue.Queue(maxsize=1);reader_transport_failure=False
        try:
            if self.process is None:self._start(deadline)
            def write_all():
                try:
                    for identity,value in zip(identities,pixels,strict=True):
                        self._write(wire.request(*identity,value),deadline)
                except BaseException as exc:
                    # Publish before EOF can wake the reader; Future completion is later.
                    writer_failures.put_nowait(exc)
                    try:self.process.stdin.close()
                    except OSError:pass
                    raise
            writer=executor.submit(write_all)
            def responses():
                nonlocal read_count,reader_transport_failure
                for identity in identities:
                    try:kind,response=self._record(deadline)
                    except (ValueError,TimeoutError)as exc:
                        reader_transport_failure=isinstance(exc,TimeoutError)or str(exc)=='truncated CNN worker stream'
                        raise
                    if kind!=2:
                        reader_transport_failure=kind==4
                        raise ValueError('CNN worker expected pipelined response')
                    actual,heads=wire.response(response)
                    if actual!=identity:raise ValueError('CNN worker pipelined source identity mismatch')
                    self.frames+=1;read_count+=1
                    yield heads
            yield responses()
            if read_count!=len(identities):raise ValueError('CNN worker responses were not fully consumed')
            writer.result(timeout=max(0,deadline-monotonic()))
        except BaseException as exc:
            cause=exc
            if reader_transport_failure and isinstance(exc,Exception):
                try:cause=writer_failures.get_nowait()
                except queue.Empty:pass
            error=self._fail(cause)
            if writer is not None:
                try:writer.result(timeout=.6)
                except BaseException:pass
            if not isinstance(exc,Exception):raise
            if not isinstance(cause,Exception):raise cause
            raise error from cause

    def close(self) -> None:
        if self.closed:
            return
        if self.process is None:
            self.closed = True
            self.complete = True  # Lazy, unused runner never starts DPU.
            return
        deadline = monotonic() + self.timeout
        try:
            assert self.process.stdin is not None and self.process.stdout is not None
            self.process.stdin.close()
            kind, footer = self._record(deadline)
            if kind != 4 or struct.unpack('<Q', footer)[0] != self.frames:
                raise ValueError('CNN worker footer/frame count mismatch')
            fd = self.process.stdout.fileno()
            while True:
                if monotonic() >= deadline:
                    raise TimeoutError('CNN worker final EOF deadline expired')
                try:
                    trailing = os.read(fd, 1)
                except BlockingIOError:
                    self._wait(fd, selectors.EVENT_READ, deadline)
                    continue
                if trailing:
                    raise ValueError('CNN worker trailing bytes after footer')
                break
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError('CNN worker exit deadline expired')
            code = self.process.wait(timeout=remaining)
            if code != 0:
                raise ValueError(f'CNN worker exit status {code}')
            self.complete = True
            self.closed = True
            self._cleanup()
        except BaseException as exc:
            error = self._fail(exc)
            if not isinstance(exc, Exception):
                raise
            raise error from exc

    def stats(self) -> dict[str, Any]:
        return {
            'frames': self.frames, 'started': self.process is not None,
            'closed': self.closed, 'complete': self.complete,
            'input_name_provenance': 'external static XIR mapping; worker v1 omits runtime name',
            'metadata': self.metadata, 'stderr_tail': self._stderr_tail, 'error': self.error,
        }


class MoveNetCnnWorkerRunner:
    """Frame-aware pad/Q7/native-CNN/float-post callable; CPU post is explicit Builtin."""

    input_width = 192
    input_height = 192
    input_dtype = 'uint8'

    def __init__(self, transport: _WorkerProcess, interpreter: Any, post_mapping: Any,
                 *, numpy_module: Any, cv2_module: Any | None = None,
                 artifact_paths: dict[str, str] | None = None, raw_post_factory=None) -> None:
        self._transport = transport
        self._post = interpreter
        self._raw_post_factory=raw_post_factory;self._raw_post=None
        self._post_timing_key='builtin_post'if raw_post_factory is None else'native_raw_post'
        self._numpy = numpy_module
        if cv2_module is None:
            try:
                import cv2 as cv2_module
            except ImportError as exc:
                raise MoveNetCnnWorkerError('Native CNN worker requires OpenCV LUT preprocessing') from exc
        if not callable(getattr(cv2_module, 'LUT', None)):
            raise MoveNetCnnWorkerError('Native CNN worker requires cv2.LUT; no float fallback')
        self._cv2 = cv2_module
        self._preprocessor = OpenCVMoveNetPreprocessor(cv2_module=self._cv2, resize_before_color=True)
        self._q7_lut = numpy_module.asarray(_Q7_RGB_LUT, dtype=numpy_module.int8)
        self._q7_lut.flags.writeable = False
        self._q7_out = numpy_module.empty((192, 192, 3), dtype=numpy_module.int8)
        self.artifact_paths = artifact_paths or {}
        self._valid_frames = 0
        self._request_post_total_ns = 0
        self._request_post_max_ns = 0
        self.last_timing_ns: dict[str, int] = {}
        self.last_pair_timing_ns=[];self._paired_calls=0
        interpreter.allocate_tensors()
        inputs, outputs = interpreter.get_input_details(), interpreter.get_output_details()
        if not isinstance(post_mapping, list) or len(post_mapping) != 4:
            raise MoveNetCnnWorkerError('Post mapping must contain four exact named roles')
        if len(inputs) != 4 or len(outputs) != 1:
            raise MoveNetCnnWorkerError('Post model requires four inputs and one output')
        self._post_inputs = {}
        used = set()
        for role, channels in zip(wire.ROLES, wire.CHANNELS, strict=True):
            entries = [m for m in post_mapping if isinstance(m, dict) and m.get('role') == _POST_ROLES[role]]
            if len(entries) != 1:
                raise MoveNetCnnWorkerError('Post exact role mapping is missing or ambiguous')
            name = entries[0].get('tflite_name')
            matches = [d for d in inputs if d.get('name') == name]
            if len(matches) != 1:
                raise MoveNetCnnWorkerError('Post exact input name mismatch')
            detail = matches[0]
            index = int(detail['index'])
            if index in used or tuple(detail['shape']) != (1, 48, 48, channels) or self._numpy.dtype(detail['dtype']).name != 'float32':
                raise MoveNetCnnWorkerError('Post input shape/dtype/index mismatch')
            used.add(index)
            self._post_inputs[role] = index
        self._output = outputs[0]
        if tuple(self._output['shape']) != (1, 1, 17, 3) or self._numpy.dtype(self._output['dtype']).name != 'float32':
            raise MoveNetCnnWorkerError('Post output must be float32[1,1,17,3]')
        if hasattr(interpreter, '_get_ops_details'):
            if any(d['op_name'] in ('CONV_2D', 'DEPTHWISE_CONV_2D', 'CUSTOM', 'DELEGATE') for d in interpreter._get_ops_details()):
                raise MoveNetCnnWorkerError('Post graph must be Builtin-only without CNN/delegates')

    def _prepare_request(self,frame):
        prepared=self._preprocessor.prepare(frame);np=self._numpy;rgb=np.asarray(prepared.pixels)
        if tuple(rgb.shape)!=(192,192,3)or np.dtype(rgb.dtype).name!='uint8':
            raise ValueError('Prepared worker input must be uint8 RGB192')
        mapped=self._cv2.LUT(rgb,self._q7_lut,dst=self._q7_out)
        if mapped is not self._q7_out:raise ValueError('cv2.LUT did not use the private Q7 output buffer')
        return self._q7_out.tobytes(),prepared.transform

    def _post_heads(self,heads):
        np=self._numpy;received=monotonic_ns()
        assert self._transport.metadata is not None
        if self._raw_post_factory is not None:
            if self._raw_post is None:self._raw_post=self._raw_post_factory(self._transport.metadata)
            points=self._raw_post(heads)
            if tuple(points.shape)!=(1,1,17,3)or np.dtype(points.dtype).name!='float32'or not bool(np.isfinite(points).all()):
                raise ValueError('Invalid/nonfinite native post output')
            return points,0,monotonic_ns()-received
        for detail in self._transport.metadata['heads']:
            value=np.frombuffer(heads[detail['role']],dtype=np.int8).reshape(detail['shape'])
            value=value.astype(np.float32)*np.float32(2.0**(-detail['fix_point']))
            self._post.set_tensor(self._post_inputs[detail['role']],value)
        post_started=monotonic_ns();self._post.invoke();points=self._post.get_tensor(int(self._output['index']))
        if tuple(points.shape)!=(1,1,17,3)or np.dtype(points.dtype).name!='float32'or not bool(np.isfinite(points).all()):
            raise ValueError('Invalid/nonfinite post output')
        return points,post_started-received,monotonic_ns()-post_started

    def __call__(self, frame: CapturedFrame[Any]) -> MoveNetInference:
        try:
            preprocessing_started_ns=monotonic_ns();pixels,transform=self._prepare_request(frame)
            identity=(frame.frame_id,frame.timestamp_ns,_SOURCE_IDS[frame.view.value],frame.width,frame.height)
            started_ns=monotonic_ns();heads=self._transport.infer(identity,pixels);received_ns=monotonic_ns()
            points,dequant,builtin=self._post_heads(heads);elapsed_ns=monotonic_ns()-started_ns
            self.last_timing_ns=dict(preprocess_q7=started_ns-preprocessing_started_ns,
                worker_request=received_ns-started_ns,dequant_set=dequant,**{self._post_timing_key:builtin})
            self._valid_frames+=1;self._request_post_total_ns+=elapsed_ns
            self._request_post_max_ns=max(self._request_post_max_ns,elapsed_ns)
            return MoveNetInference(output=points,transform=transform)
        except BaseException as exc:
            if isinstance(exc,MoveNetCnnWorkerError):raise
            error=self._transport._fail(exc)
            if not isinstance(exc,Exception):raise
            raise error from exc

    def infer_pair(self,frames,executor):
        """Only writer touches preprocessing; only reader touches the Interpreter."""
        frames=tuple(frames)
        if len(frames)!=2:raise ValueError('Paired worker requires exactly two frames')
        prepared=queue.Queue(maxsize=2);results=[];timings=[];started=monotonic_ns()
        identities=[(f.frame_id,f.timestamp_ns,_SOURCE_IDS[f.view.value],f.width,f.height)for f in frames]
        def pixels():
            for frame in frames:
                begin=monotonic_ns();value,transform=self._prepare_request(frame)
                prepared.put_nowait((transform,monotonic_ns()-begin))
                yield value
        with self._transport.pipelined(identities,pixels(),executor)as responses:
            for heads in responses:
                transform,preprocess=prepared.get_nowait()
                points,dequant,builtin=self._post_heads(heads)
                results.append(MoveNetInference(output=points,transform=transform))
                timings.append(dict(preprocess_q7=preprocess,dequant_set=dequant,**{self._post_timing_key:builtin}))
                self._valid_frames+=1
        elapsed=monotonic_ns()-started;self._paired_calls+=1
        self._request_post_total_ns+=elapsed;self._request_post_max_ns=max(self._request_post_max_ns,elapsed)
        self.last_pair_timing_ns=timings
        return results

    def start(self) -> None:
        """Warm this runtime before concurrent jobs; preserve bounded owned cleanup."""
        if self._transport.closed:raise MoveNetCnnWorkerError('CNN worker is closed')
        if self._transport.process is not None:return
        try:self._transport._start(monotonic()+self._transport.timeout)
        except BaseException as exc:
            error=self._transport._fail(exc)
            if not isinstance(exc,Exception):raise
            raise error from exc

    def close(self) -> None:
        self._transport.close()

    def stats(self) -> dict[str, Any]:
        return {**self._transport.stats(), 'worker_response_frames': self._transport.frames,
                'frames': self._valid_frames, 'q7_backend': 'opencv_lut_int8_private_out_owned_bytes',
                'post_backend':'builtin'if self._raw_post_factory is None else'native_raw_exact',
                'request_post_wall_total_ns': self._request_post_total_ns,
                'request_post_wall_max_ns': self._request_post_max_ns,
                'paired_calls': self._paired_calls,
                'timing_boundary': ('paired preparation/write/read/post overlap wall, not pure DPU latency'if self._paired_calls else
                    'request write/response read + exact CPU post; excludes pad/Q7; not pure DPU latency'),
                'artifact_paths': dict(self.artifact_paths)}


def load_movenet_cnn_worker(
    worker_path: str | Path, model_path: str | Path, mapping_path: str | Path,
    post_model_path: str | Path, post_mapping_path: str | Path, *,
    timeout_seconds: float = 5, numpy_module: Any | None = None,
    cv2_module: Any | None = None,on_process_started=None,
    native_post_library: str | Path | None = None,native_post_contract: str | Path | None = None,
) -> MoveNetCnnWorkerRunner:
    """Validate artifacts/post graph now; create the native subprocess only on first frame."""
    if os.name != 'posix' or not sys.platform.startswith('linux'):
        raise MoveNetCnnWorkerError('Native CNN worker requires POSIX Linux')
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError('timeout_seconds must be finite and positive')
    if (native_post_library is None)!=(native_post_contract is None):
        raise ValueError('native post library and trusted fixed-model contract must be provided together')
    paths = {name: str(Path(path).resolve()) for name, path in (
        ('worker', worker_path), ('model', model_path), ('mapping', mapping_path),
        ('post_model', post_model_path), ('post_mapping', post_mapping_path),
    )}
    if native_post_library is not None:
        paths.update(native_post_library=str(Path(native_post_library).resolve()),
                     native_post_contract=str(Path(native_post_contract).resolve()))
    for name, path in paths.items():
        if not Path(path).is_file():
            raise MoveNetCnnWorkerError(f'Missing {name} artifact: {path}')
    if not os.access(paths['worker'], os.X_OK):
        raise MoveNetCnnWorkerError('Native CNN worker is not executable')
    try:
        mapping = _validate_mapping(json.loads(Path(mapping_path).read_text(encoding='utf-8')))
        post_mapping = json.loads(Path(post_mapping_path).read_text(encoding='utf-8'))
        if numpy_module is None:
            import numpy as numpy_module
        try:
            from ai_edge_litert.interpreter import Interpreter, OpResolverType
        except ImportError:
            try:
                from tflite_runtime.interpreter import Interpreter, OpResolverType
            except ImportError:
                import tensorflow as tf
                Interpreter, OpResolverType = tf.lite.Interpreter, tf.lite.experimental.OpResolverType
        post = Interpreter(model_path=paths['post_model'], num_threads=2,
                           experimental_op_resolver_type=OpResolverType.BUILTIN_WITHOUT_DEFAULT_DELEGATES)
        command = [paths['worker'], paths['model'], *[mapping['heads'][role]['name'] for role in wire.ROLES]]
        transport = _WorkerProcess(command, mapping, float(timeout_seconds),on_process_started=on_process_started)
        raw_post_factory=None
        if native_post_library is not None:
            from .native_movenet_post import NativeRawPost
            def raw_post_factory(metadata):
                return NativeRawPost(paths['native_post_library'],paths['post_model'],post_mapping,
                    metadata,mapping,paths['native_post_contract'],Interpreter,OpResolverType)
        return MoveNetCnnWorkerRunner(transport, post, post_mapping, numpy_module=numpy_module,
                                      cv2_module=cv2_module, artifact_paths=paths,raw_post_factory=raw_post_factory)
    except MoveNetCnnWorkerError:
        raise
    except Exception as exc:
        raise MoveNetCnnWorkerError(f'Cannot load native CNN worker/post runtime: {exc}') from exc
