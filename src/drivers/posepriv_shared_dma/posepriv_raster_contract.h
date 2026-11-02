/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef POSEPRIV_RASTER_CONTRACT_H
#define POSEPRIV_RASTER_CONTRACT_H
#include "posepriv_shared_dma_uapi.h"

static inline __u64 pp_raster_le(const unsigned char *p, unsigned int n)
{
    __u64 value = 0;
    unsigned int i;
    for (i = 0; i < n; i++) value |= (__u64)p[i] << (i * 8);
    return value;
}
static inline __s64 pp_raster_signed(const unsigned char *p, unsigned int n)
{
    __u64 value = pp_raster_le(p, n);
    return value & ((__u64)1 << (n * 8 - 1)) ? (__s64)value - ((__s64)1 << (n * 8)) : (__s64)value;
}
static inline int pp_raster_range(const unsigned char *p, unsigned int n, __s64 limit)
{
    __s64 value = pp_raster_signed(p, n);
    return value >= -limit && value < limit;
}
static inline int posepriv_raster_payload_valid(const unsigned char *p, unsigned int bytes)
{
    unsigned int n, x, y, i, j;
    __u64 header;
    if (!posepriv_shared_output_bytes(4, bytes) || pp_raster_le(p, 4) != 0x31545850U || p[5] != 1)
        return 0;
    header = pp_raster_le(p + 8, 8);
    n = (unsigned int)pp_raster_le(p + 6, 2);
    if (header >> 48) return 0;
    if (p[4] == 1) {
        if (bytes != POSEPRIV_RASTER_TEXTURE_BYTES || n || header) return 0;
        for (i = 19; i < bytes; i += 4) if (p[i]) return 0;
        return 1;
    }
    if (p[4] != 2 || bytes != 16U + 80U * n) return 0;
    x = header & 4095U; y = (header >> 12) & 4095U;
    if (x > 4064 || y > 4064) return 0;
    for (i = 0; i < n; i++) {
        const unsigned char *d = p + 16 + i * 80;
        __u64 bbox = pp_raster_le(d, 6);
        unsigned int x0 = bbox & 4095U, x1 = (bbox >> 12) & 4095U;
        unsigned int y0 = (bbox >> 24) & 4095U, y1 = (bbox >> 36) & 4095U;
        if ((x1 >= x0 && x1 - x0 > 31) || (y1 >= y0 && y1 - y0 > 31) ||
            (d[69] & 0xfc) || (d[68] | ((d[69] & 1U) << 8)) > 256) return 0;
        if (!(d[69] & 2) && x1 >= x0 && y1 >= y0 &&
            (x0 < x || x1 >= x + 32 || y0 < y || y1 >= y + 32)) return 0;
        for (j = 70; j < 80; j++) if (d[j]) return 0;
        for (j = 0; j < 3; j++) {
            if (!pp_raster_range(d + 6 + j * 4, 4, (__s64)1 << 30) ||
                !pp_raster_range(d + 18 + j * 4, 4, (__s64)1 << 20) ||
                !pp_raster_range(d + 30 + j * 4, 4, (__s64)1 << 20)) return 0;
        }
        if (!pp_raster_range(d + 42, 5, (__s64)1 << 36) ||
            !pp_raster_range(d + 47, 5, (__s64)1 << 36)) return 0;
        for (j = 0; j < 4; j++) if (!pp_raster_range(d + 52 + j * 4, 4, (__s64)1 << 28)) return 0;
    }
    return 1;
}
static inline int posepriv_raster_output_valid(const unsigned char *tx, const unsigned char *rx,
                                               unsigned int input_bytes)
{
    unsigned int output = posepriv_shared_output_bytes(4, input_bytes), i;
    if (!output || pp_raster_le(rx, 4) != 0x31525850U || rx[4] || rx[5] != 1 ||
        rx[6] != tx[4] || rx[7] || pp_raster_le(rx + 8, 8) != (output - 16) / 16) return 0;
    for (i = 19; i < output; i += 4) if (rx[i]) return 0;
    return 1;
}
#endif
