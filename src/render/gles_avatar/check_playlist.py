"""Offline native-playlist preflight; never opens graphics or DMA devices."""
from pathlib import Path
import argparse
import json
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
from replay_sources import load_playlist,chapter_report,ReplayScoring


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--playlist',type=Path,required=True)
    parser.add_argument('--fps',type=float,default=16)
    parser.add_argument('--score-config',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():parser.error('output must be a new file; keep all input and prior reports')
    chapters=load_playlist(args.playlist,fps=args.fps)
    scorer=ReplayScoring(args.score_config) if args.score_config else None
    reports=[]
    for chapter_id,chapter in enumerate(chapters):
        report=chapter_report(chapter)
        report['score_changes']=[]
        if scorer is not None:
            previous=None
            for sample in chapter.samples:
                state=scorer.update(chapter_id,chapter,sample)
                if state is None:continue
                score=state.completed_score
                value=[state.completed_repetition_count,score.conformance_percent if score is not None and score.valid else None]
                if value!=previous:
                    report['score_changes'].append(value);previous=value
            report['consumed_raw_pairs']=scorer.consumed_raw+1
            assert report['consumed_raw_pairs']==len(chapter.raw_pairs)
        reports.append(report)
    result=dict(scope='host schedule/index/scoring preflight; no GPU, DMA, HDMI or generation FPS',
                fps=args.fps,frames=sum(len(c.samples) for c in chapters),
                scheduled_seconds=sum(len(c.samples) for c in chapters)/args.fps,chapters=reports)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as output:
        output.write(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='chapters'},ensure_ascii=False))


if __name__=='__main__':main()
