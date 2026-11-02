"""Bounded display-only interpolation of completed, causal PPS4 mesh endpoints.

The selector never changes validity, observations or scores. Its optional 50ms
lag and linear-rotation shrinkage require a separate visual/performance review.
"""
from bisect import bisect_left
from collections import deque
from dataclasses import dataclass
import math

import numpy as np


@dataclass(frozen=True)
class MeshEndpoint:
    ready_ns: int
    positions: bytes
    matrices: tuple


@dataclass(frozen=True)
class MeshInterpolation:
    left: MeshEndpoint
    right: MeshEndpoint
    fraction: float
    target_ns: int
    interpolated: bool
    reason: str


def xyz_view(data):
    if type(data) is not bytes or not data or len(data)%16:
        raise ValueError('complete immutable 16-byte-stride positions required')
    return np.frombuffer(data,dtype='<i2').reshape(-1,8)[:,:3]


def basis_array(matrices):
    return np.asarray([w.matrix.values for w in matrices],dtype=np.float64).reshape(-1,3,4)[:,:,:3]


class DelayedMeshInterpolation:
    def __init__(self,*,delay_ns=50_000_000,max_gap_ns=100_000_000,
                 max_move=.1,max_angle_degrees=30.,capacity=8):
        if (type(delay_ns) is not int or type(max_gap_ns) is not int
                or not 0<delay_ns<=max_gap_ns or type(capacity) is not int or not 2<=capacity<=8
                or not math.isfinite(max_move) or max_move<=0
                or not math.isfinite(max_angle_degrees) or not 0<max_angle_degrees<90):
            raise ValueError('positive bounded delay, geometry limits and history capacity required')
        self.delay_ns=delay_ns;self.max_gap_ns=max_gap_ns;self.max_move=max_move
        self.minimum_cosine=math.cos(math.radians(max_angle_degrees));self.capacity=capacity
        self.reset()

    def reset(self):
        self._history=deque(maxlen=self.capacity)
        self._valid=None;self._ready=None;self._display=None

    @property
    def history_size(self):return len(self._history)

    def select(self,positions,matrices,valid,*,ready_ns,display_ns,held=False):
        if (type(ready_ns) is not int or type(display_ns) is not int or ready_ns<0
                or display_ns<ready_ns):
            raise ValueError('nonnegative ready time cannot be in the display future')
        if self._ready is not None and ready_ns<self._ready:raise ValueError('ready time regressed')
        if self._display is not None and display_ns<self._display:raise ValueError('display time regressed')
        xyz=xyz_view(positions)
        valid=tuple(valid);matrices=tuple(matrices)
        if not valid or len(valid)!=len(matrices):raise ValueError('matrix and validity slots must match')
        current=MeshEndpoint(ready_ns,positions,matrices);target=display_ns-self.delay_ns
        def fallback(reason):return MeshInterpolation(current,current,1.,target,False,reason)
        self._display=display_ns;previous_ready=self._ready;self._ready=ready_ns
        reset_reason=None
        if held:reset_reason='held_geometry'
        elif not any(valid):reset_reason='no_valid_geometry'
        elif display_ns-ready_ns>self.max_gap_ns:reset_reason='stale_source'
        elif self._valid is not None and valid!=self._valid:reset_reason='validity_change'
        elif previous_ready is not None and ready_ns-previous_ready>self.max_gap_ns:reset_reason='source_gap'
        self._valid=valid
        if reset_reason is not None:
            self._history.clear()
            # Unmodified observations may seed the next interval; held/stale
            # states may not become supposedly fresh endpoints.
            if not held and any(valid) and reset_reason!='stale_source':self._history.append(current)
            return fallback(reset_reason)
        if self._history and self._history[-1].ready_ns==ready_ns:
            old=self._history[-1]
            if old.positions!=positions or old.matrices!=matrices:
                self._history.clear();self._history.append(current)
                return fallback('same_source_changed')
        else:
            if self._history:
                old=self._history[-1]
                old_xyz=xyz_view(old.positions)
                if old_xyz.shape!=xyz.shape:raise ValueError('mesh shape changed without reset')
                delta=xyz.astype(np.int32)-old_xyz.astype(np.int32)
                if np.any(np.einsum('ij,ij->i',delta.astype(np.int64),delta)> (self.max_move*16384)**2):
                    self._history.clear();self._history.append(current)
                    return fallback('vertex_motion')
                a=basis_array(old.matrices)[list(valid)];b=basis_array(matrices)[list(valid)]
                norms=np.linalg.norm(a,axis=1)*np.linalg.norm(b,axis=1)
                if (np.any(norms<=1.e-12) or
                        np.any(np.sum(a*b,axis=1)<self.minimum_cosine*norms)):
                    self._history.clear();self._history.append(current)
                    return fallback('basis_motion')
            self._history.append(current)
        # Keep the predecessor of target, plus all newer causal endpoints.
        while len(self._history)>2 and self._history[1].ready_ns<=target:self._history.popleft()
        times=[e.ready_ns for e in self._history]
        index=bisect_left(times,target)
        if index<len(times) and times[index]==target:
            endpoint=self._history[index]
            return MeshInterpolation(endpoint,endpoint,1.,target,False,'exact_endpoint')
        if index==0:
            # Hold the first causal endpoint while the deliberate display lag
            # fills. Showing current here would rewind when interpolation starts.
            endpoint=self._history[0]
            return MeshInterpolation(endpoint,endpoint,1.,target,False,'warmup_hold')
        if index==len(times):return fallback('unbracketed_target')
        left,right=self._history[index-1],self._history[index]
        if display_ns-left.ready_ns>self.max_gap_ns:
            self._history.clear();self._history.append(current)
            return fallback('stale_endpoint')
        fraction=(target-left.ready_ns)/(right.ready_ns-left.ready_ns)
        return MeshInterpolation(left,right,fraction,target,True,'interpolated')


def reference_positions(selection):
    """Float64 Q1.14 endpoint mix; not a replacement for PPS4 arithmetic."""
    a=xyz_view(selection.left.positions).astype(np.float64)/16384.
    b=xyz_view(selection.right.positions).astype(np.float64)/16384.
    return a+(b-a)*selection.fraction
