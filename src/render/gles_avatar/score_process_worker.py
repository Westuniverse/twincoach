"""Private inherited-FD worker: original replay scoring, no graphics imports."""
from pathlib import Path
import argparse
from multiprocessing.connection import Connection
import resource
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
from replay_sources import load_playlist,ReplayScoring
from score_process import PROTOCOL_VERSION


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fd',type=int,required=True)
    parser.add_argument('--playlist',type=Path,required=True)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--fps',type=float,required=True)
    args=parser.parse_args(argv)
    connection=Connection(args.fd)
    try:
        chapters=load_playlist(args.playlist,fps=args.fps)
        scorer=ReplayScoring(args.config)
        connection.send(('ready',dict(protocol_version=PROTOCOL_VERSION,chapters=len(chapters),frames=[len(c.samples) for c in chapters],
            raw_pairs=[len(c.raw_pairs) for c in chapters],
            max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)))
        active_chapter_id=active_slot=None
        last_local=-1
        while True:
            message=connection.recv()
            if message==('close',None):
                return 0
            if not isinstance(message,tuple) or len(message)!=2 or message[0]!='score':
                raise ValueError('invalid score worker request')
            request=message[1]
            if not isinstance(request,tuple) or len(request)!=3 or any(type(v) is not int or v<0 for v in request):
                raise ValueError('invalid chapter/frame indices')
            chapter_id,chapter_slot,local_frame=request
            if chapter_id==active_chapter_id:
                if chapter_slot!=active_slot:
                    raise ValueError('chapter slot change requires a new chapter_id')
                if local_frame<last_local:
                    raise ValueError('backward score frame requires a new chapter_id')
            chapter=chapters[chapter_slot]
            sample=chapter.samples[local_frame]
            started=time.perf_counter_ns()
            state=scorer.update(chapter_id,chapter,sample)
            elapsed=time.perf_counter_ns()-started
            active_chapter_id=chapter_id;active_slot=chapter_slot;last_local=local_frame
            connection.send(('result',request,state,elapsed,
                dict(max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                     consumed_raw_pairs=scorer.consumed_raw+1)))
    except EOFError:
        return 0
    except Exception as error:
        try:connection.send(('error',f'{type(error).__name__}: {error}'))
        except (OSError,EOFError):pass
        return 2
    finally:
        connection.close()


if __name__=='__main__':raise SystemExit(main())
