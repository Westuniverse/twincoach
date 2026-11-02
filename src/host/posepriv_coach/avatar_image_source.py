"""Explicit image identity for learned display poses, independent of scoring."""
from dataclasses import dataclass
from types import MappingProxyType

from .avatar_skinning import MATRIX_COUNT


@dataclass(frozen=True, slots=True)
class AvatarImageSource:
    view: str
    frame_id: int
    timestamp_ns: int
    width: int
    height: int
    rotation_degrees: int

    def __post_init__(self):
        if (self.view not in ('front', 'side') or
                any(type(v) is not int or v < 0 for v in (self.frame_id, self.timestamp_ns)) or
                any(type(v) is not int or v <= 0 for v in (self.width, self.height)) or
                type(self.rotation_degrees) is not int or self.rotation_degrees not in (0,90,180,270)):
            raise ValueError('original image identity, dimensions and rotation required')


def avatar_image_source(plan):
    """Existing scored plans keep their front clock; new plans carry a source.

    No conversion into a PoseFrame or synchronized pair is performed.
    """
    source = getattr(plan, 'source', None)
    return source if source is not None else plan.pair.front


@dataclass(frozen=True, slots=True)
class ImageAvatarFramePlan:
    """Render palette from one image, with no stereo pair or score observations.

    matrix_valid only permits drawing. All geometry is learned or a display
    prior; original model confidence is retained separately by the caller.
    """
    source: AvatarImageSource
    joints: object
    segments: tuple
    matrix_writes: tuple
    matrix_valid: tuple[bool, ...]
    vertex_requests: tuple = ()
    model_origin: tuple[float, float, float] = (0., 0., 0.)
    model_scale: float = 1.
    pose_preview: bool = True

    def __post_init__(self):
        if (not isinstance(self.source, AvatarImageSource) or
                len(self.matrix_valid) != MATRIX_COUNT or
                tuple(w.index for w in self.matrix_writes) != tuple(range(MATRIX_COUNT))):
            raise ValueError('image render plan requires explicit source and 16 ordered matrices')
        object.__setattr__(self, 'joints', MappingProxyType(dict(self.joints)))

    @property
    def points(self):
        return MappingProxyType({})  # No triangulated observations exist.

    def matrix_bank(self):
        return tuple(w.matrix for w in self.matrix_writes)

    def execute(self,backend,*,program_matrices=None):
        if program_matrices is not None:program_matrices(self.matrix_writes)
        output=backend.exchange(self.vertex_requests)
        if len(output)!=len(self.vertex_requests):
            raise ValueError('image avatar backend returned an unexpected beat count')
        return output


@dataclass(frozen=True, slots=True)
class DisplayImageKeypoints:
    """Display-only markers, tied to one original image; never a PoseFrame."""
    source: AvatarImageSource
    points: object

    def __post_init__(self):
        if not isinstance(self.source,AvatarImageSource):
            raise ValueError('explicit marker source required')
        object.__setattr__(self,'points',MappingProxyType(dict(self.points)))
