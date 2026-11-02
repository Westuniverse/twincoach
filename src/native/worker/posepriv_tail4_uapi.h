/* SPDX-License-Identifier: GPL-2.0-only WITH Linux-syscall-note */
#ifndef POSEPRIV_TAIL4_UAPI_H
#define POSEPRIV_TAIL4_UAPI_H
#include <linux/types.h>
#include <linux/ioctl.h>
#define TAIL4_DRIVER_ABI 0x00010000u
#define TAIL4_ARENA_SIZE (4u*1024u*1024u)
#define TAIL4_DEFAULT_TIMEOUT_MS 50u
#define TAIL4_MAX_TIMEOUT_MS 500u
#define TAIL4_CANCEL_TIMEOUT_MS 100u
#define TAIL4_INFO_IRQ 1u
#define TAIL4_INFO_FAULTED 2u
#define TAIL4_INFO_ACTIVE 4u
#define TAIL4_OUT_ACCEPTED 1u
#define TAIL4_OUT_QUIESCED 2u
#define TAIL4_OUT_QUARANTINED 4u
struct posepriv_tail4_info {
 __u32 driver_abi,hardware_id,hardware_version,flags;
 __u64 dma_base,arena_bytes;
 __u32 default_timeout_ms,max_timeout_ms,cancel_timeout_ms,reserved;
};
/* Offsets refer only to this device's coherent arena, never arbitrary DDR.
 * wb is W for Conv or B for Add. Unused wb/p spans have zero length.
 * Blocking SUBMIT: one cmd owns all DMA until final completion or cancel/drain.
 * outcome is copied back even on a post-START error; accepted work must not be
 * retried as though a failing ioctl meant no hardware transaction occurred.
 */
struct posepriv_tail4_submit {
 __u64 a_offset,wb_offset,p_offset,dst_offset;
 __u32 a_bytes,wb_bytes,p_bytes,dst_bytes;
 __u32 config,qparam,relu_limit,job_tag,timeout_ms,reserved;
 __u32 status,last_tag,outcome,reserved_out;
};
struct posepriv_tail4_cancel {__u32 status,last_tag,outcome,reserved;};
#define POSEPRIV_TAIL4_GET_INFO _IOR('T',0x61,struct posepriv_tail4_info)
#define POSEPRIV_TAIL4_SUBMIT _IOWR('T',0x62,struct posepriv_tail4_submit)
#define POSEPRIV_TAIL4_CANCEL _IOR('T',0x63,struct posepriv_tail4_cancel)
#endif
