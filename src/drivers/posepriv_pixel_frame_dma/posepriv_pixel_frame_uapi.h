/* SPDX-License-Identifier: GPL-2.0-only WITH Linux-syscall-note */
#ifndef POSEPRIV_PIXEL_FRAME_UAPI_H
#define POSEPRIV_PIXEL_FRAME_UAPI_H
#include <linux/types.h>
#include <linux/ioctl.h>

/* v1: arm64 little endian, USER pointers (never DMA addresses). */
struct posepriv_pixel_frame_xfer {
	__u64 tx_user, rx_user;
	__u32 input_bytes, output_bytes, timeout_ms, reserved;
};
/* Cached snapshot, not new MMIO on GET_STATUS. Mask bits: RX=1, TX=2.
 * elapsed_ns includes descriptor preparation + waits, NOT pure PL cycles.
 * protocol_error is latched from the dedicated IRQ / GIC pending+active state;
 * the existing RTL has no readable protocol_error register.
 */
struct posepriv_pixel_frame_status {
	__u64 tx_dma, rx_dma, elapsed_ns;
	__u32 input_bytes, output_bytes, mm2s_status, s2mm_status;
	__u32 protocol_error, faulted, started, completed;
	__s32 last_errno;
	__u32 reserved;
};
#define POSEPRIV_PIXEL_FRAME_XFER _IOW('P', 0x41, struct posepriv_pixel_frame_xfer)
#define POSEPRIV_PIXEL_FRAME_STATUS _IOR('P', 0x42, struct posepriv_pixel_frame_status)
#define POSEPRIV_PIXEL_INPUT_BYTES (640U * 360U * 16U)
#define POSEPRIV_PIXEL_OUTPUT_BYTES (1280U * 720U * 3U)
#endif
