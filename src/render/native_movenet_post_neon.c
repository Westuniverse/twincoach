/* SPDX-License-Identifier: GPL-2.0-only */
/* Project-written AArch64 4-x-lane post. ABI matches native_movenet_post.c. */
#if !defined(__aarch64__) || !defined(__ARM_NEON)
#error "native_post_neon requires AArch64 ARMv8-A Advanced SIMD; no host fallback"
#else
#include <arm_neon.h>
#include <stdint.h>
#include <float.h>
#ifdef __FAST_MATH__
#error "native_post_neon requires fast-math disabled"
#endif
_Static_assert(sizeof(float)==4 && FLT_RADIX==2 && FLT_MANT_DIG==24 && FLT_EVAL_METHOD==0,
               "native_post_neon requires direct IEEE float32 arithmetic");

int native_post_abi(void) { return 1; }

int native_post(const int8_t *center,const int8_t *heat,const int8_t *reg,
                const int8_t *offset,const float *center_lut,const float *heat_lut,
                const float *prior,float regression_scale,float offset_scale,
                float normalize,float distance_bias,float *points) {
    if(!center || !heat || !reg || !offset || !center_lut || !heat_lut || !prior || !points) return 1;
    int center_index=0;
    float best=center_lut[(int)center[0]+128]*prior[0];
    for(int p=1;p<2304;++p) {
        float value=center_lut[(int)center[p]+128]*prior[p];
        if(value>best){best=value;center_index=p;}
    }
    int cy=center_index/48,cx=center_index%48;
    const float lane_offsets[4]={0.0f,1.0f,2.0f,3.0f};
    const float32x4_t lanes=vld1q_f32(lane_offsets);
    const float32x4_t bias=vdupq_n_f32(distance_bias);
    for(int j=0;j<17;++j) {
        float ry=(float)reg[center_index*34+2*j]*regression_scale;
        float rx=(float)reg[center_index*34+2*j+1]*regression_scale;
        float py=(float)cy+ry,px=(float)cx+rx;
        const float32x4_t predicted_x=vdupq_n_f32(px);
        int chosen=0;float peak=-1.0f;
        for(int y=0;y<48;++y) {
            float dy=(float)y-py;
            float yy=dy*dy;
            const float32x4_t yy4=vdupq_n_f32(yy);
            /* 48 is divisible by4: no padding lanes or reordered tail. */
            for(int x=0;x<48;x+=4) {
                int p=y*48+x;
                float32x4_t positions=vaddq_f32(vdupq_n_f32((float)x),lanes);
                float32x4_t dx=vsubq_f32(positions,predicted_x);
                float32x4_t xx=vmulq_f32(dx,dx);
                float32x4_t squared=vaddq_f32(yy4,xx);
                /* Hardware FSQRT/FDIV, never FRSQRTE/FRECPE approximations. */
                float32x4_t distance=vsqrtq_f32(squared);
                float32x4_t denominator=vaddq_f32(distance,bias);
                float probabilities[4]={
                    heat_lut[(int)heat[(p+0)*17+j]+128],
                    heat_lut[(int)heat[(p+1)*17+j]+128],
                    heat_lut[(int)heat[(p+2)*17+j]+128],
                    heat_lut[(int)heat[(p+3)*17+j]+128]};
                float32x4_t score=vdivq_f32(vld1q_f32(probabilities),denominator);
                float ordered[4];vst1q_f32(ordered,score);
                /* Preserve scalar row-major first-index ties across lanes/blocks. */
                for(int lane=0;lane<4;++lane) {
                    float value=ordered[lane];
                    if(value>peak){peak=value;chosen=p+lane;}
                }
            }
        }
        float oy=(float)offset[chosen*34+2*j]*offset_scale;
        float ox=(float)offset[chosen*34+2*j+1]*offset_scale;
        float y=(float)(chosen/48)+oy,x=(float)(chosen%48)+ox;
        points[j*3]=y*normalize;points[j*3+1]=x*normalize;
        points[j*3+2]=heat_lut[(int)heat[chosen*17+j]+128];
    }
    return 0;
}
#endif
