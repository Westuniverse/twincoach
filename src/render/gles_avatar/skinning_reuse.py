"""One completed immutable output, keyed by the complete hardware inputs."""
from __future__ import annotations


class LastSkinningOutput:
    def __init__(self):
        self.reset()

    def reset(self):
        self._key=None
        self._backend=None
        self._output=None

    def execute(self, plan, backend):
        key=(plan.matrix_writes,plan.matrix_valid,plan.vertex_requests)
        if self._backend is backend and self._key==key:
            return self._output,True
        # A failed transfer must not leave an older result eligible for reuse.
        self.reset()
        output=plan.execute(backend,program_matrices=backend.program_matrices)
        if isinstance(getattr(output,'padded_data',None),bytes):
            self._key=key
            self._backend=backend
            self._output=output
        return output,False
