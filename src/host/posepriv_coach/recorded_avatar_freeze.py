"""Match a frozen recorded image pair with its last qualified avatar display."""
from dataclasses import dataclass, replace

RECORDED_HOLD_MESSAGE_ZH = '录像间断 · 保持对应姿态'


@dataclass(frozen=True, slots=True)
class RecordedAvatarFrame:
    preview: object
    frozen_source_age_ns: int | None = None


class RecordedAvatarFreeze:
    """Explicit file-replay consumer; it never refreshes an observation.

    Call reset between chapters. A new image or pose identity clears the cache,
    even when no qualified replacement avatar is available.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self._key = None
        self._preview = None
        self._display_timestamp_ns = None

    def apply(self, preview, *, source_ids, display_timestamp_ns):
        pair = preview.render_plan.pair
        ready = max(pair.front.timestamp_ns, pair.side.timestamp_ns)
        if type(display_timestamp_ns) is not int or display_timestamp_ns < ready:
            raise ValueError('recorded freeze requires display time after both sources')
        if self._display_timestamp_ns is not None and display_timestamp_ns < self._display_timestamp_ns:
            raise ValueError('non-monotonic recorded display time')
        if (set(source_ids) != {'front', 'side'} or
                any(type(value) is not int for value in source_ids.values())):
            raise ValueError('recorded freeze requires both integer source IDs')
        key = (tuple(sorted(source_ids.items())), pair.front.frame_id, pair.side.frame_id,
               pair.front.timestamp_ns, pair.side.timestamp_ns)
        if key != self._key:
            self._preview = None
            self._key = key
        self._display_timestamp_ns = display_timestamp_ns
        age = display_timestamp_ns - ready
        if age <= 100_000_000:
            if (preview.reason in ('tracked', 'partial') and
                    len(preview.render_plan.matrix_valid) >= 13 and
                    all(preview.render_plan.matrix_valid[:13])):
                self._preview = preview
            else:
                self._preview = None
            return RecordedAvatarFrame(preview)
        if preview.reason == 'neutral' and self._preview is not None:
            previous = self._preview
            plan = replace(previous.render_plan, pair=pair, points=preview.render_plan.points)
            frozen = replace(previous, render_plan=plan, inferred_slots=tuple(range(13)),
                             reason='recorded_hold', head_mode='recorded_hold', endpoint_errors=())
            return RecordedAvatarFrame(frozen, age)
        self._preview = None
        return RecordedAvatarFrame(preview)
