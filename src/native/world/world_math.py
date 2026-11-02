"""Private explicit geometry-child helper replacement; no import-time patch."""
import importlib.util
from pathlib import Path


def load(library=None):
    path=Path(library or Path(__file__).with_name('libposepriv_world_math.so')).resolve()
    spec=importlib.util.spec_from_file_location('_posepriv_world_math',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


class Installation:
    def __init__(self,library=None):
        import posepriv_coach.preview_head_orientation as head
        import posepriv_coach.preview_current_body_rotation as body
        import posepriv_coach.preview_rigid_avatar as rig
        import posepriv_coach.world_avatar_preview as world
        self.module=load(library)
        self.module.setup(head._rotation,head._unit,rig._matmul)
        self.old=[]
        for owner,name,value in ((head,'_rotation',self.module.rotation),(head,'_unit',self.module.unit),
            (head,'_matmul',self.module.matmul),(body,'_rotation',self.module.rotation),
            (body,'_matmul',self.module.matmul),(world,'_matmul',self.module.matmul),(rig,'_matmul',self.module.matmul)):
            self.old.append((owner,name,getattr(owner,name)));setattr(owner,name,value)

    def restore(self):
        for owner,name,value in reversed(self.old):setattr(owner,name,value)
        self.old.clear()


def install(library=None):
    """Call once in the explicit geometry child, before constructing its owner."""
    return Installation(library)
