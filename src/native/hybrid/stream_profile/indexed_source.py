# SPDX-License-Identifier: GPL-2.0-only
"""Index-only original source schedule. Two persistent readers, one JPEG in flight."""
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
import bisect,json,statistics

@dataclass(frozen=True)
class Record:
    timestamp_ns:int
    view:str
    frame_id:int
    offset:int
    length:int

@dataclass(frozen=True)
class Event:
    cycle:int
    source_index:int
    offset_ns:int
    record:Record


def load_records(folder):
    records=[]
    for view in('front','side'):
        with(Path(folder)/(view+'.timestamps.jsonl')).open()as f:
            for line in f:
                row=json.loads(line);records.append(Record(row['timestamp_ns'],view,row['frame_id'],row['offset'],row['length']))
    records.sort(key=lambda r:(r.timestamp_ns,r.view))
    return records

class IndexedJpegs:
    def __init__(self,folder):self.folder=Path(folder)
    def __enter__(self):
        self.stack=ExitStack();self.files={v:self.stack.enter_context((self.folder/(v+'.mjpeg')).open('rb'))for v in('front','side')};return self
    def read(self,row):
        f=self.files[row.view];f.seek(row.offset);jpeg=f.read(row.length)
        if len(jpeg)!=row.length:raise ValueError('Truncated replay image')
        return jpeg
    def __exit__(self,*exc):return self.stack.__exit__(*exc)

class Schedule:
    def __init__(self,records,*,duration_ns=None):
        if not records:raise ValueError('replay index is empty')
        if duration_ns is not None and duration_ns<=0:raise ValueError('positive explicit replay duration required')
        self.records=records;self.first=records[0].timestamp_ns;self.source_span_ns=records[-1].timestamp_ns-self.first
        self.offsets=[r.timestamp_ns-self.first for r in records];self.duration_ns=duration_ns;self.views={}
        periods=[]
        for view in('front','side'):
            stamps=[r.timestamp_ns for r in records if r.view==view];steps=[b-a for a,b in zip(stamps,stamps[1:])if b>a]
            normal=statistics.median_low(steps)if steps else None
            self.views[view]=dict(frames=len(stamps),first_original_ns=stamps[0]if stamps else None,last_original_ns=stamps[-1]if stamps else None,normal_interval_ns=normal)
            if normal is not None:periods.append(stamps[-1]-stamps[0]+normal)
        if duration_ns is not None and len(periods)!=2:raise ValueError('loop timing needs original normal intervals in both views')
        self.cycle_period_ns=max([self.source_span_ns+1,*periods])
        for v,item in self.views.items():
            item['loop_same_view_gap_ns']=None if item['first_original_ns']is None else self.cycle_period_ns+item['first_original_ns']-item['last_original_ns']
        if duration_ns is None:
            self.planned_frames=len(records);self.planned_cycles=1;self.last_offset_ns=self.source_span_ns
        else:
            cycles,remainder=divmod(duration_ns,self.cycle_period_ns);tail=bisect.bisect_left(self.offsets,remainder)
            self.planned_frames=cycles*len(records)+tail;self.planned_cycles=cycles+bool(tail)
            self.last_offset_ns=(cycles*self.cycle_period_ns+self.offsets[tail-1])if tail else((cycles-1)*self.cycle_period_ns+self.offsets[-1])
    def events(self):
        cycle=0
        while True:
            for index,row in enumerate(self.records):
                offset=cycle*self.cycle_period_ns+row.timestamp_ns-self.first
                if self.duration_ns is not None and offset>=self.duration_ns:return
                yield Event(cycle,index,offset,row)
            if self.duration_ns is None:return
            cycle+=1
    def metadata(self):
        return dict(input_payload_policy='two_readonly_handles_index_only_no_JPEG_preload',
            source_policy='one original recorded sequence, no looping'if self.duration_ns is None else'cyclic reuse of the same original recorded sequence; not independent long video',
            original_indexed_frames=len(self.records),original_min_ns=self.first,original_max_ns=self.records[-1].timestamp_ns,original_duration_ns=self.source_span_ns,
            requested_duration_ns=self.duration_ns,cycle_period_ns=self.cycle_period_ns,cycle_policy='max per-view(original last-first+median_low positive interval), at least global span+1ns; each same-view seam is >= its original normal interval',
            physical_views=self.views,planned_source_submissions=self.planned_frames,planned_cycles=self.planned_cycles,last_offset_ns=self.last_offset_ns,
            original_short_hot_window_seconds=[15,44],no_pose_or_draw_cache=True,source_inputs_are_not_NN_completion_counts=True)
