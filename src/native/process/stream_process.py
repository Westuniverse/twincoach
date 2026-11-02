# SPDX-License-Identifier: GPL-2.0-only
"""Full33 stream model/actor in the existing bounded inference process/arena."""
from contextlib import ExitStack
import os
from pathlib import Path
import pickle
import signal
import sys
from types import MappingProxyType

HERE=Path(__file__).resolve().parent
sys.path[:0]=[str(HERE.parent/'actor'),
              str(HERE.parent/'stream')]
from posepriv_coach.field_preview_process import (InferenceProcess as BaseInferenceProcess,
    inference_metadata,inference_parts,WORK_HEADER,_copy_images_into_lease)
from posepriv_coach.avatar_image_source import AvatarImageSource
from posepriv_coach.world_avatar_preview import WorldDisplayObservation

FIELDS=('x','y','z','visibility','presence')


def primitive_world(work):
    """Only own validated immutable observations cross the own-process IPC."""
    value=dict(work);records={}
    for view,observation in value.pop('world_observations').items():
        s=observation.source
        records[view]=dict(source=(s.view,s.frame_id,s.timestamp_ns,s.width,s.height,s.rotation_degrees),
            world=tuple(tuple(p[k]for k in FIELDS)for p in observation.world_landmarks),
            normalized=tuple(tuple(p[k]for k in FIELDS)for p in observation.normalized_landmarks))
    value['full33_world_records']=dict(schema=1,views=records)
    return value


def reconstruct_world(work):
    if work is None:return None
    record=work.pop('full33_world_records')
    if record['schema']!=1 or set(record['views'])!={'front','side'}:
        raise ValueError('unsupported private Full33 world IPC record')
    observations={}
    for view,r in record['views'].items():
        source=AvatarImageSource(*r['source'])
        if source.view!=view or len(r['world'])not in(0,33) or len(r['world'])!=len(r['normalized']):
            raise ValueError('world IPC physical source/shape mismatch')
        pose=work['poses'][view]
        if (source.frame_id,source.timestamp_ns,source.width,source.height)!=(
                pose.frame_id,pose.timestamp_ns,pose.source_width,pose.source_height):
            raise ValueError('world IPC differs from raw pose source')
        # Original model outputs were already validated before serialization.
        # Recreate the same immutable public value type without rescanning each
        # float in geometry's process. No foreign/untrusted IPC is consumed.
        observation=object.__new__(WorldDisplayObservation)
        object.__setattr__(observation,'source',source)
        for attribute,key in (('world_landmarks','world'),('normalized_landmarks','normalized')):
            points=tuple(MappingProxyType(dict(zip(FIELDS,p,strict=True)))for p in r[key])
            object.__setattr__(observation,attribute,points)
        observations[view]=observation
    work['world_observations']=observations
    return work


def publish_work(output,work,counts,lock):
    value=primitive_world(work)
    with lock:counts[0]+=1;counts[3]=work['dpu_frames'];sequence=counts[0]
    if hasattr(output,'writing'):
        with output.writing()as lease:
            _copy_images_into_lease(value,lease)
            metadata=inference_metadata(value);metadata['images']=lease.images
            encoded=pickle.dumps(metadata,protocol=5)
            packet=WORK_HEADER.pack(b'PPI2',len(encoded))+encoded
            lost=output.publish_lease(lease,sequence,max(r[1]for r in work['selected'].values()),packet)
    else:
        lost=output.publish_parts(sequence,max(r[1]for r in work['selected'].values()),inference_parts(value))
    with lock:counts[1]+=lost


def run_stream_owner(cfg,inputs,rotations,external_stop,stop,output,final,done,counts,lock,native_pid):
    signal.signal(signal.SIGINT,signal.SIG_IGN)
    def interrupted(*_):raise KeyboardInterrupt('Stream inference owner terminated')
    signal.signal(signal.SIGTERM,interrupted)
    model=None;error=None;final_stats=None
    try:
        sys.path.insert(0,cfg['python_path'])
        os.environ.update(LD_LIBRARY_PATH='/opt/vitis-ai-compat/lib:/lib/aarch64-linux-gnu:/lib',
            XLNX_VART_FIRMWARE=cfg['xclbin'],XLNX_DISABLE_LOAD_XCLBIN='0')
        import cv2
        from concurrent.futures import ThreadPoolExecutor
        from posepriv_coach.field_preview import LatestPair
        if cfg.get('tail4_memfd_module_dir'):
            sys.path.insert(0,cfg['tail4_memfd_module_dir'])
            from shm_client import StreamCombinedWorker
        else:
            from stream_client import StreamCombinedWorker
        from stream_runtime import StreamingPreviewModel,infer_latest_pair
        cv2.setNumThreads(1)
        transport=StreamCombinedWorker(cfg['tail4_worker_command'],capacity=cfg.get('tail4_stream_capacity',4),
            timeout=cfg.get('tail4_worker_timeout',5),
            on_process_started=lambda pid:setattr(native_pid,'value',pid))
        model=StreamingPreviewModel(cfg,transport=transport)
        latest=LatestPair(max_skew_ns=50_000_000)
        flag={1:cv2.IMREAD_COLOR,2:cv2.IMREAD_REDUCED_COLOR_2,4:cv2.IMREAD_REDUCED_COLOR_4}[cfg.get('preview_decode_scale',1)]
        with ThreadPoolExecutor(max_workers=2,thread_name_prefix='stream-process-decode')as decoder:
            while not stop.is_set()and not external_stop.is_set():
                work=infer_latest_pair(inputs,latest,rotations,None,model,flag,decoder)
                if work is None:stop.wait(.001);continue
                if stop.is_set()or external_stop.is_set():
                    with lock:counts[2]+=1
                    break
                publish_work(output,work,counts,lock)
    except BaseException as exc:error=f'{type(exc).__name__}: {exc}'
    finally:
        if model is not None:
            try:model.close()
            except BaseException as exc:
                if error is None:error=f'{type(exc).__name__}: {exc}'
            final_stats=model.stats()
            with lock:counts[3]=final_stats['frames']
        final.put(dict(error=error,model_stats=final_stats));done.set()


class InferenceProcess(BaseInferenceProcess):
    """Reuse existing spawn supervision/leases and inject only the model owner."""
    def __init__(self,cfg,inputs,rotations,stopped,*,_target=run_stream_owner):
        cfg=dict(cfg)
        # Existing cleanup verifies an owned process-group leader's first2 argv.
        # Bind that existing check to this profile's actual native worker/model.
        command=cfg['tail4_worker_command'];cfg['worker'],cfg['model']=command[:2]
        super().__init__(cfg,inputs,rotations,stopped,_target=_target)

    def take(self,timeout=.05):return reconstruct_world(super().take(timeout))

    def stats(self):
        result=super().stats();result.update(model_owner='full33_stream_process',stream_capacity=self.cfg.get('tail4_stream_capacity',4))
        return result
