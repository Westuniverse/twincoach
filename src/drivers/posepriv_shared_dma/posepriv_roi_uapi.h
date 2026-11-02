/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef POSEPRIV_ROI_UAPI_H
#define POSEPRIV_ROI_UAPI_H
#include <linux/types.h>
#include <linux/ioctl.h>
#include "roi1_regs.h"

/* Source BGR and packed Q5 map are copied into a separate coherent allocation.
 * Userspace never supplies a DMA address. Q7 output is 256x256x3 interleaved RGB (NHWC) bytes.
 * On accepted transactions status/stream_bytes are copied back even on error.
 */
struct posepriv_roi_xfer {
	__u64 source_user, map_user, rx_user, ticket;
	__u32 width, height, stride, source_bytes;
	__u32 timeout_ms, status, stream_bytes, reserved;
};
#define POSEPRIV_ROI_XFER _IOWR('R', 0x70, struct posepriv_roi_xfer)
/* Private build-only staging measurement; no crop, neural or PL work. */
#define POSEPRIV_ROI_STAGE_BENCH _IOWR('R', 0x71, struct posepriv_roi_xfer)
#define POSEPRIV_ROI_CAPACITY 1048576U
#define POSEPRIV_ROI_SOURCE_LIMIT (POSEPRIV_ROI_CAPACITY - ROI1_MAP_BYTES - 16U)

static inline int posepriv_roi_shape(const struct posepriv_roi_xfer *x)
{
	__u64 extent = (__u64)x->height * x->stride;
	return !x->reserved && x->source_user && x->map_user && x->rx_user &&
		x->width && x->width <= 2048 && x->height && x->height <= 2048 &&
		x->stride >= x->width * 3U && extent == x->source_bytes &&
		extent <= POSEPRIV_ROI_SOURCE_LIMIT &&
		x->timeout_ms && x->timeout_ms <= 2000;
}
#endif
