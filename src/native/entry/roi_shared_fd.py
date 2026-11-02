# SPDX-License-Identifier: GPL-2.0-only
"""One exclusive open, shared file description across main skin and NN ROI.

SpawnSharedFd is nonowning in the parent. DupFd is created only while spawn is
serializing the target, so multiprocessing owns failed-start cleanup instead of
leaving an eagerly created resource_sharer token. No images cross this handoff.
"""
import os
from multiprocessing.reduction import DupFd


def _receive_fd(handle):
    fd=handle.detach()
    os.set_inheritable(fd,False)  # Never leak DMA ownership into VART subprocess.
    return fd


class SpawnSharedFd:
    def __init__(self,fd):self.fd=fd
    def __reduce__(self):return _receive_fd,(DupFd(self.fd),)


class SharedDmaFdOwner:
    """Cold owner; transfer parent fd once, duplicate only in Process.start()."""
    def __init__(self,device='/dev/posepriv_shared_pose_dma',*,opener=os.open,closer=os.close):
        self.fd=opener(device,os.O_RDWR|getattr(os,'O_CLOEXEC',0))
        self._closer=closer

    def for_child(self):
        if self.fd is None:raise RuntimeError('shared DMA cold fd already transferred')
        return SpawnSharedFd(self.fd)

    def take_for_parent(self,_device,_flags):
        if self.fd is None:raise RuntimeError('shared DMA cold fd already transferred')
        fd,self.fd=self.fd,None
        return fd

    def close(self):
        if self.fd is not None:
            fd,self.fd=self.fd,None
            self._closer(fd)

    def __enter__(self):return self
    def __exit__(self,*_):self.close()


def run_with_shared_fd(fd,target,*args):
    """Spawn target wrapper: cropper borrows this fd; the wrapper closes last."""
    try:
        cfg=dict(args[0])
        cfg['preview_roi_dma_fd']=fd  # Child-local int, never a second open.
        return target(cfg,*args[1:])
    finally:
        os.close(fd)
