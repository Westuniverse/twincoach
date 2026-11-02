#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Change only explicit hybrid model/profile paths in a retained45s config."""
import argparse
import json
from pathlib import Path


def main():
    p=argparse.ArgumentParser();p.add_argument('--baseline',required=True);p.add_argument('--p4-config',required=True)
    p.add_argument('--worker',required=True);p.add_argument('--output',required=True);p.add_argument('--package-root',required=True);a=p.parse_args()
    cfg=json.loads(Path(a.baseline).read_text());cfg=cfg['config']if'config'in cfg else cfg
    if cfg.get('tail4_avatar_view')!='side' or cfg.get('preview_decoded_arena')is not True:
        raise ValueError('use the retained side-avatar decoded-arena baseline')
    if cfg.get('preview_roi_crop')is True:raise ValueError('retained CPU crop baseline required')
    cfg.update(preview_pose_backend='hybrid_full33_side_lightning_front',
        tail4_scoring_profile='hybrid_side_full33_front_lightning_raw17_v1',
        tail4_stream_capacity=4,
        hybrid_package_root=a.package_root,hybrid_public_entry=str(Path(a.package_root)/'native/profile/entry'),
        hybrid_pair_worker=a.worker,hybrid_lightning_config=a.p4_config)
    Path(a.output).write_text(json.dumps(cfg,indent=2)+'\n')


if __name__=='__main__':main()
