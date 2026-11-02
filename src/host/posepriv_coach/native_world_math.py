"""Explicit Full33 world/head CPU math; GPL-2.0-only, existing CPython/NumPy.

The native leaf preserves the Python helpers' scalar products/math.fsum,
thresholds, exceptions and owned float64 outputs. Unsupported inputs retain
their original reference path. Importing or constructing the native object
does not patch helpers. Only an isolated geometry owner calls ``install`` before
constructing its world/head/body owners, then ``scope.restore()`` on shutdown.
No configuration or display-process default is changed here.
"""
import importlib.util
from pathlib import Path

_references=None
_active_installation=None


class NativeWorldMath:
    """Direct native callables, usable without installing helper references."""
    def __init__(self,library_path):
        global _references
        self.library_path=Path(library_path).resolve(strict=True)
        spec=importlib.util.spec_from_file_location('_posepriv_world_math',self.library_path)
        self._module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self._module)
        if _references is None:
            from .preview_head_orientation import _rotation,_unit
            from .preview_rigid_avatar import _matmul
            _references=(_rotation,_unit,_matmul)
        self._module.setup(*_references)
        self.rotation=self._module.rotation
        self.unit=self._module.unit
        self.matmul=self._module.matmul

    def stats(self):
        return self._module.stats()


class _Installation:
    def __init__(self,native):
        from . import preview_head_orientation as head
        from . import preview_current_body_rotation as body
        from . import preview_rigid_avatar as rig
        from . import world_avatar_preview as world
        self.native=native;self.module=native._module;self._old=[]
        for owner,name,value in ((head,'_rotation',native.rotation),(head,'_unit',native.unit),
            (head,'_matmul',native.matmul),(body,'_rotation',native.rotation),
            (body,'_matmul',native.matmul),(world,'_matmul',native.matmul),(rig,'_matmul',native.matmul)):
            self._old.append((owner,name,getattr(owner,name)));setattr(owner,name,value)

    def restore(self):
        global _active_installation
        for owner,name,value in reversed(self._old):setattr(owner,name,value)
        self._old.clear()
        if _active_installation is self:_active_installation=None

    def __enter__(self):return self

    def __exit__(self,*unused):self.restore()


def install(library_path):
    """Explicit single geometry-owner installation; never an import-time patch.

    Build owners after installation so their saved matmul reference uses the
    native function. One active scope prevents overlapping restore/fallback
    ownership. Existing filters, thresholds and source/score logic are untouched.
    """
    global _active_installation
    if _active_installation is not None:
        raise RuntimeError('native world math already installed in this owner')
    scope=_Installation(NativeWorldMath(library_path));_active_installation=scope
    return scope
