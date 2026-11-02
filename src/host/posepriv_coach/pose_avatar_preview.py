"""Retarget the displayed pose pair without claiming camera triangulation."""
from __future__ import annotations

import math
from types import SimpleNamespace

from .avatar_pipeline import AvatarFramePlan
from .joint_filter import AdaptiveJointFilter, JointFilterConfig
from .preview_depth import PreviewDepthTracker
from .preview_head import PreviewHeadDirection
from .preview_head_face_axis import current_head_face_axis
from .preview_core_support import current_core_support
from .avatar_skinning import MATRIX_COUNT, AffineMatrixQ1_14, MatrixWrite, compose_affine_q1_14
from .display_hands import HAND_LINKS, palm_down_x_axis, continuous_palm_down_x_axis
from .skeleton_pose import (DEFAULT_BONE_LINKS, SkeletonJoint3D, bone_affine_matrix_q1_14,
                            build_bone_segments, build_virtual_joints, stable_bone_local_x_axis)


class PoseAvatarPreviewPlanner:
    """Front image XY plus optional side-relative depth, in display units."""

    def __init__(self, *, hand_bones=False, depth_stability=False, palm_roll_stability=False, head_stability=False, head_depth=False, core_view_support=False, head_face_axis=False, affine_composer=None,bone_affine_builder=None):
        if head_face_axis and head_depth:
            raise ValueError('head-face-axis and head-depth are separate head targets')
        if affine_composer is not None and not callable(affine_composer):
            raise TypeError('affine composer must be callable')
        self._compose_affine = compose_affine_q1_14 if affine_composer is None else affine_composer
        self._bone_affine = bone_affine_matrix_q1_14 if bone_affine_builder is None else bone_affine_builder
        if not callable(self._bone_affine):raise TypeError('bone affine builder must be callable')
        self.config = SimpleNamespace(bone_links=DEFAULT_BONE_LINKS + (HAND_LINKS if hand_bones else ()),
                                      triangulation_backend="video-pose-preview")
        self._axes = [None] * MATRIX_COUNT
        self._identity_matrix = AffineMatrixQ1_14.identity()
        self._hand_directions = [None] * MATRIX_COUNT
        self._palm_roll_stability = palm_roll_stability
        self._head_depth = head_depth
        self._head_face_axis = head_face_axis
        self.last_head_face_axis = None
        self.head_tracker = PreviewHeadDirection() if head_stability else None
        self.joint_filter = AdaptiveJointFilter(JointFilterConfig(
            minimum_cutoff_hz=1.5, motion_beta=1.6))
        self._core_view_support = core_view_support
        self._supported_filter = AdaptiveJointFilter(self.joint_filter.config) if core_view_support else None
        self.last_core_support = None
        self._tracked = set()
        self._timestamp_ns = None
        self._side_forward_sign = None
        self._last_preview_plan = None
        self.depth_tracker = PreviewDepthTracker() if depth_stability else None

    def current_face_source(self,plan):
        """Freeze the sign only for the exact plan returned by the last build."""
        if self._last_preview_plan is None or plan is not self._last_preview_plan:
            raise ValueError('face source must follow the matching preview build')
        from .preview_face_source import FaceAxisSource,pair_source_key
        return FaceAxisSource(pair_source_key(plan.pair),self._timestamp_ns,self._side_forward_sign)

    @staticmethod
    def _visible(point, minimum_confidence=.5):
        return (point is not None and point.confidence >= minimum_confidence
                and math.isfinite(point.x) and math.isfinite(point.y)
                and 0 <= point.x <= 1 and 0 <= point.y <= 1)

    @property
    def bone_axes(self):
        """Current source axes; display adapters cannot mutate planner history."""
        return tuple(self._axes)

    def build(self, pair, *, vertex_requests=(), bone_bind_lengths_q1_14=(),
              inverse_bind_matrices_q1_14=(), bone_topology_indices=(), display_timestamp_ns=None):
        self._last_preview_plan = None
        front, side = pair.front, pair.side
        timestamp_ns = (front.timestamp_ns + side.timestamp_ns) // 2
        previous_timestamp_ns = self._timestamp_ns
        if self._timestamp_ns is not None and timestamp_ns < self._timestamp_ns:
            raise ValueError("non-monotonic preview timestamp")
        if self._timestamp_ns is not None and timestamp_ns - self._timestamp_ns > 250_000_000:
            self._tracked.clear()
            self._side_forward_sign = None
            self._axes = [None] * MATRIX_COUNT
            self._hand_directions = [None] * MATRIX_COUNT
            if self.depth_tracker is not None:
                self.depth_tracker.reset()
        self._timestamp_ns = timestamp_ns
        front_size = max(front.source_width, front.source_height)
        side_size = max(side.source_width, side.source_height)
        hips = [side.keypoints.get(name) for name in ("left_hip", "right_hip")]
        depth_available = all(self._visible(point) for point in hips)
        depth_origin = sum(point.x for point in hips) / 2 if depth_available else .5
        if self._side_forward_sign is None:
            face = side.keypoints.get("nose")
            shoulders = [side.keypoints.get(name) for name in ("left_shoulder", "right_shoulder")]
            if self._visible(face) and all(self._visible(p) for p in shoulders):
                facing = face.x - sum(p.x for p in shoulders) / 2
                if abs(facing) >= .015:
                    self._side_forward_sign = math.copysign(1., facing)
        front_visible = {name: point for name, point in front.keypoints.items()
                         if self._visible(point, .35 if name in self._tracked else .5)}
        trusted_names = set(front_visible)
        if self._core_view_support:
            self.last_core_support = current_core_support(pair,trusted_names,
                forward_sign=self._side_forward_sign,display_timestamp_ns=display_timestamp_ns)
            front_visible.update((name,front.keypoints[name]) for name in self.last_core_support.names)
        supported_names = self.last_core_support.names if self.last_core_support is not None else ()
        stable_depth = (self.depth_tracker.update(side, trusted_names if self._core_view_support else front_visible,
                        forward_sign=self._side_forward_sign, timestamp_ns=timestamp_ns)
                        if self.depth_tracker is not None else None)
        head_depth_available = False
        if self._head_depth:
            head_depth_available = (
                all(name in front_visible and (name in supported_names or
                    name in stable_depth and stable_depth[name].source != "flat")
                    for name in ("nose", "left_shoulder", "right_shoulder"))
                if stable_depth is not None else
                depth_available and self._side_forward_sign is not None and
                all(self._visible(side.keypoints.get(name))
                    for name in ("nose", "left_shoulder", "right_shoulder")))
        observed = {}
        for name, point in front_visible.items():
            profile = side.keypoints.get(name)
            # The avatar faces negative Z. Side-camera image right is not back.
            depth = ((depth_origin - profile.x) * self._side_forward_sign * 2 * side.source_width / side_size
                     if depth_available and self._side_forward_sign is not None and self._visible(profile) else 0.)
            if stable_depth is not None and name in stable_depth:
                depth = stable_depth[name].value
            observed[name] = SkeletonJoint3D(
                name, ((point.x - .5) * 2 * front.source_width / front_size,
                       (.5 - point.y) * 2 * front.source_height / front_size, depth),
                point.confidence, math.inf, True)
        self._tracked = trusted_names
        if self._supported_filter is None:
            observed = self.joint_filter.update(observed, timestamp_ns=timestamp_ns)
        else:
            supported = {name:joint for name,joint in observed.items() if name not in trusted_names}
            observed = self.joint_filter.update({name:joint for name,joint in observed.items()
                if name in trusted_names}, timestamp_ns=timestamp_ns)
            observed.update(self._supported_filter.update(supported,timestamp_ns=timestamp_ns))
        joints = build_virtual_joints(observed)
        # MoveNet's shoulder midpoint is not the base of a human head. Retarget
        # the shirt to the shoulders and leave a short neck above them.
        if "neck" in joints and "pelvis" in joints:
            shoulder = joints["neck"]
            torso = math.dist(shoulder.position, joints["pelvis"].position)
            for name, amount in (("chest", -.08), ("neck", .06)):
                joints[name] = SkeletonJoint3D(
                    name, (shoulder.position[0], shoulder.position[1] + torso * amount,
                           shoulder.position[2]), shoulder.confidence, math.inf, True)
            if "nose" in joints:
                nose = joints["nose"]
                joints["nose"] = SkeletonJoint3D(
                    "nose", (nose.position[0], nose.position[1] + torso * .16,
                             nose.position[2] if self._head_depth and head_depth_available else shoulder.position[2]),
                    nose.confidence, math.inf, True)
        if self._head_face_axis:
            nose, self.last_head_face_axis = current_head_face_axis(pair, joints.get('neck'), joints.get('nose'),
                forward_sign=self._side_forward_sign, display_timestamp_ns=display_timestamp_ns)
            if nose is not None:
                joints['nose'] = nose
        if self.head_tracker is not None:
            nose = self.head_tracker.update(joints.get("neck"), joints.get("nose"), timestamp_ns=timestamp_ns)
            if nose is not None:
                joints["nose"] = nose
        links = self.config.bone_links
        indices = bone_topology_indices or tuple(range(len(links)))
        bind_lengths = {links[index].name:bone_bind_lengths_q1_14[slot]/16384
                        for slot,index in enumerate(indices) if slot<len(bone_bind_lengths_q1_14)}
        for link in HAND_LINKS if len(links)>len(DEFAULT_BONE_LINKS) else ():
            wrist=joints.get(link.parent)
            elbow=joints.get(link.parent.replace('wrist','elbow'))
            length=bind_lengths.get(link.name,0.)
            if wrist is None or elbow is None or length<=0: continue
            forearm=tuple(w-e for w,e in zip(wrist.position,elbow.position))
            magnitude=math.sqrt(sum(v*v for v in forearm))
            if magnitude<=1e-6: continue
            joints[link.child]=SkeletonJoint3D(link.child,
                tuple(w+v*length/magnitude for w,v in zip(wrist.position,forearm)),
                min(wrist.confidence,elbow.confidence),math.inf,True)
        segments = build_bone_segments(joints,links)
        matrices = [self._identity_matrix] * MATRIX_COUNT
        valid = [False] * MATRIX_COUNT
        next_hand_directions = [None] * MATRIX_COUNT
        for slot, index in enumerate(indices):
            segment = segments[index]
            reference = self._axes[slot]
            if reference is None:
                reference = (0., 0., 1.) if "arm" in segment.name else (1., 0., 0.)
            if segment.name in ('left_hand','right_hand'):
                dt = (timestamp_ns-previous_timestamp_ns)/1e9 if previous_timestamp_ns is not None else 0.
                axis=(continuous_palm_down_x_axis(segment,previous_axis=self._axes[slot],
                        previous_direction=self._hand_directions[slot],max_roll_radians=8.*dt)
                      if self._palm_roll_stability else
                      palm_down_x_axis(segment,previous_axis=self._axes[slot],max_roll_radians=8.*dt))
            else:
                axis = stable_bone_local_x_axis(segment, reference)
            try:
                matrix = self._bone_affine(
                    segment, local_x_reference=axis,
                    local_bone_length_q1_14=(bone_bind_lengths_q1_14[slot]
                        if segment.name!='head' and slot<len(bone_bind_lengths_q1_14) else None))
                if matrix is not None and slot < len(inverse_bind_matrices_q1_14):
                    matrix = self._compose_affine(matrix, inverse_bind_matrices_q1_14[slot])
            except ValueError:
                # A detected limb can exceed the fixed-point matrix range.
                # Omit its mesh this frame rather than deforming a neutral limb.
                continue
            if matrix is not None:
                self._axes[slot] = axis
                matrices[slot], valid[slot] = matrix, True
                if self._palm_roll_stability and segment.name in ('left_hand','right_hand'):
                    next_hand_directions[slot]=tuple((b-a)/segment.length
                        for a,b in zip(segment.start,segment.end))
        self._hand_directions=next_hand_directions
        plan=AvatarFramePlan(
            pair=pair, points={}, joints=joints, segments=segments,
            matrix_writes=tuple(MatrixWrite(i, value) for i, value in enumerate(matrices)),
            matrix_valid=tuple(valid), vertex_requests=tuple(vertex_requests), pose_preview=True)
        self._last_preview_plan=plan
        return plan
