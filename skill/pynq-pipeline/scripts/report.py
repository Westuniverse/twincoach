#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Render a pipeline result without upgrading software evidence to hardware claims."""
import argparse
import json
from pathlib import Path

def render(result):
    environment=('PYNQ 设备执行' if result['hardware_executed'] else
                 'PYNQ 设备执行未开始' if result.get('backend')=='pynq' else '软件模型执行')
    verdict='通过' if result['passed'] else '不通过'
    if 'error' in result:
        return ('# 数据通路验证\n\n'+f'环境：{environment}。结果：{verdict}。\n\n'
                +f'错误：{result["error_type"]}: {result["error"]}\n\n'
                +f'保留缓冲数：{result["buffers_retained"]}。不得把失败记录计为完成确认。\n')
    return ('# 数据通路验证\n\n'
        f'样例：{result["case"]}。环境：{environment}。逐元素比对：{verdict}。\n\n'
        '| 项目 | 内容 |\n| --- | --- |\n'
        f'| 数据类型 | {result["dtype"]} |\n| 元素数 / 字节数 | {result["elements"]} / {result["bytes"]} |\n'
        f'| 计量边界 | {result["measurement_scope"]} |\n\n'
        '执行顺序：'+ ' → '.join(result['events'])+'。\n\n'
        '此记录用于接口、执行顺序和数值比对。持续吞吐、资源、功耗及屏幕延迟需要对应测量。\n')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('input',type=Path);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();args.output.write_text(render(json.loads(args.input.read_text())));print(args.output)
