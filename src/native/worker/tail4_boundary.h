/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef TAIL4_BOUNDARY_H
#define TAIL4_BOUNDARY_H
#include <stdint.h>
#include "tail4_program.h"
/* On-board ARM only: native NHWC S8Q4 -> DDR A16 CHWc4, real values unchanged.
 * Two inputs total9216B; destination slot0Q10/slot1Q11 total18432B.
 * Multiplication implements exact signed left shift without C negative-shift UB.
 */
static inline void tail4_pack_cut_c4(const int8_t*src,int16_t*dst,unsigned shift) {
 for(unsigned cg=0;cg<72;++cg)for(unsigned pixel=0;pixel<16;++pixel)
  for(unsigned lane=0;lane<4;++lane)
   dst[(cg*16+pixel)*4+lane]=(int16_t)((int32_t)src[pixel*288+cg*4+lane]*(int32_t)(1u<<shift));
}
static inline void tail4_pack_initial_cuts(const int8_t*mp245,const int8_t*mp246,void*arena) {
 tail4_pack_cut_c4(mp245,(int16_t*)arena,6);
 tail4_pack_cut_c4(mp246,(int16_t*)((uint8_t*)arena+TAIL4_SLOT_BYTES),7);
}
#endif
