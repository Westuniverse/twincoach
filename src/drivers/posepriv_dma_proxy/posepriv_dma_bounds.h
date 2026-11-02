/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef POSEPRIV_DMA_BOUNDS_H
#define POSEPRIV_DMA_BOUNDS_H

/* The current joint DMA is 128-bit, no-DRE, HP0 DDR_LOW (exclusive 2 GiB). */
static inline int pp_dma_region_valid(unsigned long long address,
				     unsigned long long bytes)
{
	const unsigned long long end = 0x80000000ULL;

	return bytes && bytes <= end && !(address & 15ULL) &&
		address <= end - bytes;
}

static inline int pp_dma_length_valid(unsigned int length, unsigned int capacity)
{
	return length && length <= capacity && !(length & 15U);
}

/* One simple-DMA packet must fit the IP length field. Do not let DMAengine
 * splitting create intermediate TLAST or a false whole-packet completion.
 * AXI DMA supports length fields of 8..26 bits; round down for no-DRE 128b.
 */
static inline unsigned int pp_dma_packet_limit(unsigned int bits,
					       unsigned int capacity)
{
	unsigned int limit;

	if (bits < 8 || bits > 26)
		return 0;
	limit = ((1U << bits) - 1U) & ~15U;
	return (limit < capacity ? limit : capacity) & ~15U;
}

#endif
