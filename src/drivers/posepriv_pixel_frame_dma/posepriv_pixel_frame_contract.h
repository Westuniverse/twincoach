/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef POSEPRIV_PIXEL_FRAME_CONTRACT_H
#define POSEPRIV_PIXEL_FRAME_CONTRACT_H
#ifdef __KERNEL__
#include <linux/errno.h>
#include <linux/string.h>
#else
#include <errno.h>
#include <string.h>
#endif
#include "posepriv_pixel_frame_uapi.h"

/* AXI DMA simple-mode DMASR: DMA/SG error bits + Error IRQ (IOC may be
 * cleared by DMAengine's IRQ handler, so it is never required here).
 */
#define POSEPRIV_PIXEL_DMASR_ERROR_MASK 0x4770U
static inline int pixel_frame_dma_status_error(unsigned int tx, unsigned int rx)
{
	return (tx | rx) & POSEPRIV_PIXEL_DMASR_ERROR_MASK;
}

static inline int pixel_frame_header_valid(const struct posepriv_pixel_frame_xfer *x)
{
	return x->tx_user && x->rx_user && !x->reserved &&
		x->input_bytes == POSEPRIV_PIXEL_INPUT_BYTES &&
		x->output_bytes == POSEPRIV_PIXEL_OUTPUT_BYTES &&
		x->timeout_ms && x->timeout_ms <= 2000;
}
static inline int pixel_frame_payload_valid(const unsigned char *data, unsigned int bytes)
{
	unsigned int i;
	if (!data || bytes != POSEPRIV_PIXEL_INPUT_BYTES)
		return 0;
	/* Linux GCC: prove alignment before the assumption, never type-pun bytes.
	 * All four padding bytes belong to this pixel; zero is endian-independent.
	 * Known-aligned memcpy permits a word load without aliasing violations;
	 * inspect the actual target build rather than assuming compiler flags.
	 */
	if (!((unsigned long)data & 3UL)) {
		data = __builtin_assume_aligned(data, 4);
		for (i = 12; i < bytes; i += 16) {
			__u32 padding;
			memcpy(&padding, data + i, sizeof(padding));
			if (padding)
				return 0;
		}
		return 1;
	}
	/* Preserve the original behavior for unaligned CPU reference callers. */
	for (i = 12; i < bytes; i += 16)
		if (data[i] || data[i + 1] || data[i + 2] || data[i + 3])
			return 0;
	return 1;
}

/* The actual kernel transaction uses this small injectable sequence, so the
 * deterministic C test executes ordering + every failure edge, not a rewrite.
 * Caller holds the device mutex and owns the one shared absolute deadline.
 */
struct pixel_frame_ops {
	int (*check_error)(void *);
	int (*start)(void *, unsigned int); /* 0=TX, 1=RX */
	int (*wait)(void *, unsigned int);
	void (*stop)(void *);
};
static inline int pixel_frame_run(void *ctx, const struct pixel_frame_ops *ops,
				  struct posepriv_pixel_frame_status *status)
{
	int ret;
	if (status->faulted)
		return -EIO;
	status->started = status->completed = 0;
	ret = ops->check_error(ctx);
	if (ret) goto fault;
	ret = ops->start(ctx, 1); /* S2MM FIRST, one whole frame */
	if (ret) goto fault;
	status->started |= 1;
	ret = ops->start(ctx, 0);
	if (ret) goto fault;
	status->started |= 2;
	ret = ops->wait(ctx, 0);
	if (ret) goto fault;
	status->completed |= 2;
	ret = ops->wait(ctx, 1);
	if (ret) goto fault;
	status->completed |= 1;
	ret = ops->check_error(ctx);
	if (ret) goto fault;
	status->last_errno = 0;
	return 0;
fault:
	status->last_errno = ret;
	status->faulted = 1;
	ops->stop(ctx);
	return ret;
}
#endif
