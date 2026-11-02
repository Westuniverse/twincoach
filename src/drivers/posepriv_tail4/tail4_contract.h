/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef TAIL4_CONTRACT_H
#define TAIL4_CONTRACT_H
#ifdef __KERNEL__
#include <linux/errno.h>
#else
#include <errno.h>
#endif
#include "posepriv_tail4_uapi.h"
#include "tail4_operator_regs.h"
static inline int tail4_span_ok(__u64 off,__u32 length,__u64 capacity) {
 return !(off&15u)&&length&&length<=capacity&&off<=capacity-length;
}
static inline int tail4_overlap(__u64 a,__u32 an,__u64 b,__u32 bn) {
 return an&&bn&&a<b+bn&&b<a+an;
}
/* The one cold interface boundary. Lengths derive from the fixed operator ABI;
 * no payload/perword/model scan is performed in the driver or hardware loop.
 */
static inline int tail4_validate_submit(const struct posepriv_tail4_submit*s,__u64 capacity) {
 __u32 kind=s->config&3u,mode=(s->config>>4)&7u,dim4=(s->config>>8)&1u,stride2=(s->config>>9)&1u;
 __u32 input=dim4?9216u:2304u,wb=0,p=0,output=0;
 if((s->config&~0x373u)||(s->qparam&~0x10fffu)||s->relu_limit>32767u||s->job_tag>65535u||
    s->reserved||!s->timeout_ms||s->timeout_ms>TAIL4_MAX_TIMEOUT_MS)return -EINVAL;
 if(kind==TAIL4_OP_KIND_CONV) {
  if(mode>4u||(mode>=2u&&dim4))return -EINVAL;
  if(mode==0u) {if(stride2)return -EINVAL;wb=165888u;p=4608u;output=input;}
  else if(mode==1u) {wb=23040u;p=4608u;output=stride2?input/4u:input;}
  else {if(stride2)return -EINVAL;wb=mode==2u?451584u:(mode==3u?276480u:9216u);p=mode==2u?3136u:(mode==3u?1920u:64u);output=mode==2u?390u:(mode==3u?234u:2u);}
 } else if(kind==TAIL4_OP_KIND_ADD) {
  if(mode||stride2)return -EINVAL;
  wb=input;output=input;
 } else if(kind==TAIL4_OP_KIND_POOL) {
  if(mode||!dim4||!stride2||(s->qparam&TAIL4_OP_RELU)||((s->qparam&15u)!=((s->qparam>>8)&15u)))return -EINVAL;
  output=2304u;
 } else return -EINVAL;
 if(s->a_bytes!=input||s->wb_bytes!=wb||s->p_bytes!=p||s->dst_bytes!=output)return -EINVAL;
 /* Destination's final AXI word is mapped even when its WSTRB masks head pad. */
 output=(output+15u)&~15u;
 if(!tail4_span_ok(s->a_offset,input,capacity)||!tail4_span_ok(s->dst_offset,output,capacity)||
   (wb&&!tail4_span_ok(s->wb_offset,wb,capacity))||(p&&!tail4_span_ok(s->p_offset,p,capacity)))return -ERANGE;
 if(tail4_overlap(s->dst_offset,output,s->a_offset,input)||tail4_overlap(s->dst_offset,output,s->wb_offset,wb)||tail4_overlap(s->dst_offset,output,s->p_offset,p))return -EINVAL;
 return 0;
}
#endif
