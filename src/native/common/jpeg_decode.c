/* SPDX-License-Identifier: GPL-2.0-only
 * Original libjpeg API adapter. This software is based in part on the work
 * of the Independent JPEG Group. libjpeg-turbo is linked, not incorporated.
 */
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <setjmp.h>
#include <jpeglib.h>
#ifndef POSEPRIV_JPEG_NEON_ROTATE
#define POSEPRIV_JPEG_NEON_ROTATE 0
#endif
#if defined(__aarch64__) && POSEPRIV_JPEG_NEON_ROTATE
#include <arm_neon.h>
#endif

#define TILE 32
typedef struct {
    struct jpeg_error_mgr base;
    jmp_buf jump;
    char message[JMSG_LENGTH_MAX];
} Error;
typedef struct {
    struct jpeg_decompress_struct jpeg;
    Error error;
    unsigned char *tile;
    size_t tile_capacity;
    unsigned width,height;
    int rotation,ready;
} Decoder;

static void failed(j_common_ptr jpeg){
    Error *error=(Error*)jpeg->err;
    (*jpeg->err->format_message)(jpeg,error->message);
    longjmp(error->jump,1);
}

void *posepriv_jpeg_create(void){
    Decoder *volatile d=calloc(1,sizeof(*d));if(!d)return NULL;
    d->jpeg.err=jpeg_std_error(&d->error.base);d->error.base.error_exit=failed;
    if(setjmp(d->error.jump)){jpeg_destroy_decompress(&d->jpeg);free(d);return NULL;}
    jpeg_create_decompress(&d->jpeg);
    return d;
}

void posepriv_jpeg_destroy(void *opaque){
    Decoder *d=opaque;if(!d)return;
    jpeg_destroy_decompress(&d->jpeg);free(d->tile);free(d);
}

const char *posepriv_jpeg_error(void *opaque){return ((Decoder*)opaque)->error.message;}
int posepriv_jpeg_abi(void){return 1;}
int posepriv_jpeg_header_version(void){return JPEG_LIB_VERSION;}

/* Header/readiness belongs to one thread-local handle and one immutable input
 * buffer, kept alive by the Python caller through finish. Never shared by two
 * simultaneous decodes. Caller owns the eventual independent image buffer. */
int posepriv_jpeg_header(void *opaque,const unsigned char *input,size_t length,
        int scale,int rotation,unsigned dimensions[2]){
    Decoder *d=opaque;d->ready=0;
    if(setjmp(d->error.jump)){jpeg_abort_decompress(&d->jpeg);return -1;}
    if((scale!=1&&scale!=2&&scale!=4&&scale!=8)||
       (rotation!=0&&rotation!=90&&rotation!=180&&rotation!=270)){
        snprintf(d->error.message,sizeof(d->error.message),"Unsupported JPEG scale/rotation");return -2;
    }
    jpeg_mem_src(&d->jpeg,input,length);
    jpeg_save_markers(&d->jpeg,JPEG_APP0+1,65535);
    if(jpeg_read_header(&d->jpeg,TRUE)!=JPEG_HEADER_OK){jpeg_abort_decompress(&d->jpeg);return -1;}
    for(jpeg_saved_marker_ptr marker=d->jpeg.marker_list;marker;marker=marker->next){
        if(marker->marker==JPEG_APP0+1&&marker->data_length>=6&&memcmp(marker->data,"Exif\0\0",6)==0){
            snprintf(d->error.message,sizeof(d->error.message),"EXIF input requires original OpenCV orientation path");jpeg_abort_decompress(&d->jpeg);return -2;
        }
    }
    if(d->jpeg.num_components!=1&&d->jpeg.num_components!=3){
        snprintf(d->error.message,sizeof(d->error.message),"CMYK/other JPEG requires original OpenCV color conversion");jpeg_abort_decompress(&d->jpeg);return -2;
    }
    /* Exact OpenCV4.5.4 default decode settings. JCS_EXT_BGR selects the same
     * component values in BGR order without OpenCV's separate RGB-swap row. */
    d->jpeg.scale_num=1;d->jpeg.scale_denom=(unsigned)scale;
    d->jpeg.dct_method=JDCT_ISLOW;
    d->jpeg.do_fancy_upsampling=TRUE;d->jpeg.do_block_smoothing=TRUE;
    d->jpeg.out_color_space=JCS_EXT_BGR;
    jpeg_calc_output_dimensions(&d->jpeg);
    d->width=d->jpeg.output_width;d->height=d->jpeg.output_height;d->rotation=rotation;
    dimensions[0]=(rotation==90||rotation==270)?d->width:d->height;
    dimensions[1]=(rotation==90||rotation==270)?d->height:d->width;
    d->ready=1;return 0;
}

#if defined(__aarch64__) && POSEPRIV_JPEG_NEON_ROTATE
/* Deinterleaved 8-bit BGR channel rows -> channel columns. All lane shuffles
 * are exact byte permutations; no JPEG arithmetic or color conversion here. */
static inline void transpose8_u8(uint8x8_t r0,uint8x8_t r1,uint8x8_t r2,uint8x8_t r3,
        uint8x8_t r4,uint8x8_t r5,uint8x8_t r6,uint8x8_t r7,uint8x8_t out[8]){
    uint8x8x2_t a0=vtrn_u8(r0,r1),a1=vtrn_u8(r2,r3),a2=vtrn_u8(r4,r5),a3=vtrn_u8(r6,r7);
    uint16x4x2_t b0=vtrn_u16(vreinterpret_u16_u8(a0.val[0]),vreinterpret_u16_u8(a1.val[0]));
    uint16x4x2_t b1=vtrn_u16(vreinterpret_u16_u8(a0.val[1]),vreinterpret_u16_u8(a1.val[1]));
    uint16x4x2_t b2=vtrn_u16(vreinterpret_u16_u8(a2.val[0]),vreinterpret_u16_u8(a3.val[0]));
    uint16x4x2_t b3=vtrn_u16(vreinterpret_u16_u8(a2.val[1]),vreinterpret_u16_u8(a3.val[1]));
    uint32x2x2_t c0=vtrn_u32(vreinterpret_u32_u16(b0.val[0]),vreinterpret_u32_u16(b2.val[0]));
    uint32x2x2_t c1=vtrn_u32(vreinterpret_u32_u16(b1.val[0]),vreinterpret_u32_u16(b3.val[0]));
    uint32x2x2_t c2=vtrn_u32(vreinterpret_u32_u16(b0.val[1]),vreinterpret_u32_u16(b2.val[1]));
    uint32x2x2_t c3=vtrn_u32(vreinterpret_u32_u16(b1.val[1]),vreinterpret_u32_u16(b3.val[1]));
    out[0]=vreinterpret_u8_u32(c0.val[0]);out[4]=vreinterpret_u8_u32(c0.val[1]);
    out[1]=vreinterpret_u8_u32(c1.val[0]);out[5]=vreinterpret_u8_u32(c1.val[1]);
    out[2]=vreinterpret_u8_u32(c2.val[0]);out[6]=vreinterpret_u8_u32(c2.val[1]);
    out[3]=vreinterpret_u8_u32(c3.val[0]);out[7]=vreinterpret_u8_u32(c3.val[1]);
}

static inline void rotate_tail_pixel(const unsigned char *tile,unsigned char *out,
        unsigned width,unsigned height,unsigned first,unsigned y,unsigned x,int rotation){
    const unsigned char *p=tile+((size_t)y*width+x)*3;
    unsigned char *q=rotation==90?out+((size_t)x*height+height-1-first-y)*3:
        out+((size_t)(width-1-x)*height+first+y)*3;
    q[0]=p[0];q[1]=p[1];q[2]=p[2];
}

static void rotate_tile_neon(const unsigned char *tile,unsigned char *out,
        unsigned width,unsigned height,unsigned first,unsigned rows,int rotation){
    unsigned full_width=width&~7U,full_rows=rows&~7U;
    size_t stride=(size_t)width*3;
    for(unsigned by=0;by<full_rows;by+=8)for(unsigned bx=0;bx<full_width;bx+=8){
        const unsigned char *p=tile+((size_t)by*width+bx)*3;
        uint8x8x3_t r0=vld3_u8(p),r1=vld3_u8(p+stride),r2=vld3_u8(p+2*stride),r3=vld3_u8(p+3*stride);
        uint8x8x3_t r4=vld3_u8(p+4*stride),r5=vld3_u8(p+5*stride),r6=vld3_u8(p+6*stride),r7=vld3_u8(p+7*stride);
        uint8x8_t blue[8],green[8],red[8];
        transpose8_u8(r0.val[0],r1.val[0],r2.val[0],r3.val[0],r4.val[0],r5.val[0],r6.val[0],r7.val[0],blue);
        transpose8_u8(r0.val[1],r1.val[1],r2.val[1],r3.val[1],r4.val[1],r5.val[1],r6.val[1],r7.val[1],green);
        transpose8_u8(r0.val[2],r1.val[2],r2.val[2],r3.val[2],r4.val[2],r5.val[2],r6.val[2],r7.val[2],red);
#define STORE90(N) do { \
        uint8x8x3_t pixel={{vrev64_u8(blue[N]),vrev64_u8(green[N]),vrev64_u8(red[N])}}; \
        vst3_u8(out+((size_t)(bx+(N))*height+height-first-by-8)*3,pixel); \
    } while(0)
#define STORE270(N) do { \
        uint8x8x3_t pixel={{blue[N],green[N],red[N]}}; \
        vst3_u8(out+((size_t)(width-1-bx-(N))*height+first+by)*3,pixel); \
    } while(0)
        if(rotation==90){STORE90(0);STORE90(1);STORE90(2);STORE90(3);STORE90(4);STORE90(5);STORE90(6);STORE90(7);}
        else{STORE270(0);STORE270(1);STORE270(2);STORE270(3);STORE270(4);STORE270(5);STORE270(6);STORE270(7);}
#undef STORE90
#undef STORE270
    }
    /* Only complete8x8 blocks enter NEON; odd columns/rows keep scalar bytes. */
    for(unsigned x=full_width;x<width;x++)for(unsigned y=0;y<full_rows;y++)
        rotate_tail_pixel(tile,out,width,height,first,y,x,rotation);
    for(unsigned y=full_rows;y<rows;y++)for(unsigned x=0;x<width;x++)
        rotate_tail_pixel(tile,out,width,height,first,y,x,rotation);
}
#endif

static void rotate_tile(const unsigned char *tile,unsigned char *out,
        unsigned width,unsigned height,unsigned first,unsigned rows,int rotation){
#if defined(__aarch64__) && POSEPRIV_JPEG_NEON_ROTATE
    if(rotation==90||rotation==270){rotate_tile_neon(tile,out,width,height,first,rows,rotation);return;}
#endif
    if(rotation==180){
        for(unsigned y=0;y<rows;y++)for(unsigned x=0;x<width;x++){
            const unsigned char *p=tile+((size_t)y*width+x)*3;
            unsigned char *q=out+((size_t)(height-1-first-y)*width+width-1-x)*3;
            q[0]=p[0];q[1]=p[1];q[2]=p[2];
        }
        return;
    }
    /* Input32-row strip -> small x tiles -> contiguous destination row. The
     * persistent strip replaces a whole temporary decoded BGR+cv2.rotate copy. */
    for(unsigned bx=0;bx<width;bx+=TILE){
        unsigned end=bx+TILE<width?bx+TILE:width;
        for(unsigned x=bx;x<end;x++){
            unsigned char *q=rotation==90?out+((size_t)x*height+height-first-rows)*3:
                out+((size_t)(width-1-x)*height+first)*3;
            if(rotation==90){
                for(unsigned y=rows;y>0;y--){
                    const unsigned char *p=tile+((size_t)(y-1)*width+x)*3;
                    q[0]=p[0];q[1]=p[1];q[2]=p[2];q+=3;
                }
            }else{
                for(unsigned y=0;y<rows;y++){
                    const unsigned char *p=tile+((size_t)y*width+x)*3;
                    q[0]=p[0];q[1]=p[1];q[2]=p[2];q+=3;
                }
            }
        }
    }
}

int posepriv_jpeg_finish(void *opaque,unsigned char *output,size_t capacity){
    Decoder *d=opaque;
    if(!d->ready){snprintf(d->error.message,sizeof(d->error.message),"JPEG header not ready");return -1;}
    d->ready=0;
    if(setjmp(d->error.jump)){jpeg_abort_decompress(&d->jpeg);return -1;}
    size_t bytes=(size_t)d->width*d->height*3;
    if(capacity<bytes){snprintf(d->error.message,sizeof(d->error.message),"Caller image buffer too small");jpeg_abort_decompress(&d->jpeg);return -1;}
    size_t stride=(size_t)d->width*3;
    if(d->rotation&&d->tile_capacity<stride*TILE){
        unsigned char *p=realloc(d->tile,stride*TILE);
        if(!p){snprintf(d->error.message,sizeof(d->error.message),"JPEG strip allocation failed");jpeg_abort_decompress(&d->jpeg);return -1;}
        d->tile=p;d->tile_capacity=stride*TILE;
    }
    jpeg_start_decompress(&d->jpeg);
    while(d->jpeg.output_scanline<d->height){
        unsigned first=d->jpeg.output_scanline;
        unsigned count=d->height-first<TILE?d->height-first:TILE;
        JSAMPROW rows[TILE];
        for(unsigned i=0;i<count;i++)rows[i]=d->rotation?d->tile+i*stride:output+((size_t)first+i)*stride;
        unsigned got=0;
        while(got<count){
            unsigned n=jpeg_read_scanlines(&d->jpeg,rows+got,count-got);
            if(!n){snprintf(d->error.message,sizeof(d->error.message),"Suspended JPEG scanline");jpeg_abort_decompress(&d->jpeg);return -1;}got+=n;
        }
        if(d->rotation)rotate_tile(d->tile,output,d->width,d->height,first,count,d->rotation);
    }
    jpeg_finish_decompress(&d->jpeg);return 0;
}
