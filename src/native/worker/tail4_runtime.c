// SPDX-License-Identifier: GPL-2.0-only
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#include "tail4_runtime.h"
#include "tail4_operator_regs.h"
#include "tail4_boundary.h"
#include "tail4_submit.h"
static uint32_t page_up(uint32_t n) {return (n+4095u)&~4095u;}
static int read_blob(const char*name,void*buffer,size_t bytes) {
 FILE*f=fopen(name,"rb");if(!f)return -errno;
 int ret=0;if(fread(buffer,1,bytes,f)!=bytes||fgetc(f)!=EOF)ret=-EINVAL;
 fclose(f);return ret;
}
static int setup(struct tail4_runtime*r,const char*W,const char*P) {
 r->weights_bytes=0;r->post_bytes=0;r->head195=r->head117=r->head1=0;
 for(unsigned i=0;i<TAIL4_OP_COUNT;++i) {
  const struct tail4_op_desc*o=&tail4_ops[i];
  if(o->engine==0) {
   if(o->w_offset+o->w_bytes>r->weights_bytes)r->weights_bytes=o->w_offset+o->w_bytes;
   if(o->p_offset+o->p_bytes>r->post_bytes)r->post_bytes=o->p_offset+o->p_bytes;
  }
 }
 for(unsigned i=0;i<TAIL4_TENSOR_COUNT;++i) {
  const struct tail4_tensor_desc*t=&tail4_tensors[i];
  if(t->c==195&&t->q==5)r->head195=t->offset;
  if(t->c==117&&t->q==14)r->head117=t->offset;
  if(t->c==1&&t->q==8)r->head1=t->offset;
 }
 r->weights_offset=page_up(TAIL4_ARENA_BYTES);r->post_offset=page_up(r->weights_offset+r->weights_bytes);
 if(r->capacity<r->post_offset+r->post_bytes||!r->head195||!r->head117||!r->head1)return -EINVAL;
 int ret=read_blob(W,r->arena+r->weights_offset,r->weights_bytes);if(ret)return ret;
 return read_blob(P,r->arena+r->post_offset,r->post_bytes);
}
static int device_submit(void*context,struct posepriv_tail4_submit*s) {
 struct tail4_runtime*r=context;
 return ioctl(r->fd,POSEPRIV_TAIL4_SUBMIT,s)<0?-errno:0;
}
int tail4_runtime_open(struct tail4_runtime*r,const char*device,const char*W,const char*P) {
 memset(r,0,sizeof(*r));r->fd=-1;
 r->fd=open(device,O_RDWR|O_CLOEXEC);if(r->fd<0)return -errno;
 if(ioctl(r->fd,POSEPRIV_TAIL4_GET_INFO,&r->info)<0) {int e=-errno;tail4_runtime_close(r);return e;}
 if(r->info.driver_abi!=TAIL4_DRIVER_ABI||r->info.hardware_id!=TAIL4_OP_ID||r->info.hardware_version!=TAIL4_OP_ABI_VERSION||r->info.arena_bytes!=TAIL4_ARENA_SIZE) {tail4_runtime_close(r);return -EPROTO;}
 r->capacity=(size_t)r->info.arena_bytes;
 void*map=mmap(NULL,r->capacity,PROT_READ|PROT_WRITE,MAP_SHARED,r->fd,0);
 if(map==MAP_FAILED) {int e=-errno;tail4_runtime_close(r);return e;}
 r->arena=map;r->owns_map=1;r->submit=device_submit;r->submit_context=r;
 int ret=setup(r,W,P);if(ret)tail4_runtime_close(r);return ret;
}
int tail4_runtime_fake(struct tail4_runtime*r,void*arena,size_t capacity,const char*W,const char*P,tail4_runtime_submit_fn submit,void*context) {
 memset(r,0,sizeof(*r));r->fd=-1;r->arena=arena;r->capacity=capacity;r->submit=submit;r->submit_context=context;
 return setup(r,W,P);
}
struct dispatch_context {struct tail4_runtime*r;unsigned index;};
static int submit_one(void*opaque,const struct tail4_layer_cmd*c) {
 struct dispatch_context*d=opaque;struct tail4_runtime*r=d->r;const struct tail4_op_desc*o=&tail4_ops[d->index];
 struct posepriv_tail4_submit s={.a_offset=c->a,.wb_offset=c->w_or_b,.p_offset=c->p,.dst_offset=c->dst,
  .a_bytes=o->a_bytes,.wb_bytes=o->engine==0?o->w_bytes:o->b_bytes,.p_bytes=o->engine==0?o->p_bytes:0,.dst_bytes=o->dst_bytes,
  .config=c->cfg,.qparam=c->q,.relu_limit=c->limit,.job_tag=c->tag,.timeout_ms=TAIL4_DEFAULT_TIMEOUT_MS};
 int ret=r->submit(r->submit_context,&s);r->last_submission=s;
 if(!ret)++d->index;
 return ret;
}
int tail4_runtime_execute(struct tail4_runtime*r,const int8_t*mp245,const int8_t*mp246,uint16_t tag) {
 tail4_pack_initial_cuts(mp245,mp246,r->arena);
 __sync_synchronize();
 struct dispatch_context d={.r=r,.index=0};int ret=tail4_execute35(0,r->weights_offset,r->post_offset,tag,submit_one,&d);
 if(ret)return ret; /* no automatic retry or partial-result publication */
 __sync_synchronize();
 return 0;
}
int tail4_runtime_run(struct tail4_runtime*r,const int8_t*mp245,const int8_t*mp246,uint64_t id,uint64_t ns,uint16_t tag,struct tail4_result*out) {
 int ret=tail4_runtime_execute(r,mp245,mp246,tag);if(ret)return ret;
 const int16_t*image=(const int16_t*)(r->arena+r->head195);const int16_t*world=(const int16_t*)(r->arena+r->head117);const int16_t*presence=(const int16_t*)(r->arena+r->head1);
 struct tail4_result result;result.source_frame_id=id;result.source_timestamp_ns=ns;
 for(unsigned i=0;i<195;++i)result.landmarks[i]=ldexpf((float)image[i],-5);
 for(unsigned i=0;i<117;++i)result.world[i]=ldexpf((float)world[i],-14);
 const float logit=ldexpf((float)presence[0],-8);result.pose_presence=1.0f/(1.0f+expf(-logit));
 *out=result;return 0;
}
void tail4_runtime_close(struct tail4_runtime*r) {
 if(r->owns_map&&r->arena)munmap(r->arena,r->capacity);
 if(r->fd>=0)close(r->fd);
 r->fd=-1;r->arena=NULL;r->owns_map=0;
}
