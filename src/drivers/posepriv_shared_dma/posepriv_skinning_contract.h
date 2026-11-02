/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef POSEPRIV_SKINNING_CONTRACT_H
#define POSEPRIV_SKINNING_CONTRACT_H
#include "posepriv_shared_dma_uapi.h"

static inline int posepriv_skinning_header_valid(const unsigned char *tx)
{
	unsigned int offset, lane;
	for (offset = 0; offset < POSEPRIV_SKINNING_HEADER_BYTES; offset += 32)
		for (lane = 24; lane < 32; lane++)
			if (tx[offset + lane]) return 0;
	return 1;
}
static inline int posepriv_skinning_vertices_valid(const unsigned char *tx, unsigned int bytes)
{
	unsigned int offset, lane, sum;
	if (!posepriv_skinning_static_vertices_bytes_valid(bytes)) return 0;
	for (offset = 0; offset < bytes; offset += 16) {
		sum = 0;
		for (lane = 8; lane < 16; lane += 2)
			sum += tx[offset + lane] | ((unsigned int)tx[offset + lane + 1] << 8);
		if (sum != 32768U) return 0;
	}
	return 1;
}

static inline int posepriv_skinning_payload_valid(const unsigned char *tx, unsigned int bytes)
{
	return posepriv_shared_output_bytes(5, bytes) && posepriv_skinning_header_valid(tx) &&
		posepriv_skinning_vertices_valid(tx + POSEPRIV_SKINNING_HEADER_BYTES,
			bytes - POSEPRIV_SKINNING_HEADER_BYTES);
}

static inline int posepriv_skinning_output_valid(const unsigned char *rx, unsigned int input_bytes)
{
	unsigned int offset, lane, output = posepriv_shared_output_bytes(5, input_bytes);
	if (!output) return 0;
	for (offset = 0; offset < output; offset += 16)
		for (lane = 6; lane < 16; lane++)
			if (rx[offset + lane]) return 0;
	return 1;
}
/* Whole-frame keeps the same matrix, weight and output byte semantics. */
static inline int posepriv_skinning_whole_payload_valid(const unsigned char *tx, unsigned int bytes)
{
	return posepriv_skinning_whole_output_bytes(5, bytes) && posepriv_skinning_header_valid(tx) &&
		posepriv_skinning_vertices_valid(tx + POSEPRIV_SKINNING_HEADER_BYTES,
			bytes - POSEPRIV_SKINNING_HEADER_BYTES);
}

static inline int posepriv_skinning_whole_output_valid(const unsigned char *rx, unsigned int input_bytes)
{
	unsigned int offset, lane, output = posepriv_skinning_whole_output_bytes(5, input_bytes);
	if (!output) return 0;
	for (offset = 0; offset < output; offset += 16)
		for (lane = 6; lane < 16; lane++)
			if (rx[offset + lane]) return 0;
	return 1;
}
#endif
