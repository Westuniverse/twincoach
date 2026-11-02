/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef POSEPRIV_SHARED_DMA_CONTRACT_H
#define POSEPRIV_SHARED_DMA_CONTRACT_H
#include "posepriv_shared_dma_uapi.h"
#include "posepriv_raster_contract.h"
#include "posepriv_skinning_contract.h"

/* Byte checks are independent of CPU alignment/endian. Payload is 16-byte ABI. */
static inline int posepriv_shared_payload_valid(const unsigned char *payload,
					       unsigned int op, unsigned int bytes)
{
	unsigned int index, view;
	if (!posepriv_shared_output_bytes(op, bytes))
		return 0;
	if (op == 5)
		return posepriv_skinning_payload_valid(payload, bytes);
	if (op == 4)
		return posepriv_raster_payload_valid(payload, bytes);
	if (op == 3)
		return 1; /* whole signed24 XYZ/RGB8/id fields, no reserved bits */
	view = payload[13] & 1;
	for (index = 0; index < bytes; index += 16) {
		if (payload[index + 15])
			return 0;
		if (op == 2)
			continue; /* rectified bits 127:120 only */
		if ((payload[index + 13] & 0xfe) || payload[index + 14])
			return 0;
		if (op == 1 && view != (payload[index + 13] & 1))
			return 0;
	}
	return 1;
}

/* Structural result checks only; never recalculate normal or sqrt on PS. */
static inline int posepriv_shared_lighting_output_valid(const unsigned char *tx,
						        const unsigned char *rx,
							unsigned int input_bytes)
{
	unsigned int face, lane, light;
	__u64 length;
	if (!posepriv_shared_output_bytes(3, input_bytes))
		return 0;
	for (face = 0; face < input_bytes / 32; face++) {
		const unsigned char *in = tx + face * 32, *out = rx + face * 16;
		if (out[14] || out[15] || (out[13] & 0xc0) || (out[7] & 2) ||
		    out[5] != in[30] || out[6] != in[31])
			return 0;
		light = out[3] | ((unsigned int)out[4] << 8);
		length = 0;
		for (lane = 0; lane < 7; lane++)
			length |= (__u64)out[7 + lane] << (8 * lane);
		length >>= 2;
		if (out[7] & 1) {
			if (length || light != 11469)
				return 0;
		} else if (length < 17 || length >= ((__u64)1 << 50) || light < 9175 || light > 32768)
			return 0;
	}
	return 1;
}
#endif
