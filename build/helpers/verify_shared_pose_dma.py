#!/usr/bin/env python3
"""单 DMA 黄金对拍；按镜像显式加入光照/光栅/蒙皮，不加载镜像或重试。"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import statistics
import sys
from time import monotonic_ns

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from posepriv_coach.linux_shared_pose_dma import LinuxSharedPoseDma
from posepriv_coach.pl_metrics import AxisBeat
from posepriv_coach.pl_shared_pose_accel import reference_shared_pose_output


def dma_batches(*, include_lighting=False):
    """复用 RTL 混合黄金，并补齐三角测量的最大 DMA 包和非整齐长度。"""
    from verify_shared_pose_rtl_bridge import verification_batches
    batches = list(verification_batches(include_lighting=include_lighting))
    rectified = next(beats for op, beats in batches if op == 2 and len(beats) > 1)
    for size in (257, 8192):
        beats = tuple(AxisBeat(rectified[index % len(rectified)].data, index == size - 1)
                      for index in range(size))
        batches.append((2, beats))
    return tuple(batches)


def verify_batches(transport, batches, repetitions, *, evidence="host_mock", clock=monotonic_ns):
    # 黄金计算和输入生成不计入 exchange 时间；该时间仍包含 Python 打包、
    # 两次用户复制、内核校验/选择及 DMA 等待，不是纯 PL 或零拷贝延迟。
    prepared = tuple((op, beats, reference_shared_pose_output(op, beats))
                     for op, beats in batches)
    records = []
    report = {"schema": "posepriv.shared_pose_dma_golden.v1", "evidence": evidence,
              "passed": False, "repetitions": repetitions, "transactions": records,
              "timing_scope": "Python exchange including packing/copy/validation/ioctl/DMA wait; excludes golden calculation"}
    for repetition in range(repetitions):
        for batch_index, (operator, inputs, golden) in enumerate(prepared):
            row = {"repetition": repetition, "batch": batch_index, "operator": operator,
                   "input_bytes": len(inputs) * 16, "output_bytes": len(golden) * 16,
                   "start_monotonic_ns": clock(), "byte_equal": False}
            records.append(row)
            try:
                output = transport.exchange(operator, inputs)
                row["end_monotonic_ns"] = clock()
                row["exchange_ns"] = row["end_monotonic_ns"] - row["start_monotonic_ns"]
                if output != golden:
                    raise RuntimeError("shared DMA output bytes/count/final packet marker differ from reference")
                row["byte_equal"] = True
            except Exception as error:
                row["error"] = f"{type(error).__name__}: {error}"
                report["error"] = row["error"]
                return report
    groups = {}
    for row in records:
        key = f"op{row['operator']}_tx{row['input_bytes']}_rx{row['output_bytes']}"
        groups.setdefault(key, []).append(row["exchange_ns"])
    report["latency_by_packet"] = {
        key: {"samples": len(values), "median_ns": statistics.median(values),
              "p95_ns": sorted(values)[math.ceil(0.95 * len(values)) - 1],
              "max_ns": max(values)} for key, values in groups.items()
    }
    report["passed"] = True
    return report



def pps3_config_matches(config):
    expected={"length_bits":20,"tx_capacity_bytes":1048560,"rx_capacity_bytes":131072,
              "capability_mask":"0x1f","raster_abi":"PXT1/PXR1v1"}
    return (config.get("config_id")=="0x50505333" and all(config.get(k)==v for k,v in expected.items())
            and config.get("operations")==["joint_metrics","motion_summary_single_view",
                "rectified_triangulation","triangle_lighting","textured_tile"])


def pps4_config_matches(config):
    expected=dict(config_id='0x50505334',length_bits=20,stream_bits=128,
        tx_capacity_bytes=1048560,rx_capacity_bytes=131072,capability_mask='0x3f',
        raster_abi='PXT1/PXR1v1',skinning_abi='avatar_skinning_packet.v0.3',
        skinning_matrix_bytes=512,skinning_output_stride=16,
        max_runtime_packet_bytes_by_operation=[131072]*4+[1048560,131584],
        input_packet_bytes_by_operation=['16N','16N','16N','32N','PXT1','512+16N'],
        output_packet_bytes_by_operation=['16N','16','16N','16N','PXR1','16N'],
        input_items_range_by_operation=[[1,8192],[1,8192],[1,8192],[1,4096],[0,13106],[1,8192]],
        operations=['joint_metrics','motion_summary_single_view','rectified_triangulation',
                    'triangle_lighting','textured_tile','vertex_skinning'])
    return (type(config.get('skinning_resource_shared')) is bool and
            all(config.get(key)==value for key,value in expected.items()))


def pps4_skinning_config_matches(config):
    from pps4_roi1_profile import pps4_roi_config_matches
    legacy=(config.get("compatible","posepriv,shared-pose-dma-v1")=="posepriv,shared-pose-dma-v1" and pps4_config_matches(config))
    return legacy or pps4_roi_config_matches(config)


def load_board_manifest(expected_boot, runtime_manifest):
    """Bind an explicit runtime candidate to this boot; no device writes."""
    if Path('/proc/sys/kernel/random/boot_id').read_text().strip() != expected_boot:
        raise ValueError('boot ID 不匹配')
    manifest_bytes = Path(runtime_manifest).read_bytes()
    if manifest_bytes != Path('/boot/firmware/unified_sd_candidate.json').read_bytes():
        raise ValueError('运行包与当前 SD 候选不匹配')
    return json.loads(manifest_bytes.decode('utf-8-sig'))


def raster_packets(generation=0):
    from posepriv_coach.avatar_raster import RasterCommand,prepare_raster_command
    from posepriv_coach.avatar_raster_packet import pack_texture_upload,pack_tile,response_header,reference_tile
    texture=bytes(value for y in range(512) for x in range(256)
                  for value in ((x+generation*31)&255,(3*y+generation*53)&255,(x^y^generation)&255))
    a=prepare_raster_command(((0,0),(32,0),(0,32)),((0.,0.),(1.,0.),(0.,1.)),(0,0,32,32),256)
    b=RasterCommand((4064,4095,4064,4095),((1,0,0),)*3,(-65536,33488896),
                    (98304,-32768,16384,-98304),149)
    packets=[("upload",pack_texture_upload(texture),response_header(1,0))]
    for label,origin,commands in (("triangle",(0,0),[a]),("border",(4064,4064),[b]),
                                  ("empty",(32,0),[]),("after_all_legacy_modes",(0,0),[a])):
        packets.append((label,pack_tile(origin,0x203040,commands),reference_tile(origin,0x203040,commands,texture)))
    return tuple(packets)


def verify_raster(transport,repetitions,*,evidence="host_mock",clock=monotonic_ns):
    # Prepare goldens before timing; legacy interleaves prove texture residency.
    old={}
    for op,beats in dma_batches(include_lighting=True):
        if op not in old:old[op]=(beats,reference_shared_pose_output(op,beats))
    report=dict(schema="posepriv.raster_dma_golden.v1",evidence=evidence,passed=False,transactions=[],
                timing_scope="exchange includes Python/kernel checks and copy/ioctl/DMA; excludes golden, not pure PL FPS")
    for repetition in range(repetitions):
        for index,(label,payload,golden) in enumerate(raster_packets(repetition)):
            row=dict(repetition=repetition,label=label,operator=4,input_bytes=len(payload),output_bytes=len(golden),byte_equal=False)
            report["transactions"].append(row)
            try:
                start=clock();actual=transport.exchange_raster_packet(payload);row["exchange_ns"]=clock()-start
                if actual!=golden:raise RuntimeError("PPS3 raster response bytes differ from software golden")
                row["byte_equal"]=True
                op=index%4;inputs,expected=old[op]
                row=dict(repetition=repetition,label="legacy_interleave",operator=op,input_bytes=len(inputs)*16,
                         output_bytes=len(expected)*16,byte_equal=False)
                report["transactions"].append(row)
                start=clock();actual=transport.exchange(op,inputs);row["exchange_ns"]=clock()-start
                if actual!=expected:raise RuntimeError("legacy interleave differs after renderer transaction")
                row["byte_equal"]=True
            except Exception as error:
                row["error"]=report["error"]=f"{type(error).__name__}: {error}"
                return report
    report["passed"]=True
    return report

def skinning_packets():
    from posepriv_coach.avatar_skinning import (AffineMatrixQ1_14,MatrixWrite,VertexQ1_14,
        VertexSkinningRequest,ReferencePacketAvatarTransport,pack_skinning_packet)
    writes=tuple(MatrixWrite(i,AffineMatrixQ1_14((16384,0,0,(i-8)*67,
        0,16384,0,(i%3-1)*31,0,0,16384,i*101))) for i in range(16))
    reference=ReferencePacketAvatarTransport()
    packets=[]
    for count in (1,17,257,8192):
        requests=tuple(VertexSkinningRequest(VertexQ1_14((i*7919&65535)-32768,
            32767-(i*10973&65535),(i*337&65535)-32768),
            tuple((i+j)&15 for j in (0,1,7,11)),
            ((32768,0,0,0),(8192,)*4,(1,16383,16383,1))[i%3]) for i in range(count))
        payload=pack_skinning_packet(writes,requests)
        packets.append((count,payload,reference.transfer(payload)))
    return tuple(packets)


def verify_skinning(transport,repetitions,*,include_raster=False,evidence='host_mock',clock=monotonic_ns):
    prepared=skinning_packets()
    raster=raster_packets() if include_raster else ()
    rows=[]
    report=dict(schema='posepriv.skinning_dma_golden.v1',evidence=evidence,passed=False,transactions=rows,
        timing_scope='exchange includes Python/kernel validation/copy/ioctl/DMA; excludes goldens, not pure PL FPS')
    def exchange(op,payload,golden,label,repetition):
        row=dict(operator=op,input_bytes=len(payload),output_bytes=len(golden),label=label,
                 repetition=repetition,byte_equal=False)
        rows.append(row)
        start=clock()
        if op==5:output=transport.exchange_skinning_packet(payload)
        elif op==4:output=transport.exchange_raster_packet(payload)
        else:output=transport.exchange_bytes(op,payload)
        row['exchange_ns']=clock()-start
        if output!=golden:raise RuntimeError('shared skinning/interleave bytes differ from golden')
        row['byte_equal']=True
    try:
        for repetition in range(repetitions):
            if raster:exchange(4,raster[0][1],raster[0][2],'texture_upload',repetition)
            for count,payload,golden in prepared:
                exchange(5,payload,golden,f'vertices_{count}',repetition)
                exchange(0,bytes(16),bytes(16),'joint_interleave',repetition)
                if raster:exchange(4,raster[1][1],raster[1][2],'resident_texture_after_skinning',repetition)
    except Exception as error:
        report['error']=f'{type(error).__name__}: {error}'
        if rows:rows[-1]['error']=report['error']
        return report
    report['passed']=True
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-boot", required=True)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--profile", choices=("first", "mixed"), default="mixed")
    parser.add_argument("--include-lighting", action="store_true",
                        help="PPS2/3/4 混合对拍加入模式 3；现役 PPS1 默认不变")
    parser.add_argument("--include-raster", action="store_true", help="PPS3/4: full texture/tile golden and legacy interleaves")
    parser.add_argument("--include-skinning",action="store_true",help="PPS4 only: explicit op5 full golden and cross-mode checks")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("真实 DMA 验证只在 Linux 板端执行；主机测试使用显式 mock")
    if not 1 <= args.repetitions <= 128:
        parser.error("repetitions 必须为 1..128")
    if args.include_lighting and args.profile != "mixed":
        parser.error("光照仅用于 mixed，first 始终为单关节包")
    if args.include_raster and args.profile != "mixed":
        parser.error("raster requires mixed profile; first remains a single joint request")
    if args.include_skinning and args.profile != 'mixed':
        parser.error('skinning requires mixed profile')
    if args.output.exists():
        parser.error("不覆盖旧证据")
    boot_path = Path("/proc/sys/kernel/random/boot_id")
    try:
        manifest = load_board_manifest(args.expected_boot, args.runtime_manifest)
    except ValueError as error:
        parser.error(str(error))
    config = manifest.get("shared_pose_dma", {})
    if (manifest.get("dma_channels") != 1 or config.get("config_id") not in ("0x50505331", "0x50505332", "0x50505333", "0x50505334")
            or config.get("kernel_module") != "posepriv_shared_dma"):
        parser.error("不是匹配的 shared pose DMA 镜像")
    if config.get("config_id") == "0x50505333" and not pps3_config_matches(config):
        parser.error("PPS3 renderer/length/capability manifest mismatch")
    if config.get('config_id')=='0x50505334' and not pps4_skinning_config_matches(config):
        parser.error('PPS4 skinning/length/capability manifest mismatch')
    if args.include_raster and not (pps3_config_matches(config) or pps4_config_matches(config)):
        parser.error("raster requires complete PPS3/PPS4 manifest")
    if args.include_skinning and not pps4_skinning_config_matches(config):
        parser.error('skinning requires complete PPS4 manifest')
    if args.include_lighting and config.get("config_id") not in ("0x50505332", "0x50505333", "0x50505334"):
        parser.error("模式 3 必须使用 PPS2，不能发往现役 PPS1")
    with LinuxSharedPoseDma() as transport:
        batches = ((0, (AxisBeat(bytes(16), True),)),) if args.profile == "first" else dma_batches(
            include_lighting=args.include_lighting)
        report = verify_batches(transport, batches, args.repetitions,
                                evidence="board_linux_shared_dma_ioctl_byte_golden")
        if report["passed"] and args.include_raster:
            report["raster_stage"] = verify_raster(transport,args.repetitions,evidence="board_linux_shared_dma_ioctl_byte_golden")
            report["passed"] = report["raster_stage"]["passed"]
            if not report["passed"]:report["error"] = report["raster_stage"].get("error")
        if report['passed'] and args.include_skinning:
            report['skinning_stage']=verify_skinning(transport,args.repetitions,
                include_raster=args.include_raster,evidence='board_linux_shared_dma_ioctl_byte_golden')
            report['passed']=report['skinning_stage']['passed']
            if not report['passed']:report['error']=report['skinning_stage'].get('error')
    report["profile"] = args.profile
    report["include_lighting"] = args.include_lighting
    report["include_raster"] = args.include_raster
    if args.include_skinning:report['include_skinning']=True
    report["config_id"] = config["config_id"]
    report["boot_id"] = args.expected_boot
    if boot_path.read_text().strip() != args.expected_boot:
        report.update(passed=False, error="boot ID changed during verification")
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"passed": report["passed"], "boot_id": report["boot_id"],
                      "transactions": len(report["transactions"]), "output": str(args.output),
                      "error": report.get("error")}, ensure_ascii=False), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
