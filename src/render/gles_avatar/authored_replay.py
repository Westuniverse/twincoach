"""Opt-in full authored positions and matching all-weight PS normals.

The validated world cache retains authority over images, driver/D/state bytes
and original scoring. This adapter never estimates or relabels observations.
"""
from pathlib import Path
import json,time

from posepriv_coach.avatar_skinning import AffineMatrixQ1_14,VertexQ1_14
from posepriv_coach.avatar_skinning_palettes import WeightedSkinningVertex,WeightedSkinningPlan
from posepriv_coach.native_skinning_palettes import NativeSkinningPalettes
from posepriv_coach.weighted_normals import WeightedNormalPlan,NativeWeightedNormals


def load_weighted_mesh(path, asset):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    names = tuple(data['bone_names'])
    if (data.get('schema_version') != 1 or data.get('scope') != 'authored_weighted_mesh' or
            not data.get('source') or not data.get('license_id') or
            not names or len(set(names)) != len(names) or any(type(n) is not str or not n for n in names)):
        raise ValueError('licensed authored weighted mesh schema1 with unique named bones required')
    vertices = tuple(WeightedSkinningVertex(VertexQ1_14(*v['position']),
        tuple(v['bone_indices']),tuple(v['weights_q0_15'])) for v in data['vertices'])
    if (len(vertices) != len(asset.vertices) or
            any(v.vertex != old.position for v, old in zip(vertices, asset.vertices)) or
            tuple(tuple(t) for t in data['triangles']) != asset.triangles or
            tuple(tuple(c) for c in data['triangle_colors_rgb']) != asset.triangle_colors_rgb or
            any(i >= len(names) for v in vertices for i in v.bone_indices)):
        raise ValueError('authored mesh must retain exact current rest vertices, triangles and colors')
    return names, vertices


class AuthoredReplay:
    def __init__(self, path, world, asset, rest_normals, *, native_library=None,native_normal_library=None,alias_rig=None):
        import numpy as np
        path = Path(path).resolve(strict=True)
        manifest = json.loads(path.read_text(encoding='utf-8'))
        if manifest.get('schema_version') != 1 or len(manifest.get('chapters', ())) != len(world.entries):
            raise ValueError('authored manifest must match validated world chapters')
        mesh = Path(manifest['mesh'])
        self.names, vertices = load_weighted_mesh(mesh if mesh.is_absolute() else path.parent/mesh, asset)
        self.aliases = None
        if alias_rig is not None:
            from posepriv_coach.authored_rig import AuthoredRig
            rig = AuthoredRig.from_mapping(json.loads(Path(alias_rig).read_text(encoding='utf-8')))
            if rig.output_bones != self.names:raise ValueError('alias rig and authored mesh bone order differ')
            self.aliases = rig.matrix_aliases()
        self.plan = WeightedSkinningPlan(self.aliases.remap_vertices(vertices) if self.aliases else vertices,
            bone_count=len(self.aliases.representatives) if self.aliases else len(self.names))
        self.normals = WeightedNormalPlan(vertices, rest_normals, bone_count=len(self.names))
        self.native_normals = NativeWeightedNormals(self.normals,native_normal_library) if native_normal_library is not None else None
        self.native = NativeSkinningPalettes(self.plan, native_library) if native_library is not None else None
        self.entries = []
        for source, entry in zip(world.folders, manifest['chapters']):
            if source is None or entry is None:
                if source is not None or entry is not None:
                    raise ValueError('authored/world chapter availability differs')
                self.entries.append(None);continue
            folder = Path(entry)
            if not folder.is_absolute():folder = path.parent/folder
            report = json.loads((folder/'report.json').read_text(encoding='utf-8'))
            original = json.loads((source/'report.json').read_text(encoding='utf-8'))
            n = original['frames']
            if any(not all(row['matrix_valid'][:13]) for row in json.loads(
                    (source/'states.json').read_text(encoding='utf-8'))):
                raise ValueError('authored display requires complete inferred source render palettes')
            if (report.get('schema_version') != 1 or report.get('scope') != 'host_authored_rig_preview' or
                    report.get('global_bones') != list(self.names) or report.get('frames') != n or
                    report.get('original_world_report') != original or
                    not report.get('all_geometry_inferred') or report.get('source_schedule') != 'latest-raw'):
                raise ValueError('authored cache bone order and exact original world report required')
            for a, b in (('driver_matrices.bin','matrices.bin'),('states.json','states.json'),('display.bin','display.bin')):
                if (folder/a).read_bytes() != (source/b).read_bytes():
                    raise ValueError('authored cache driver/state/display bytes differ from world cache')
            raw = (folder/'matrices.bin').read_bytes()
            if len(raw) != n*len(self.names)*24:
                raise ValueError('authored global matrix frame length differs')
            self.entries.append(np.frombuffer(raw, dtype='<i2').reshape(n,len(self.names),12))
        self.reset()

    def reset(self):
        self._key = self._output = self._normal = None

    def execute(self, chapter, index, *, transfer, validator=None, reuse=False):
        bank = self.entries[chapter]
        if bank is None:raise ValueError('no authored pose for this source chapter')
        row = bank[index];key = row.tobytes()
        if reuse and key == self._key:
            return self._output,self._normal,True,0,0
        matrices = tuple(AffineMatrixQ1_14(tuple(int(v) for v in m)) for m in row)
        start = time.perf_counter_ns()
        normals = self.normals.apply(matrices,native_ops=self.native_normals)
        normal_ns = time.perf_counter_ns()-start
        start = time.perf_counter_ns()
        position_matrices = self.aliases.compress(matrices) if self.aliases else matrices
        output = self.plan.execute(position_matrices, transfer=transfer, validator=validator, native_ops=self.native)
        position_ns = time.perf_counter_ns()-start
        # Publish/reuse only after every position packet has succeeded.
        self._key,self._output,self._normal = key,output,normals
        return output,normals,False,normal_ns,position_ns
