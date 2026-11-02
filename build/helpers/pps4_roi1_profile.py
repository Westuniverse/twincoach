# SPDX-License-Identifier: GPL-2.0-only
"""Exact PPS4 raster-off/ROI1 manifest profile; old PPS4 remains separate."""
from copy import deepcopy

PURPOSE='single_B3136_PPS4_skinning_ROI1_candidate_not_release'
COMPATIBLE='posepriv,shared-pose-dma-roi-v1'
ROI1=dict(compatible='posepriv,roi-sampler-v1',base='0xa2040000',range='0x1000',
    config_id='0x524f4931',version='0x00010000',device='/dev/posepriv_shared_pose_dma',
    shared_owner=True,additional_dma_channels=0,ioctl='0xc0405270',
    source_pool_bytes=1048576,source_limit_bytes=786416,map_bytes=262144,
    output_bytes=196608,output_layout='NHWC_RGB_Q7',maximum_width=2048,maximum_height=2048,
    map_offset='ALIGN(source_bytes,16)+16',read_quiesced_mask='0x200',
    DMA_coherent_property=False)
SHARED=dict(config_id='0x50505334',capability_mask='0x2f',compatible=COMPATIBLE,
    length_bits=20,stream_bits=128,tx_capacity_bytes=1048560,rx_capacity_bytes=131072,
    skinning_abi='avatar_skinning_packet.v0.3',skinning_matrix_bytes=512,
    skinning_output_stride=16,skinning_resource_shared=True,raster_abi=None,
    raster_texture_upload_bytes=0,raster_tile_output_bytes=0,
    operations=['joint_metrics','motion_summary_single_view','rectified_triangulation',
                'triangle_lighting','disabled','vertex_skinning'],
    max_runtime_packet_bytes_by_operation=[131072]*4+[0,131584],
    input_packet_bytes_by_operation=['16N','16N','16N','32N',None,'512+16N'],
    output_packet_bytes_by_operation=['16N','16','16N','16N',None,'16N'],
    input_items_range_by_operation=[[1,8192],[1,8192],[1,8192],[1,4096],[0,0],[1,8192]])


def shared_profile(original):
    result=deepcopy(original);result.update(deepcopy(SHARED))
    result.update(raster_production_default=False,raster_validation='not_present')
    return result


def pps4_roi_config_matches(config):
    return (isinstance(config,dict) and type(config.get("skinning_resource_shared")) is bool and
        all(config.get(k)==v for k,v in SHARED.items()))


def roi1_manifest_matches(manifest):
    roi=manifest.get('roi_sampler',{})
    return (manifest.get('schema')=='posepriv.dpu_isolation_sd_candidate.v1' and
        manifest.get('purpose')==PURPOSE and manifest.get('dma_channels')==1 and
        manifest.get('control_base')=='0xa1000000' and manifest.get('dpu_spi')==104 and
        manifest.get('kernel_release')=='5.15.0-1027-xilinx-zynqmp' and
        pps4_roi_config_matches(manifest.get('shared_pose_dma')) and
        isinstance(roi,dict) and all(roi.get(k)==v for k,v in ROI1.items()))
