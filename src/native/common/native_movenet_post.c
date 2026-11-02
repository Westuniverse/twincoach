/* SPDX-License-Identifier: GPL-2.0-only */
/* Project-written fixed float32 raw-head post; no model/constant/LUT data. */
#include <math.h>
#include <stdint.h>
#include <float.h>
#ifdef __FAST_MATH__
#error "native_post requires fast-math disabled"
#endif
_Static_assert(sizeof(float)==4 && FLT_RADIX==2 && FLT_MANT_DIG==24 && FLT_EVAL_METHOD==0,
               "native_post requires direct IEEE float32 arithmetic");

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
    for(int j=0;j<17;++j) {
        float ry=(float)reg[center_index*34+2*j]*regression_scale;
        float rx=(float)reg[center_index*34+2*j+1]*regression_scale;
        float py=(float)cy+ry,px=(float)cx+rx;
        int chosen=0;float peak=-1.0f;
        for(int y=0;y<48;++y)for(int x=0;x<48;++x) {
            int p=y*48+x;
            float dy=(float)y-py,dx=(float)x-px;
            float yy=dy*dy,xx=dx*dx;
            float distance=sqrtf(yy+xx);
            float denominator=distance+distance_bias;
            float value=heat_lut[(int)heat[p*17+j]+128]/denominator;
            if(value>peak){peak=value;chosen=p;}
        }
        float oy=(float)offset[chosen*34+2*j]*offset_scale;
        float ox=(float)offset[chosen*34+2*j+1]*offset_scale;
        float y=(float)(chosen/48)+oy,x=(float)(chosen%48)+ox;
        points[j*3]=y*normalize;points[j*3+1]=x*normalize;
        points[j*3+2]=heat_lut[(int)heat[chosen*17+j]+128];
    }
    return 0;
}
