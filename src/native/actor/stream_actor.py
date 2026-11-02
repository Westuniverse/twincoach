# SPDX-License-Identifier: GPL-2.0-only
"""One-owner causal ROI/preprocess/decode actor over bounded source transport."""
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
import sys
from threading import Condition, Thread
from time import monotonic,monotonic_ns

HERE=Path(__file__).resolve().parent
sys.path[:0]=[str(HERE.parent/'tail4_hdmi_candidate_20261030'),
              str(HERE.parent/'tail4_preview_adapter_20261030')]
from preview_adapter import TailHeads,identity_for
from preview_runtime import empty_decoded
from posepriv_coach.avatar_image_source import AvatarImageSource
from posepriv_coach.contracts import CameraView

VIEWS=('front','side')


@dataclass
class SourceTicket:
    pair: object
    source: AvatarImageSource
    generation: int
    roi: object=None
    pixels: bytes|None=None
    prior_source: object=None
    result: object=None
    submitted: bool=False
    committed: bool=False
    prepare_started_ns: int=0
    prepared_ns: int=0
    submitted_ns: int=0
    decoded_ns: int=0


@dataclass
class PairTicket:
    sequence: int
    decoded: dict
    sources: dict
    epoch: object
    tickets: dict=field(default_factory=dict)
    next_view: int=0


@dataclass
class ViewState:
    rect: object=None
    prior_source: object=None
    previous_input: object=None
    epoch: object=None
    generation: int=0
    pending: deque=field(default_factory=deque)
    committed_source: object=None


class StreamingPoseActor:
    """Core is stepped by one owner; offer/take/EOF/cancel are thread safe.

    Two live image pairs and transport capacity2..4 let the next pair start before
    the previous pair's last tail completes. Each source owns its exact ROI.
    A pending crop never reads ROI from a later completion during decode.
    """
    def __init__(self,transport,prepare,decode,*,capacity=4,max_pairs=2,rotations=None):
        if type(capacity)is not int or not 2<=capacity<=4 or max_pairs!=2:
            raise ValueError('this actor profile requires two..four tickets/two live pairs')
        self.transport=transport;self.prepare=prepare;self.decode=decode
        self.capacity=capacity;self.max_pairs=max_pairs;self.rotations=rotations or dict(front=0,side=0)
        self.views={v:ViewState()for v in VIEWS};self.pairs=OrderedDict();self.inflight={}
        self.active=None;self._latest=None;self._ready=None;self._changed=Condition()
        self._last_offered={};self._sequence=0;self._input_eof=False;self._transport_eof=False
        self.done=False;self.cancelled=False;self.error=None;self.thread=None
        self.counts=dict(offered_pairs=0,replaced_input_pairs=0,prepared_sources=0,
            submitted_sources=0,completed_neural_sources=0,committed_empty_sources=0,
            completed_pairs=0,replaced_output_pairs=0,taken_pairs=0,max_live_pairs=0,
            max_inflight_sources=0,cancelled_source_tickets=0)

    def offer(self,decoded,*,epoch=0):
        sources={}
        for view in VIEWS:
            frame=decoded['captured'][view][0];record=decoded['selected'][view]
            if ((frame.frame_id,frame.timestamp_ns)!=record[:2] or frame.view is not CameraView(view)
                    or frame.pixel_format!='bgr8' or frame.payload.shape!=(frame.height,frame.width,3)):
                raise ValueError('stream image and original physical ticket differ')
            sources[view]=AvatarImageSource(view,frame.frame_id,frame.timestamp_ns,
                frame.width,frame.height,self.rotations[view])
        with self._changed:
            if self._input_eof or self.cancelled:raise RuntimeError('stream input is closed')
            for view,source in sources.items():
                previous=self._last_offered.get(view)
                if previous is not None and previous[0]==epoch and (
                        source.frame_id<=previous[1].frame_id or source.timestamp_ns<=previous[1].timestamp_ns):
                    raise ValueError('non-increasing physical source within epoch')
            for view,source in sources.items():self._last_offered[view]=(epoch,source)
            # Decoded cv2 images transfer ownership; no shared/reused decode arena
            # is enabled in this profile. Retained source tickets keep them alive.
            for view in VIEWS:decoded['captured'][view][0].payload.flags.writeable=False
            self._sequence+=1;pair=PairTicket(self._sequence,decoded,sources,epoch)
            self.counts['offered_pairs']+=1
            if self._latest is not None:self.counts['replaced_input_pairs']+=1
            self._latest=pair;self._changed.notify_all()

    def seed_roi(self,view,rect,source,*,epoch=0):
        """Explicit reviewed/test detector seed, only before actor has work."""
        if self.pairs or self.inflight or self.thread is not None:raise RuntimeError('seed before start')
        state=self.views[view];state.rect=tuple(rect);state.prior_source=source;state.epoch=epoch

    def take(self,timeout=0):
        with self._changed:
            self._changed.wait_for(lambda:self._ready is not None or self.done or self.cancelled or self.error is not None,timeout)
            if self.cancelled:return None
            if self.error is not None:raise self.error
            value=self._ready;self._ready=None
            if value is not None:self.counts['taken_pairs']+=1
            return value

    def finish_eof(self):
        with self._changed:self._input_eof=True;self._changed.notify_all()

    def cancel(self):
        with self._changed:
            if self.cancelled:return
            self.cancelled=True;self._latest=None;self._ready=None;self._changed.notify_all()
        self.transport.cancel()

    @staticmethod
    def geometry(source):return source.view,source.width,source.height,source.rotation_degrees

    def _prepare_one(self,pair,view):
        source=pair.sources[view];state=self.views[view];previous=state.previous_input
        if previous is not None and (pair.epoch!=state.epoch or self.geometry(source)!=self.geometry(previous)
                or source.timestamp_ns-previous.timestamp_ns>250_000_000):
            state.generation+=1;state.rect=None;state.prior_source=None
        state.previous_input,state.epoch=source,pair.epoch
        ticket=SourceTicket(pair,source,state.generation,prepare_started_ns=monotonic_ns())
        image=pair.decoded['captured'][view][0].payload
        # prepare may seed a matching asynchronous detector prior, but does not
        # install a pending landmark slot or update ROI using unfinished heads.
        prepared=self.prepare(source,image,state,pair.epoch)
        if state.prior_source is not None and (
                self.geometry(state.prior_source)!=self.geometry(source) or
                state.prior_source.timestamp_ns>source.timestamp_ns):
            raise ValueError('ROI prior is not a causal matching physical image')
        ticket.prepared_ns=monotonic_ns();ticket.prior_source=state.prior_source
        if prepared is None:
            ticket.result=empty_decoded(source);ticket.decoded_ns=ticket.prepared_ns
        else:
            if prepared.source!=source:raise ValueError('prepared crop changed original source')
            ticket.roi,ticket.pixels=prepared.roi,prepared.input_q7
        pair.tickets[view]=ticket;state.pending.append(ticket)
        self.counts['prepared_sources']+=1
        return ticket

    def _receive(self):
        progress=False
        while True:
            response=self.transport.take(timeout=0)
            if response is None:break
            identity,heads=response;ticket=self.inflight.pop(tuple(identity),None)
            if ticket is None:raise ValueError('completion has no owned source/ROI ticket')
            h=TailHeads(tuple(identity),heads['head195'],heads['world117'],heads['presence_logit'])
            ticket.result=self.decode(ticket.source,ticket.roi,h,heads['heat233'])
            if ticket.result.source!=ticket.source:raise ValueError('decoded result changed source')
            ticket.decoded_ns=monotonic_ns();self.counts['completed_neural_sources']+=1;progress=True
        return progress

    def _commit(self):
        progress=False
        for view in VIEWS:
            state=self.views[view]
            while state.pending and state.pending[0].result is not None:
                ticket=state.pending.popleft();source=ticket.source
                if state.committed_source is not None and ticket.pair.epoch==state.committed_source[0] and (
                        source.frame_id<=state.committed_source[1].frame_id or
                        source.timestamp_ns<=state.committed_source[1].timestamp_ns):
                    raise ValueError('out-of-order per-physical-view commit')
                state.committed_source=(ticket.pair.epoch,source)
                if ticket.submitted and ticket.generation==state.generation:
                    rect=ticket.result.next_roi()
                    state.rect=rect if rect is not None and min(rect[2:4])>0 else None
                    state.prior_source=source
                if not ticket.submitted:self.counts['committed_empty_sources']+=1
                ticket.committed=True;progress=True
        while self.pairs:
            sequence,pair=next(iter(self.pairs.items()))
            if len(pair.tickets)!=2 or not all(t.committed for t in pair.tickets.values()):break
            self.pairs.pop(sequence);work=self._work(pair)
            # A ticket's owning pair also holds its tickets. Break that small
            # cycle immediately so full source images need no cyclic-GC delay.
            for ticket in pair.tickets.values():ticket.pair=None;ticket.pixels=None
            pair.tickets.clear()
            with self._changed:
                if self._ready is not None:self.counts['replaced_output_pairs']+=1
                self._ready=work;self.counts['completed_pairs']+=1;self._changed.notify_all()
            progress=True
        return progress

    def _work(self,pair):
        tickets=pair.tickets;original=pair.decoded
        stages={}
        for view,ticket in tickets.items():
            stages[view]=dict(decode_rotate=original['captured'][view][1]/1e6,
                roi_crop_prepare=(ticket.prepared_ns-ticket.prepare_started_ns)/1e6,
                stream_submit_to_decode=(ticket.decoded_ns-ticket.submitted_ns)/1e6 if ticket.submitted else 0.)
        return dict(selected=original['selected'],poses={v:tickets[v].result.pose for v in VIEWS},
            world_observations={v:tickets[v].result.world for v in VIEWS},
            full33_presence={v:tickets[v].result.presence_score for v in VIEWS},
            images={v:original['captured'][v][0].payload for v in VIEWS},stages=stages,
            begin=original['begin'],inferred=max(t.decoded_ns for t in tickets.values()),
            fetch_started=original['fetch_started'],decode_ready_ns=max(original['captured'][v][2]for v in VIEWS),
            model_begin_ns=min(t.prepare_started_ns for t in tickets.values()),
            dpu_frames=self.counts['completed_neural_sources'],pose_frames=self.counts['completed_neural_sources'],
            pose_backend='full33_dpu_tail4',scoring_profile='full33_raw17_candidate_v1',
            stream_pair_sequence=pair.sequence,capture_epoch=pair.epoch,
            stream_sources={v:dict(identity=identity_for(tickets[v].source),
                rect=None if tickets[v].roi is None else tickets[v].roi.rect,
                prior_source=None if tickets[v].prior_source is None else identity_for(tickets[v].prior_source),
                actual_neural=tickets[v].submitted)for v in VIEWS},
            tail4_model_stats=self.stats())

    def step(self):
        """Deterministic one-owner transition; one new source per step."""
        if self.done:return False
        if self.cancelled:
            self.counts['cancelled_source_tickets']=sum(len(s.pending)for s in self.views.values())
            for pair in self.pairs.values():
                for ticket in pair.tickets.values():ticket.pair=None;ticket.pixels=None
                pair.tickets.clear()
            self.inflight.clear();self.pairs.clear();self.active=None
            for state in self.views.values():state.pending.clear()
            self.done=True;return True
        progress=self._receive();progress=self._commit()or progress
        if self.active is None and len(self.pairs)<self.max_pairs:
            with self._changed:pair=self._latest;self._latest=None
            if pair is not None:
                self.pairs[pair.sequence]=pair;self.active=pair;progress=True
                self.counts['max_live_pairs']=max(self.counts['max_live_pairs'],len(self.pairs))
        pair=self.active
        if pair is not None and len(self.inflight)<self.capacity:
            view=VIEWS[pair.next_view]
            ticket=pair.tickets.get(view)or self._prepare_one(pair,view)
            accepted=True
            if ticket.result is None and not ticket.submitted:
                accepted=self.transport.submit(identity_for(ticket.source),ticket.pixels)
                if accepted:
                    ticket.submitted=True;ticket.submitted_ns=monotonic_ns()
                    self.inflight[identity_for(ticket.source)]=ticket
                    self.counts['submitted_sources']+=1
                    self.counts['max_inflight_sources']=max(self.counts['max_inflight_sources'],len(self.inflight))
            if accepted:
                pair.next_view+=1;progress=True
                if pair.next_view==2:self.active=None
        progress=self._commit()or progress
        with self._changed:input_done=self._input_eof and self._latest is None
        if input_done and self.active is None and not self._transport_eof:
            self.transport.finish_eof();self._transport_eof=True;progress=True
        if self._transport_eof and self.transport.eof and not self.inflight and not self.pairs:
            if self.transport.drain(timeout=1):raise ValueError('unclaimed source at stream EOF')
            self.done=True
            with self._changed:self._changed.notify_all()
            progress=True
        return progress

    def start(self):
        if self.thread is not None:return
        self.transport.start();self.thread=Thread(target=self._run,name='tail4-causal-actor',daemon=True);self.thread.start()

    def _run(self):
        try:
            while not self.done:
                if not self.step():
                    with self._changed:self._changed.wait(.001)
        except BaseException as exc:
            if not self.cancelled:self.error=exc
            self.transport.cancel()
            if self.cancelled:self.step()
            self.done=True
            with self._changed:self._changed.notify_all()

    def close(self,timeout=7):
        explicitly_cancelled=self.cancelled
        self.finish_eof()
        if self.thread is not None:
            self.thread.join(timeout)
            if self.thread.is_alive():self.cancel();self.thread.join(timeout=1)
            if self.thread.is_alive():raise RuntimeError('stream actor failed to stop')
        if self.error is not None:raise self.error
        if self.cancelled and not explicitly_cancelled:raise RuntimeError('stream actor cancelled before clean EOF')

    def stats(self):
        with self._changed:
            return dict(self.counts,frames=self.counts['completed_neural_sources'],
                input_pending=int(self._latest is not None),output_pending=int(self._ready is not None),
                live_pairs=len(self.pairs),inflight_sources=len(self.inflight),done=self.done,
                cancelled=self.cancelled,capacity=self.capacity,max_pairs=self.max_pairs,
                transport=self.transport.stats(),online_compute='KV260 ARM+DPU+Tail4 PL')
