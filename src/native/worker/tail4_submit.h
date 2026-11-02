/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef TAIL4_SUBMIT_H
#define TAIL4_SUBMIT_H
#include <stdint.h>
#include "tail4_program.h"
/* Matches dispatcher AXI-Lite shadow register contract; no physical base,
 * MMIO probing, board access or service operation is embedded here.
 * submit must complete the accepted layer and final AXI B before returning0.
 */
struct tail4_layer_cmd {
 uint64_t a,w_or_b,p,dst;
 uint32_t cfg,q;
 uint16_t limit,tag;
};
typedef int (*tail4_submit_fn)(void*context,const struct tail4_layer_cmd*cmd);
static inline struct tail4_layer_cmd tail4_command(const struct tail4_op_desc*o,
 uint64_t arena,uint64_t weights,uint64_t post,uint16_t frame_tag) {
 struct tail4_layer_cmd c;
 const unsigned kind=o->engine==0?0u:(o->mode==0?1u:2u);
 c.a=arena+o->a_offset;c.dst=arena+o->dst_offset;
 c.w_or_b=o->engine==0?weights+o->w_offset:(o->b_bytes?arena+o->b_offset:0);
 c.p=o->engine==0?post+o->p_offset:0;
 c.cfg=kind|((o->engine==0?(uint32_t)o->mode:0u)<<4)|((uint32_t)o->dim4<<8)|((uint32_t)o->stride2<<9);
 c.q=(uint32_t)o->qa|((uint32_t)o->qb<<4)|((uint32_t)o->qout<<8)|((uint32_t)o->relu<<16);
 c.limit=o->relu_limit;c.tag=(uint16_t)(frame_tag+o->job_tag);
 return c;
}
static inline int tail4_execute35(uint64_t arena,uint64_t weights,uint64_t post,
 uint16_t frame_tag,tail4_submit_fn submit,void*context) {
 for(unsigned i=0;i<TAIL4_OP_COUNT;++i) {
  struct tail4_layer_cmd c=tail4_command(&tail4_ops[i],arena,weights,post,frame_tag);
  const int status=submit(context,&c);if(status)return status;
 }
 return 0;
}
/* AXIL: CFG14,Q18,limit1c,tag20,A30/34,WorB38/3c,P40/44,dst48/4c.
 * CTRL08 START1/CANCEL2/ACK4; STATUS0c busy0 done1 success2 error3
 * cancelled4 IRQ5, lasttag10. Driver chooses actual MMIO mapping/owner.
 */
#endif
