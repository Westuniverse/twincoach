# SPDX-License-Identifier: GPL-2.0-only
"""Private scheduling candidate: one owner services existing JPEG futures/NN.

No new thread, model/transport credit, arena slot or live diagnostic. Existing
two JPEG executor workers retain ownership until both original captures finish.
While JPEG jobs run, the owner can publish a completed previous neural pair.
"""


class CompletionFirstDecodePump:
    def __init__(self, model, inputs, latest, rotations, decode_flag, executor, *,
                 select=None, decode=None, wait_seconds=.003):
        if select is None or decode is None:
            from posepriv_coach.field_preview_pipeline import select_latest_pair, decode_preview_frame
            select=select or select_latest_pair
            decode=decode or decode_preview_frame
        self.model=model;self.inputs=inputs;self.latest=latest
        self.rotations=rotations;self.decode_flag=decode_flag;self.executor=executor
        self.select=select;self.decode=decode;self.wait_seconds=wait_seconds
        self.pending=None

    def take(self):
        work=self.model.take(timeout=0)
        if work is not None:return work
        if self.pending is None:
            selection=self.select(self.inputs,self.latest)
            if selection is None:return None
            futures={view:self.executor.submit(self.decode,view,record,self.rotations[view],self.decode_flag)
                     for view,record in selection['selected'].items()}
            self.pending=(selection,futures)
        selection,futures=self.pending
        while True:
            if all(f.done()for f in futures.values()):
                # Same source order and original captured objects. A failed JPEG
                # future raises before offer, as in decode_selected_pair.
                captured={view:f.result()for view,f in futures.items()}
                self.pending=None
                self.model.offer(dict(selection,captured=captured),self.rotations)
                return self.model.take(timeout=0)
            # Existing actor condition wait releases the GIL. Bound polling is
            # only for the other already-running futures, not a new collector.
            work=self.model.take(timeout=self.wait_seconds)
            if work is not None:return work


def infer_latest_pair(inputs,latest,rotations,estimator,model,decode_flag,decoder=None,**ignored):
    if decoder is None:
        raise ValueError('completion-first candidate uses the existing two-worker JPEG executor')
    pump=getattr(model,'_completion_first_decode_pump',None)
    if pump is None:
        pump=CompletionFirstDecodePump(model,inputs,latest,rotations,decode_flag,decoder)
        model._completion_first_decode_pump=pump
    return pump.take()


def run_completion_first_owner(*args,**kwargs):
    # Spawn target patches this one helper only in the isolated inference owner.
    # run_stream_owner imports the helper inside its try block, after this point.
    import stream_runtime
    from stream_process import run_stream_owner
    original=stream_runtime.infer_latest_pair
    stream_runtime.infer_latest_pair=infer_latest_pair
    try:return run_stream_owner(*args,**kwargs)
    finally:stream_runtime.infer_latest_pair=original
