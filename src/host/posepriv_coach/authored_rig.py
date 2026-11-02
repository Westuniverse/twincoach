"""Common-bind hierarchical display retargeting, AUTHORED-RIG 0.1.

Input drivers already contain inverse-bind. Output matrices act on the same
common rest space; they must not be multiplied by inverse-bind a second time.
No source observation, clock, inference confidence or scoring is created here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math

from .avatar_skinning import AffineMatrixQ1_14


def _dot(a, b):
    return math.fsum(float(x) * float(y) for x, y in zip(a, b))


def _transpose(a):
    return tuple(zip(*a))


def _multiply(a, b):
    return tuple(tuple(_dot(row, col) for col in zip(*b)) for row in a)


def _vector(a, v):
    return tuple(_dot(row, v) for row in a)


def _grid(a):
    return tuple(tuple(round(v * 16777216) / 16777216 for v in row) for row in a)


def _inverse(a):
    x, y, z = a
    cross = lambda u, v: (u[1]*v[2]-u[2]*v[1], u[2]*v[0]-u[0]*v[2], u[0]*v[1]-u[1]*v[0])
    cofactor = (cross(y, z), cross(z, x), cross(x, y))
    determinant = _dot(x, cofactor[0])
    if determinant <= 1e-8:
        raise ValueError('driver linear part must be nonsingular and orientation preserving')
    return tuple(tuple(v / determinant for v in row) for row in zip(*cofactor))


def _polar(a):
    """Fixed scalar polar iteration, retaining positive stretch separately."""
    rotation = a
    for _ in range(8):
        inverse_transpose = _transpose(_inverse(rotation))
        rotation = tuple(tuple((x+y)/2 for x, y in zip(row, inv))
                         for row, inv in zip(rotation, inverse_transpose))
    check = _multiply(_transpose(rotation), rotation)
    if max(abs(check[i][j] - (i == j)) for i in range(3) for j in range(3)) > 1e-7:
        raise ValueError('driver stretch outside supported polar convergence range')
    rotation = _grid(rotation)
    return rotation, _multiply(_transpose(rotation), a)


def _unit(q):
    length = math.sqrt(_dot(q, q))
    if length < 1e-12:
        raise ValueError('degenerate rotation axis or quaternion')
    return tuple(v/length for v in q)


def _quaternion(m):
    trace = math.fsum(m[k][k] for k in range(3))
    if trace > 0:
        s = 2 * math.sqrt(1 + trace)
        q = (s/4, (m[2][1]-m[1][2])/s, (m[0][2]-m[2][0])/s, (m[1][0]-m[0][1])/s)
    else:
        k = max(range(3), key=lambda i: m[i][i])
        j, l = (k+1) % 3, (k+2) % 3
        s = 2 * math.sqrt(max(0., 1+m[k][k]-m[j][j]-m[l][l]))
        v = [0., 0., 0.]
        v[k], v[j], v[l] = s/4, (m[j][k]+m[k][j])/s, (m[l][k]+m[k][l])/s
        q = ((m[l][j]-m[j][l])/s, *v)
    q = _unit(q)
    # Stable shortest-arc convention, including an exact half turn.
    sign = next((v for v in q if abs(v) > 1e-12), 1.)
    return tuple(-v for v in q) if sign < 0 else q


def _rotation(q):
    w, x, y, z = _unit(q)
    return _grid(((1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)),
                  (2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)),
                  (2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y))))


def _fractional(rotation, amount):
    q = _quaternion(rotation)
    length = math.sqrt(_dot(q[1:], q[1:]))
    if length < 1e-12:
        return ((1., 0., 0.), (0., 1., 0.), (0., 0., 1.))
    half = amount * math.atan2(length, q[0])
    return _rotation((math.cos(half), *(v * math.sin(half)/length for v in q[1:])))


@dataclass(frozen=True, slots=True)
class AuthoredBone:
    name: str
    parent: str | None
    pivot_q1_14: tuple[int, int, int]
    driver: int

    def __post_init__(self):
        pivot = tuple(self.pivot_q1_14)
        if (type(self.name) is not str or not self.name or
                self.parent is not None and (type(self.parent) is not str or not self.parent) or
                len(pivot) != 3 or any(type(v) is not int or not -32768 <= v <= 32767 for v in pivot) or
                type(self.driver) is not int or not 0 <= self.driver < 16):
            raise ValueError('named bone, common Q1.14 pivot and explicit driver required')
        object.__setattr__(self, 'pivot_q1_14', pivot)


@dataclass(frozen=True, slots=True)
class RotationBlend:
    bone: str
    from_driver: int
    to_driver: int
    fraction: float


@dataclass(frozen=True, slots=True)
class AxialTwist:
    bone: str
    from_driver: int
    to_driver: int
    axis_q1_14: tuple[int, int, int]
    fraction: float = 1.

    def __post_init__(self):
        axis = tuple(self.axis_q1_14)
        if (len(axis) != 3 or any(type(v) is not int or not -32768 <= v <= 32767 for v in axis)
                or not any(axis)):
            raise ValueError('nonzero common-rest-space Q1.14 twist axis required')
        object.__setattr__(self, 'axis_q1_14', axis)


@dataclass(frozen=True, slots=True)
class AuthoredPose:
    matrices: tuple[AffineMatrixQ1_14, ...]
    ambiguous_twists: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AuthoredRig:
    bones: tuple[AuthoredBone, ...]
    output_bones: tuple[str, ...]
    driver_count: int
    rules: tuple[RotationBlend | AxialTwist, ...] = ()
    _order: tuple[AuthoredBone, ...] = field(init=False, repr=False)

    def __post_init__(self):
        bones, outputs, rules = tuple(self.bones), tuple(self.output_bones), tuple(self.rules)
        if (type(self.driver_count) is not int or not 1 <= self.driver_count <= 16 or
                not bones or any(not isinstance(b, AuthoredBone) for b in bones)):
            raise ValueError('authored hierarchy and 1..16 source drivers required')
        by_name = {b.name: b for b in bones}
        if (len(by_name) != len(bones) or sum(b.parent is None for b in bones) != 1 or
                any(b.driver >= self.driver_count or b.parent is not None and b.parent not in by_name for b in bones) or
                not outputs or len(set(outputs)) != len(outputs) or any(n not in by_name for n in outputs)):
            raise ValueError('one rooted hierarchy with unique bones and explicit outputs required')
        order, visiting, visited = [], set(), set()
        def visit(name):
            if name in visiting:
                raise ValueError('authored hierarchy contains a cycle')
            if name in visited:
                return
            visiting.add(name)
            b = by_name[name]
            if b.parent is not None:
                visit(b.parent)
            visiting.remove(name); visited.add(name); order.append(b)
        for b in bones:
            visit(b.name)
        seen = set()
        for rule in rules:
            if (not isinstance(rule, (RotationBlend, AxialTwist)) or rule.bone not in by_name or
                    by_name[rule.bone].parent is None or rule.bone in seen or
                    any(type(i) is not int or not 0 <= i < self.driver_count for i in (rule.from_driver, rule.to_driver)) or
                    type(rule.fraction) not in (int, float) or not math.isfinite(rule.fraction) or not 0 <= rule.fraction <= 1):
                raise ValueError('unique non-root rotation rules with finite fractions in [0,1] required')
            seen.add(rule.bone)
        object.__setattr__(self, 'bones', bones)
        object.__setattr__(self, 'output_bones', outputs)
        object.__setattr__(self, 'rules', rules)
        object.__setattr__(self, '_order', tuple(order))

    def to_mapping(self):
        return dict(schema_version=1, scope='authored_common_bind_display_rig',
            driver_count=self.driver_count, bones=[asdict(b) for b in self.bones],
            output_bones=list(self.output_bones), rules=[dict(kind=(
                'rotation_blend' if isinstance(rule, RotationBlend) else 'axial_twist'),
                **asdict(rule)) for rule in self.rules])

    def matrix_aliases(self):
        """Conservative symbolic equality for this rig, valid beyond a cached pose.

        Only a child with the same absolute linear expression as its parent
        inherits that parent's full affine. Equal drivers across different
        anchors alone are insufficient. Nonzero rules remain independent.
        """
        from .skinning_matrix_aliases import SkinningMatrixAliases
        rules = {rule.bone: rule for rule in self.rules}
        linear, roots = {}, {}
        for bone in self._order:
            rule = rules.get(bone.name)
            expression = (('driver', bone.driver) if rule is None else
                          ('driver', rule.from_driver) if rule.fraction == 0 else
                          ('rule', bone.name))
            linear[bone.name] = expression
            roots[bone.name] = (roots[bone.parent] if bone.parent is not None and
                               expression == linear[bone.parent] else bone.name)
        lookup, groups, representatives = {}, [], []
        for index, name in enumerate(self.output_bones):
            root = roots[name]
            if root not in lookup:
                lookup[root] = len(representatives);representatives.append(index)
            groups.append(lookup[root])
        return SkinningMatrixAliases(tuple(groups), tuple(representatives))

    @classmethod
    def from_mapping(cls, value):
        if (not isinstance(value, dict) or type(value.get('schema_version')) is not int or
                value['schema_version'] != 1 or value.get('scope') != 'authored_common_bind_display_rig'):
            raise ValueError('unsupported authored common-bind rig schema')
        rules = []
        for row in value.get('rules', ()):
            row = dict(row)
            kind = row.pop('kind')
            if kind not in ('rotation_blend', 'axial_twist'):
                raise ValueError('unsupported authored rotation rule')
            rules.append((RotationBlend if kind == 'rotation_blend' else AxialTwist)(**row))
        return cls(tuple(AuthoredBone(**b) for b in value['bones']),
                   tuple(value['output_bones']), value['driver_count'], tuple(rules))

    def retarget(self, drivers) -> AuthoredPose:
        drivers = tuple(drivers)
        if len(drivers) != self.driver_count or any(not isinstance(m, AffineMatrixQ1_14) for m in drivers):
            raise ValueError('one common-space affine matrix per declared driver required')
        matrices = tuple(tuple(tuple(v / 16384 for v in tuple(m.values)[i:i+4])
                               for i in (0, 4, 8)) for m in drivers)
        linear = tuple(tuple(row[:3] for row in m) for m in matrices)
        overrides, decomposed, ambiguous = {}, {}, []
        def decompose(index):
            if index not in decomposed:
                decomposed[index] = _polar(linear[index])
            return decomposed[index]
        for rule in self.rules:
            if rule.fraction == 0:
                overrides[rule.bone] = linear[rule.from_driver]
                continue
            start, stretch = decompose(rule.from_driver)
            end, _ = decompose(rule.to_driver)
            relative = _multiply(_transpose(start), end)
            if isinstance(rule, RotationBlend):
                delta = _fractional(relative, rule.fraction)
            else:
                axis = _unit(rule.axis_q1_14)
                q = _quaternion(relative)
                projection = _dot(q[1:], axis)
                if math.hypot(q[0], projection) < 1e-8:
                    # A half-turn swing has no defined twist. Keep the parent
                    # driver for this bone and report the ambiguity explicitly.
                    ambiguous.append(rule.bone)
                    overrides[rule.bone] = linear[rule.from_driver]
                    continue
                half = rule.fraction * math.atan2(projection, q[0])
                delta = _rotation((math.cos(half), *(v * math.sin(half) for v in axis)))
            overrides[rule.bone] = _multiply(_multiply(start, delta), stretch)
        completed = {}
        for bone in self._order:
            a = overrides.get(bone.name, linear[bone.driver])
            if bone.parent is None:
                translation = tuple(row[3] for row in matrices[bone.driver])
            else:
                parent_a, parent_t = completed[bone.parent]
                pivot = tuple(v / 16384 for v in bone.pivot_q1_14)
                parent_point, point = _vector(parent_a, pivot), _vector(a, pivot)
                translation = tuple(math.fsum((x, y, -z)) for x, y, z in zip(parent_point, parent_t, point))
            completed[bone.name] = a, translation
        result = []
        for name in self.output_bones:
            a, t = completed[name]
            result.append(AffineMatrixQ1_14(tuple(round(v * 16384) for row, d in zip(a, t) for v in (*row, d))))
        return AuthoredPose(tuple(result), tuple(ambiguous))
