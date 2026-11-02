/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef POSEPRIV_DMA_BINDING_H
#define POSEPRIV_DMA_BINDING_H

#ifdef __KERNEL__
#include <linux/string.h>
#else
#include <string.h>
#endif

/* Preserve the legacy exact-node selection. Each additional known proxy
 * is opt-in at module load, not a wildcard for other old DMA clients.
 */
static inline int pp_dma_node_allowed(const char *actual, const char *selected,
				      int enable_motion, int enable_rectified)
{
	return actual && selected &&
		(!strcmp(actual, selected) ||
		 (enable_motion && !strcmp(actual, "posepriv_unified_motion_proxy")) ||
		 (enable_rectified && !strcmp(actual, "posepriv_unified_rectified_proxy")));
}

#endif
