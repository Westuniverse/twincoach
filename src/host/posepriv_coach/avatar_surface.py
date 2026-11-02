"""Optional soft presentation lighting; source pose and scoring are untouched."""


def soft_triangle_lights(points,triangles,vertex_valid=None):
    """Area-weighted vertex normals, camera-space light, gamma 2.2 approximation.

    Returns one uniform factor per source face, so the existing PPS3 shade ABI
    can consume it. This is soft face shading, not per-fragment Phong shading.
    """
    import numpy as np
    faces=np.asarray(triangles,dtype=np.int64).reshape(-1,3)
    ambient=.55**(1/2.2)
    result=np.full(len(faces),ambient)
    if not len(points) or not len(faces):return result
    positions=np.asarray(points,dtype=np.float64)
    selected=np.all((faces>=0)&(faces<len(positions)),axis=1)
    if vertex_valid is not None:
        indices=np.flatnonzero(selected)
        selected[indices]=np.asarray(vertex_valid,dtype=bool)[faces[indices]].all(axis=1)
    active=faces[selected]
    normals=np.zeros_like(positions)
    area=np.cross(positions[active[:,1]]-positions[active[:,0]],
                  positions[active[:,2]]-positions[active[:,0]])
    for lane in range(3):np.add.at(normals,active[:,lane],area)
    length=np.linalg.norm(normals,axis=1)
    diffuse=np.zeros(len(positions))
    usable=length>1.e-12
    diffuse[usable]=np.abs(normals[usable]@np.array((-.35,.55,-.76)))/length[usable]
    light=np.power(.55+.45*np.clip(diffuse,0,1),1/2.2)
    result[selected]=light[active].mean(axis=1)
    return result
