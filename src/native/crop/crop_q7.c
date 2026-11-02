// SPDX-License-Identifier: GPL-2.0-only
// Original project fused BGR -> replicated bilinear RGB/U8 -> exact Q7 sampler.
// Coordinate/rounding reference: OpenCV 4.11.0 imgwarp.cpp (BSD-3-Clause).
#include <stdint.h>
#include <stddef.h>
#include <math.h>
#include <limits.h>
#if defined(POSEPRIV_CROP_NEON) && defined(__aarch64__)
#include <arm_neon.h>
#define CROP_NEON 1
#endif

static int limit(int x,int high){return x<0?0:x>high?high:x;}
#if !defined(POSEPRIV_CROP_COORD_PAIR) || !CROP_NEON
static int quantized_coord(double value){
    if(value<INT_MIN)return INT_MIN;
    if(value>INT_MAX)return INT_MAX;
    return (int)lrint(value);
}
#endif
#if !CROP_NEON
static uint8_t q7(unsigned value){
    unsigned n=(value*128u+127u)/255u;
    return (uint8_t)(n>127u?127u:n);
}
#endif

#if CROP_NEON
static uint16x4_t channels(const uint8_t *p){
    uint32_t word=(uint32_t)p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16);
    return vget_low_u16(vmovl_u8(vcreate_u8(word)));
}
#if !defined(POSEPRIV_CROP_FAST_NEIGHBORS) || !defined(POSEPRIV_CROP_COORD_PAIR)
static void interpolate_neon(const uint8_t *a,const uint8_t *b,const uint8_t *c,const uint8_t *d,
                             unsigned fx,unsigned fy,uint8_t *out,uint8_t *rgb){
    uint16x4_t top=vmla_n_u16(vmul_n_u16(channels(a),32-fx),channels(b),fx);
    uint16x4_t bottom=vmla_n_u16(vmul_n_u16(channels(c),32-fx),channels(d),fx);
    uint32x4_t sum=vmlal_n_u16(vmull_n_u16(top,32-fy),bottom,fy);
    uint16x4_t sampled=vrshrn_n_u32(sum,10);
    if(rgb){rgb[0]=(uint8_t)vget_lane_u16(sampled,2);rgb[1]=(uint8_t)vget_lane_u16(sampled,1);rgb[2]=(uint8_t)vget_lane_u16(sampled,0);}
    // For all U8 values the original floor(v*128/255+.5) is ceil(v/2),
    // with only255 clamped from128 to127. Avoid three magic divisions.
    uint16x4_t q=vmin_u16(vshr_n_u16(vadd_u16(sampled,vdup_n_u16(1)),1),vdup_n_u16(127));
    out[0]=(uint8_t)vget_lane_u16(q,2);out[1]=(uint8_t)vget_lane_u16(q,1);out[2]=(uint8_t)vget_lane_u16(q,0);
}
#endif

#if defined(POSEPRIV_CROP_COORD_PAIR)
static void pixel_neon(const uint8_t *bgr,int width,int height,size_t stride,
                       int X,int Y,uint8_t *out,uint8_t *rgb){
    int x=X>>5,y=Y>>5,fx=X&31,fy=Y&31;
    int x0=limit(x,width-1),x1=limit(x+1,width-1);
    int y0=limit(y,height-1),y1=limit(y+1,height-1);
#if defined(POSEPRIV_CROP_FAST_NEIGHBORS)
    const uint8_t *a=bgr+(size_t)y0*stride+(size_t)x0*3;
    const uint8_t *c=bgr+(size_t)y1*stride+(size_t)x0*3;
    uint16x4_t sampled;
    if(x0==x1){
        if(y0==y1)sampled=channels(a);
        else sampled=vrshr_n_u16(vmla_n_u16(vmul_n_u16(channels(a),32-fy),channels(c),fy),5);
    }else{
        uint16x4_t top,bottom;
        if(x0+2<width){
            // Interior adjacent BGR neighbors fit in one safe8-byte load/row.
            // The unused fourth channel may hold the next B; it is not stored.
            uint8x8_t row=vld1_u8(a);
            uint16x4_t left=vget_low_u16(vmovl_u8(row));
            uint16x4_t right=vget_low_u16(vmovl_u8(vext_u8(row,row,3)));
            top=vmla_n_u16(vmul_n_u16(left,32-fx),right,fx);
            if(y0==y1)bottom=top;
            else{
                row=vld1_u8(c);left=vget_low_u16(vmovl_u8(row));
                right=vget_low_u16(vmovl_u8(vext_u8(row,row,3)));
                bottom=vmla_n_u16(vmul_n_u16(left,32-fx),right,fx);
            }
        }else{
            top=vmla_n_u16(vmul_n_u16(channels(a),32-fx),channels(a+3),fx);
            bottom=y0==y1?top:vmla_n_u16(vmul_n_u16(channels(c),32-fx),channels(c+3),fx);
        }
        sampled=y0==y1?vrshr_n_u16(top,5):vrshrn_n_u32(vmlal_n_u16(vmull_n_u16(top,32-fy),bottom,fy),10);
    }
    if(rgb){rgb[0]=(uint8_t)vget_lane_u16(sampled,2);rgb[1]=(uint8_t)vget_lane_u16(sampled,1);rgb[2]=(uint8_t)vget_lane_u16(sampled,0);}
    uint16x4_t q=vmin_u16(vshr_n_u16(vadd_u16(sampled,vdup_n_u16(1)),1),vdup_n_u16(127));
    out[0]=(uint8_t)vget_lane_u16(q,2);out[1]=(uint8_t)vget_lane_u16(q,1);out[2]=(uint8_t)vget_lane_u16(q,0);
#else
    interpolate_neon(bgr+(size_t)y0*stride+(size_t)x0*3,
                     bgr+(size_t)y0*stride+(size_t)x1*3,
                     bgr+(size_t)y1*stride+(size_t)x0*3,
                     bgr+(size_t)y1*stride+(size_t)x1*3,
                     (unsigned)fx,(unsigned)fy,out,rgb);
#endif
}
static void coordinates_neon(const uint8_t *bgr,int width,int height,size_t stride,
                             const double *inv,uint8_t *out,uint8_t *rgb,int mode){
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
                size_t offset=((size_t)y*256+block+t)*3;
                pixel_neon(bgr,width,height,stride,(int)vgetq_lane_s64(X,0),(int)vgetq_lane_s64(Y,0),out+offset,rgb?rgb+offset:NULL);
                pixel_neon(bgr,width,height,stride,(int)vgetq_lane_s64(X,1),(int)vgetq_lane_s64(Y,1),out+offset+3,rgb?rgb+offset+3:NULL);
            }
        }
    }
}
#endif
#endif

// inv is the EXACT inverse from cv2.invert(original getPerspectiveTransform).
// mode0 keeps projective coefficients. mode1 removes only M6/M7 (experiment).
// rgb_out may be NULL; useful for comparing sampled U8 before Q7.
void posepriv_crop_bgr_q7(const uint8_t *bgr,int width,int height,size_t stride,
                         const double *inv,uint8_t *output,uint8_t *rgb_out,int mode){
#if defined(POSEPRIV_CROP_COORD_PAIR) && CROP_NEON
    coordinates_neon(bgr,width,height,stride,inv,output,rgb_out,mode);
    return;
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
                int x=X>>5,sy=Y>>5,fx=X&31,fy=Y&31;
                int x0=limit(x,width-1),x1=limit(x+1,width-1);
                int y0=limit(sy,height-1),y1=limit(sy+1,height-1);
                const uint8_t *a=bgr+(size_t)y0*stride+(size_t)x0*3;
                const uint8_t *b=bgr+(size_t)y0*stride+(size_t)x1*3;
                const uint8_t *c=bgr+(size_t)y1*stride+(size_t)x0*3;
                const uint8_t *d=bgr+(size_t)y1*stride+(size_t)x1*3;
                size_t offset=((size_t)y*256+block+t)*3;
#if CROP_NEON
                interpolate_neon(a,b,c,d,(unsigned)fx,(unsigned)fy,output+offset,rgb_out?rgb_out+offset:NULL);
#else
                unsigned wa=(unsigned)((32-fx)*(32-fy)),wb=(unsigned)(fx*(32-fy));
                unsigned wc=(unsigned)((32-fx)*fy),wd=(unsigned)(fx*fy);
                for(int channel=0;channel<3;++channel){
                    int source=2-channel;
                    unsigned u8=(a[source]*wa+b[source]*wb+c[source]*wc+d[source]*wd+512u)>>10;
                    output[offset+channel]=q7(u8);
                    if(rgb_out)rgb_out[offset+channel]=(uint8_t)u8;
                }
#endif
            }
        }
    }
#endif
}
