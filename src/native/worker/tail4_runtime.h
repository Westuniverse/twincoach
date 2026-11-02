/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef TAIL4_RUNTIME_H
#define TAIL4_RUNTIME_H
#include <stddef.h>
#include <stdint.h>
#include "posepriv_tail4_uapi.h"
typedef int (*tail4_runtime_submit_fn)(void*,struct posepriv_tail4_submit*);
struct tail4_runtime {
 int fd,owns_map;
 uint8_t *arena;
 size_t capacity;
 uint32_t weights_offset,post_offset,weights_bytes,post_bytes,head195,head117,head1;
 tail4_runtime_submit_fn submit;
 void *submit_context;
 struct posepriv_tail4_info info;
 struct posepriv_tail4_submit last_submission;
};
struct tail4_result {
 uint64_t source_frame_id,source_timestamp_ns;
 float landmarks[195],world[117],pose_presence;
};
int tail4_runtime_open(struct tail4_runtime*,const char*device,const char*W,const char*P);
int tail4_runtime_fake(struct tail4_runtime*,void*arena,size_t capacity,const char*W,const char*P,tail4_runtime_submit_fn,void*context);
/* Same control path, raw heads remain in arena at cached head offsets. */
int tail4_runtime_execute(struct tail4_runtime*,const int8_t*mp245,const int8_t*mp246,uint16_t job_tag);
/* Fixed two4608B NHWC S8Q4 DPU cuts; owner serializes a whole source transaction.
 * No upper-host NN/Add/Pool. Output is published only after all35 PL commands.
 */
int tail4_runtime_run(struct tail4_runtime*,const int8_t*mp245,const int8_t*mp246,
 uint64_t source_frame_id,uint64_t source_timestamp_ns,uint16_t job_tag,struct tail4_result*);
void tail4_runtime_close(struct tail4_runtime*);
#endif
