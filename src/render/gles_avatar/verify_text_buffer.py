"""Compare synthetic Chinese text pixels on the actual board Mali context."""
import argparse
import json
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    import numpy as np
    from egl_context import EglSession,clearcolor,clear,finish,read,info,glerror
    from benchmark import build_program
    from gles_text import GpuText
    from posepriv_coach.text_overlay import default_cjk_font
    original=[('合成文字测试 · 已完成 7 次',28,580,(230,235,240)),
              ('',20,600,(0,0,0)),('上次得分 85.5',40,620,(150,185,205))]
    moved=[(t,x+3,y-2,col) for t,x,y,col in original]
    colored=[(t,x,y,(100,110,120)) for t,x,y,_ in original]
    frames=[[('',0,0,(0,0,0))],original,original,moved,list(reversed(moved)),colored,[],original,[('Ω',5,9,(12,34,56))]]
    characters=''.join(text for text,_,_,_ in original)
    checks=[]
    with EglSession(1280,720):
        renderer=info(0x1F01).decode()
        if 'Mali' not in renderer:raise RuntimeError('actual Mali renderer required')
        baseline=GpuText(default_cjk_font(),characters,build_program)
        candidate=GpuText(default_cjk_font(),characters,build_program,reuse_buffer=True)
        def pixels(text,lines):
            clearcolor(.05,.08,.11,1);clear(0x4000|0x0100)
            text.draw_lines(lines);finish()
            image=np.empty((720,1280,4),np.uint8)
            read(0,0,1280,720,0x1908,0x1401,image.ctypes.data)
            if glerror()!=0:raise RuntimeError('GL error during synthetic text comparison')
            return image
        for index,lines in enumerate(frames):
            before=pixels(baseline,lines);after=pixels(candidate,lines)
            checks.append(dict(frame=index,equal=bool(np.array_equal(before,after)),
                               differing_pixels=int(np.any(before!=after,axis=2).sum()),
                               max_channel_difference=int(np.abs(before.astype(np.int16)-after.astype(np.int16)).max())))
        result=dict(scope='actual Mali 1280x720 synthetic text readback; no DMA, avatar, FPS or physical scanout claim',
                    gpu_renderer=renderer,frames=len(frames),checks=checks,
                    baseline_uploads=baseline.buffer_uploads,candidate_uploads=candidate.buffer_uploads,
                    missing_glyphs_equal=baseline.missing==candidate.missing,
                    passed=all(row['equal'] for row in checks) and baseline.missing==candidate.missing)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as output:
        json.dump(result,output,ensure_ascii=False,indent=2);output.write('\n')
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
