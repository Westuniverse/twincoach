"""Render policy only: core absence must not publish a floating partial avatar."""
CORE_MESSAGE_ZH = '等待完整躯干与头部'
CORE_DETAIL_ZH = '暂不显示碎片模型'


class PreviewCoreVisibility:
    CORE_BONES = ('spine','neck','head')

    def __init__(self,bone_names):
        names=tuple(bone_names)
        self.slots=tuple(names.index(name) for name in self.CORE_BONES)

    def missing(self,matrix_valid):
        return tuple(name for name,slot in zip(self.CORE_BONES,self.slots) if not matrix_valid[slot])
