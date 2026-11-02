"""Bounded media pacing and observed GPU-finish/swap-return cadence, not scanout."""
from collections import deque


class MoviePresentation:
    def __init__(self):
        self.origin_ns = self.first_pts_ns = self.last_pts_ns = None
        self.first_confirmed_ns = self.last_confirmed_ns = self.warm_ns = None
        self.total_frames = self.loop_frames = self.complete_loops = 0
        self.first_loop_frames = None
        self.recent = deque(maxlen=256)
        self.sampled_windows = self.windows_below_24 = 0
        self.minimum_window_frames = None
        self.maximum_gap_ns = 0
        self.statistics_saturated = False

    def deadline(self, pts_ns, now_ns):
        if type(pts_ns) is not int or pts_ns < 0:
            raise ValueError('movie PTS must be a nonnegative integer')
        if self.last_pts_ns is not None and pts_ns <= self.last_pts_ns:
            raise ValueError('movie PTS must increase within each loop')
        if self.first_pts_ns is None:
            self.first_pts_ns, self.origin_ns = pts_ns, now_ns
        self.last_pts_ns = pts_ns
        return self.origin_ns + pts_ns - self.first_pts_ns

    def confirmed(self, now_ns):
        if self.last_confirmed_ns is not None:
            if now_ns <= self.last_confirmed_ns:
                raise ValueError('confirmed frame clock must increase')
            self.maximum_gap_ns = max(self.maximum_gap_ns, now_ns - self.last_confirmed_ns)
        else:
            self.first_confirmed_ns = now_ns
        self.last_confirmed_ns = now_ns
        self.total_frames += 1
        self.loop_frames += 1
        while self.recent and self.recent[0] <= now_ns - 1_000_000_000:
            self.recent.popleft()
        self.statistics_saturated |= len(self.recent) == self.recent.maxlen
        self.recent.append(now_ns)
        if self.total_frames == 9:
            self.warm_ns = now_ns
        if self.warm_ns is not None and now_ns - self.warm_ns >= 1_000_000_000:
            count = len(self.recent)
            self.sampled_windows += 1
            self.windows_below_24 += count < 24
            self.minimum_window_frames = (count if self.minimum_window_frames is None
                                          else min(self.minimum_window_frames, count))

    def end_loop(self):
        if self.loop_frames == 0:
            raise ValueError('movie loop has no frames')
        if self.first_loop_frames is None:
            self.first_loop_frames = self.loop_frames
        elif self.loop_frames != self.first_loop_frames:
            raise ValueError('repeated movie loop frame count changed')
        self.complete_loops += 1
        self.loop_frames = 0
        self.origin_ns = self.first_pts_ns = self.last_pts_ns = None

    def report(self):
        seconds = ((self.last_confirmed_ns - self.first_confirmed_ns) / 1e9
                   if self.total_frames > 1 else 0.)
        return dict(total_confirmed_frames=self.total_frames,
                    complete_loops=self.complete_loops, current_loop_frames=self.loop_frames,
                    frames_in_first_complete_loop=self.first_loop_frames,
                    confirmed_seconds=seconds,
                    confirmed_fps=(self.total_frames - 1) / seconds if seconds else None,
                    sampled_one_second_windows=self.sampled_windows,
                    minimum_sampled_one_second_confirmed_frames=self.minimum_window_frames,
                    sampled_windows_below_24=self.windows_below_24,
                    maximum_confirmed_gap_ms=self.maximum_gap_ns / 1e6,
                    statistics_timestamp_limit=self.recent.maxlen,
                    statistics_saturated=self.statistics_saturated,
                    cadence_scope='one-second (T-1s,T] windows at confirmed frame endpoints, after 8 warm frames; counts are lower bounds if saturated; not every continuous endpoint or physical scanout')
