/* SPDX-License-Identifier: GPL-2.0-only WITH Linux-syscall-note */
#ifndef POSEPRIV_SHARED_DMA_UAPI_H
#define POSEPRIV_SHARED_DMA_UAPI_H
#include <linux/types.h>
#include <linux/ioctl.h>

/* arm64 little-endian userspace. Pointers are USER addresses, never DMA/DDR. */
struct posepriv_shared_xfer {
	__u64 tx_user;
	__u64 rx_user;
	__u32 operator_id;
	__u32 input_bytes;
	__u32 timeout_ms;
	__u32 reserved;
};
#define POSEPRIV_SHARED_XFER _IOW('P', 0x31, struct posepriv_shared_xfer)
/* Private explicit op5 whole-frame candidate; old command/limits unchanged. */
#define POSEPRIV_SKINNING_WHOLE_XFER _IOW('P', 0x32, struct posepriv_shared_xfer)
#define POSEPRIV_SKINNING_WHOLE_CAPACITY (256U * 1024U)
#define POSEPRIV_SKINNING_STATIC_UPLOAD _IOW('P', 0x33, struct posepriv_shared_xfer)
#define POSEPRIV_SKINNING_STATIC_RUN _IOW('P', 0x34, struct posepriv_shared_xfer)
#define POSEPRIV_SHARED_CAPACITY (128U * 1024U)
#define POSEPRIV_RASTER_CAPACITY ((1U << 20) - 16U)
#define POSEPRIV_RASTER_TEXTURE_BYTES 524304U
#define POSEPRIV_SHARED_ID_V3 0x50505333U
#define POSEPRIV_SHARED_ID_V4 0x50505334U
#define POSEPRIV_SKINNING_HEADER_BYTES 512U
#define POSEPRIV_SHARED_ID_V1 0x50505331U
#define POSEPRIV_SHARED_ID 0x50505332U

static inline int posepriv_shared_id_valid(__u32 id)
{
	return id == POSEPRIV_SHARED_ID_V1 || id == POSEPRIV_SHARED_ID ||
		id == POSEPRIV_SHARED_ID_V3 || id == POSEPRIV_SHARED_ID_V4;
}
static inline int posepriv_shared_operator_supported(__u32 id, __u32 op)
{
	return posepriv_shared_id_valid(id) && (op <= 2 || (id != POSEPRIV_SHARED_ID_V1 && op == 3) ||
		((id == POSEPRIV_SHARED_ID_V3 || id == POSEPRIV_SHARED_ID_V4) && op == 4) ||
		(id == POSEPRIV_SHARED_ID_V4 && op == 5));
}

static inline int posepriv_shared_capabilities_valid(__u32 id, __u32 capabilities)
{
	if (id == POSEPRIV_SHARED_ID_V4) return capabilities == 0x3fU;
	if (id == POSEPRIV_SHARED_ID_V3) return capabilities == 0x1fU;
	return id == POSEPRIV_SHARED_ID_V1 || id == POSEPRIV_SHARED_ID;
}

static inline __u32 posepriv_shared_tx_capacity(__u32 id)
{
	return id == POSEPRIV_SHARED_ID_V3 || id == POSEPRIV_SHARED_ID_V4 ?
		POSEPRIV_RASTER_CAPACITY : POSEPRIV_SHARED_CAPACITY;
}

static inline int posepriv_shared_configured_idle(__u32 id, __u32 status)
{
    return (id == POSEPRIV_SHARED_ID_V3 || id == POSEPRIV_SHARED_ID_V4 ? (status & ~4U) : status) == 2U;
}

static inline __u32 posepriv_shared_output_bytes(__u32 op, __u32 input)
{
	if (op == 5)
		return input >= POSEPRIV_SKINNING_HEADER_BYTES + 16U &&
			input <= POSEPRIV_SKINNING_HEADER_BYTES + POSEPRIV_SHARED_CAPACITY &&
			!(input & 15U) ? input - POSEPRIV_SKINNING_HEADER_BYTES : 0;
	if (op == 4) {
		if (input == POSEPRIV_RASTER_TEXTURE_BYTES) return 16U;
		return input >= 16U && input <= POSEPRIV_RASTER_CAPACITY && !((input - 16U) % 80U) ? 4112U : 0;
	}
	if (op > 3 || !input || input > POSEPRIV_SHARED_CAPACITY || (input & 15U))
		return 0;
	if (op == 3)
		return (input & 31U) ? 0 : input / 2;
	return op == 1 ? 16U : input;
}
/* Independent admission: never broaden the old posepriv_shared_output_bytes. */
static inline __u32 posepriv_skinning_whole_output_bytes(__u32 op, __u32 input)
{
	return op == 5 && input >= POSEPRIV_SKINNING_HEADER_BYTES + 16U &&
		input <= POSEPRIV_SKINNING_HEADER_BYTES + POSEPRIV_SKINNING_WHOLE_CAPACITY &&
		!(input & 15U) ? input - POSEPRIV_SKINNING_HEADER_BYTES : 0;
}
static inline int posepriv_skinning_static_vertices_bytes_valid(__u32 bytes)
{
	return bytes && bytes <= POSEPRIV_SKINNING_WHOLE_CAPACITY && !(bytes & 15U);
}
static inline int posepriv_skinning_static_upload_shape(const struct posepriv_shared_xfer *x)
{
	return x->operator_id == 5 && x->tx_user && !x->rx_user &&
		posepriv_skinning_static_vertices_bytes_valid(x->input_bytes) &&
		!x->timeout_ms && !x->reserved;
}
static inline int posepriv_skinning_static_run_shape(const struct posepriv_shared_xfer *x)
{
	return x->operator_id == 5 && x->tx_user && x->rx_user &&
		x->input_bytes == POSEPRIV_SKINNING_HEADER_BYTES &&
		x->timeout_ms && x->timeout_ms <= 2000 && !x->reserved;
}
#endif
