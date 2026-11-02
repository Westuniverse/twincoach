// SPDX-License-Identifier: GPL-2.0-only
// Frozen project implementation is retained byte-for-byte, including its
// exported posepriv_crop_bgr_q7 fused reference and original NEON sampler.
#include "crop_q7.c"

// One uint32 per destination pixel, X U16Q5 in bits15:0, Y U16Q5 in31:16.
// Python interface checks width/height in1..2048 once. Clamp AFTER the exact
// original nearest-even conversion: this folds BORDER_REPLICATE into the map.
static uint32_t packed_coord(int X,int Y,int width,int height){
    return (uint32_t)limit(X,(width-1)<<5) |
           ((uint32_t)limit(Y,(height-1)<<5)<<16);
}

void posepriv_crop_coords_q5(int width,int height,const double *inv,
                            uint32_t *map,int mode){
#if defined(POSEPRIV_CROP_COORD_PAIR) && CROP_NEON
    const float64x2_t zero=vdupq_n_f64(0.),thirtytwo=vdupq_n_f64(32.);
    const float64x2_t lo=vdupq_n_f64((double)INT_MIN),hi=vdupq_n_f64((double)INT_MAX);
    for(int y=0;y<256;++y){
        for(int block=0;block<256;block+=64){
            double bx=inv[0]*block+inv[1]*y+inv[2];
            double by=inv[3]*block+inv[4]*y+inv[5];
            double bw=inv[6]*block+inv[7]*y+inv[8];
            for(int t=0;t<64;t+=2){
                double offsets[2]={(double)t,(double)(t+1)};
                float64x2_t dt=vld1q_f64(offsets);
                float64x2_t den=mode?vdupq_n_f64(inv[8]):vaddq_f64(vdupq_n_f64(bw),vmulq_n_f64(dt,inv[6]));
                float64x2_t scale=vdivq_f64(thirtytwo,den);
                scale=vbslq_f64(vceqq_f64(den,zero),zero,scale);
                float64x2_t xd=vmulq_f64(vaddq_f64(vdupq_n_f64(bx),vmulq_n_f64(dt,inv[0])),scale);
                float64x2_t yd=vmulq_f64(vaddq_f64(vdupq_n_f64(by),vmulq_n_f64(dt,inv[3])),scale);
                int64x2_t X=vcvtnq_s64_f64(vmaxq_f64(lo,vminq_f64(hi,xd)));
                int64x2_t Y=vcvtnq_s64_f64(vmaxq_f64(lo,vminq_f64(hi,yd)));
                size_t offset=(size_t)y*256+block+t;
                map[offset]=packed_coord((int)vgetq_lane_s64(X,0),(int)vgetq_lane_s64(Y,0),width,height);
                map[offset+1]=packed_coord((int)vgetq_lane_s64(X,1),(int)vgetq_lane_s64(Y,1),width,height);
            }
        }
    }
#else
    for(int y=0;y<256;++y){
        for(int block=0;block<256;block+=64){
            double bx=inv[0]*block+inv[1]*y+inv[2];
            double by=inv[3]*block+inv[4]*y+inv[5];
            double bw=inv[6]*block+inv[7]*y+inv[8];
            for(int t=0;t<64;++t){
                double denominator=mode?inv[8]:bw+inv[6]*t;
                double scale=denominator?32.0/denominator:0.;
                int X=quantized_coord((bx+inv[0]*t)*scale);
                int Y=quantized_coord((by+inv[3]*t)*scale);
                map[(size_t)y*256+block+t]=packed_coord(X,Y,width,height);
            }
        }
    }
#endif
}

void posepriv_crop_sample_q5(const uint8_t *bgr,int width,int height,size_t stride,
                            const uint32_t *map,uint8_t *output,uint8_t *rgb_out){
    for(size_t i=0;i<256u*256u;++i){
        int X=(int)(map[i]&65535u),Y=(int)(map[i]>>16);
#if defined(POSEPRIV_CROP_COORD_PAIR) && CROP_NEON
        pixel_neon(bgr,width,height,stride,X,Y,output+i*3,rgb_out?rgb_out+i*3:NULL);
#else
        int x=X>>5,y=Y>>5,fx=X&31,fy=Y&31;
        int x1=limit(x+1,width-1),y1=limit(y+1,height-1);
        const uint8_t *a=bgr+(size_t)y*stride+(size_t)x*3;
        const uint8_t *b=bgr+(size_t)y*stride+(size_t)x1*3;
        const uint8_t *c=bgr+(size_t)y1*stride+(size_t)x*3;
        const uint8_t *d=bgr+(size_t)y1*stride+(size_t)x1*3;
#if CROP_NEON
        interpolate_neon(a,b,c,d,(unsigned)fx,(unsigned)fy,output+i*3,rgb_out?rgb_out+i*3:NULL);
#else
        unsigned wa=(unsigned)((32-fx)*(32-fy)),wb=(unsigned)(fx*(32-fy));
        unsigned wc=(unsigned)((32-fx)*fy),wd=(unsigned)(fx*fy);
        for(int channel=0;channel<3;++channel){
            int source=2-channel;
            unsigned u8=(a[source]*wa+b[source]*wb+c[source]*wc+d[source]*wd+512u)>>10;
            output[i*3+channel]=q7(u8);
            if(rgb_out)rgb_out[i*3+channel]=(uint8_t)u8;
        }
#endif
#endif
    }
}

void posepriv_crop_split_q5(const uint8_t *bgr,int width,int height,size_t stride,
                           const double *inv,uint32_t *map,uint8_t *output,
                           uint8_t *rgb_out,int mode){
    posepriv_crop_coords_q5(width,height,inv,map,mode);
    posepriv_crop_sample_q5(bgr,width,height,stride,map,output,rgb_out);
}

const char *posepriv_crop_build_variant(void){
#if defined(POSEPRIV_CROP_COORD_PAIR) && CROP_NEON
    return "aarch64-cortex-a53-original-NEON-COORD_PAIR-FAST_NEIGHBORS";
#else
    return "host-scalar-original-formula";
#endif
}
