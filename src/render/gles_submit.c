// SPDX-License-Identifier: GPL-2.0-only
// Original project GLES submission order, from field_preview_gles.py.
// Uses the existing system libGLESv2.so.2 via POSIX dlopen/dlsym; no GL source copied.
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <dlfcn.h>

typedef struct {
    void (*BindTexture)(uint32_t,uint32_t);
    void (*Viewport)(int,int,int,int);
    void (*UseProgram)(uint32_t);
    void (*BindBuffer)(uint32_t,uint32_t);
    void (*EnableVertexAttribArray)(uint32_t);
    void (*VertexAttribPointer)(uint32_t,int,uint32_t,uint8_t,int,const void*);
    void (*Uniform1i)(int,int);
    void (*DrawArrays)(uint32_t,int,int);
    void (*BufferData)(uint32_t,intptr_t,const void*,uint32_t);
    void (*Uniform3f)(int,float,float,float);
    void (*LineWidth)(float);
    void (*Disable)(uint32_t);
    void (*Enable)(uint32_t);
    void (*ClearColor)(float,float,float,float);
    void (*Clear)(uint32_t);
    void (*TexSubImage2D)(uint32_t,int,int,int,int,int,uint32_t,uint32_t,const void*);
    void (*BufferSubData)(uint32_t,intptr_t,intptr_t,const void*);
    void (*UniformMatrix3fv)(int,int,uint8_t,const float*);
    void (*UniformMatrix4fv)(int,int,uint8_t,const float*);
    void (*DrawElements)(uint32_t,int,uint32_t,const void*);
    void (*Flush)(void);
} pp_api;

typedef struct {uint32_t buffer;int location,size;uint32_t type;} pp_attribute;
typedef struct {
    uint32_t mesh,quad,overlay,qv,pv,iv,overlay_vbo,bg;
    int quad_pos,quad_uv,quad_image,overlay_pos,overlay_color,overlay_controls;
    int mesh_pos,mesh_bones,mesh_light,mesh_transform;
    pp_attribute attributes[4];
    int full_viewport[4],avatar_viewport[4],background_viewport[4];
    float line_width,point_size;
} pp_config;
typedef struct {uint32_t texture;int x,y,width,height;const void *pixels;} pp_upload;
typedef struct {
    pp_upload upload;int viewport[4];
    const float *lines,*dots;int line_count,dot_count;
} pp_video;
typedef struct {
    const pp_upload *backgrounds;uint32_t background_count;
    const pp_video *videos;uint32_t video_count;
    const void *skin;intptr_t skin_bytes;
    const void *indices;intptr_t index_bytes;int index_count,index_update;
    const float *bones,*light,*transform;
    int reserved_upload_before_draw,flush; // ABI1 layout retained; the caller fixes reserved to0.
} pp_frame;
typedef struct {pp_api api;void *library;} pp_context;

static const char *last_error;
const char *posepriv_gl_error(void){return last_error;}
uint32_t posepriv_gl_abi(void){return 1;}

void *posepriv_gl_create(const char *library){
    pp_context *ctx=calloc(1,sizeof(*ctx));
    if(!ctx){last_error="cannot allocate GLES submission context";return NULL;}
    ctx->library=dlopen(library,RTLD_NOW|RTLD_LOCAL);
    if(!ctx->library){last_error=dlerror();free(ctx);return NULL;}
#define LOAD(name) do {void *p=dlsym(ctx->library,"gl" #name); if(!p){last_error="missing gl" #name;dlclose(ctx->library);free(ctx);return NULL;} memcpy(&ctx->api.name,&p,sizeof(p));}while(0)
    LOAD(BindTexture);LOAD(Viewport);LOAD(UseProgram);LOAD(BindBuffer);
    LOAD(EnableVertexAttribArray);LOAD(VertexAttribPointer);LOAD(Uniform1i);LOAD(DrawArrays);
    LOAD(BufferData);LOAD(Uniform3f);LOAD(LineWidth);LOAD(Disable);LOAD(Enable);LOAD(ClearColor);
    LOAD(Clear);LOAD(TexSubImage2D);LOAD(BufferSubData);LOAD(UniformMatrix3fv);
    LOAD(UniformMatrix4fv);LOAD(DrawElements);LOAD(Flush);
#undef LOAD
    return ctx;
}
void *posepriv_gl_create_callbacks(const pp_api *api){
    pp_context *ctx=calloc(1,sizeof(*ctx));if(ctx)ctx->api=*api;return ctx;
}
void posepriv_gl_destroy(void *context){
    pp_context *ctx=context;if(!ctx)return;
    if(ctx->library)dlclose(ctx->library);
    free(ctx);
}

static void viewport(const pp_api *g,const int *v){g->Viewport(v[0],v[1],v[2],v[3]);}
static void quad(const pp_api *g,const pp_config *c,uint32_t texture,const int *box){
    g->BindTexture(0x0DE1,texture);viewport(g,box);
    g->UseProgram(c->quad);g->BindBuffer(0x8892,c->qv);
    g->EnableVertexAttribArray((uint32_t)c->quad_pos);
    g->VertexAttribPointer((uint32_t)c->quad_pos,2,0x1406,0,16,NULL);
    g->EnableVertexAttribArray((uint32_t)c->quad_uv);
    g->VertexAttribPointer((uint32_t)c->quad_uv,2,0x1406,0,16,(const void*)(uintptr_t)8);
    g->Uniform1i(c->quad_image,0);g->DrawArrays(5,0,4);
}
static void upload(const pp_api *g,const pp_upload *u){
    g->BindTexture(0x0DE1,u->texture);
    g->TexSubImage2D(0x0DE1,0,u->x,u->y,u->width,u->height,0x1908,0x1401,u->pixels);
}
static void overlay(const pp_api *g,const pp_config *c,const pp_video *v){
    viewport(g,c->full_viewport);g->UseProgram(c->overlay);g->BindBuffer(0x8892,c->overlay_vbo);
    g->LineWidth(c->line_width);
    for(int pass=0;pass<2;++pass){
        const float *vertices=pass?v->dots:v->lines;int count=pass?v->dot_count:v->line_count;
        if(!count)continue;
        g->BufferData(0x8892,(intptr_t)count*5*(intptr_t)sizeof(float),vertices,0x88E8);
        g->EnableVertexAttribArray((uint32_t)c->overlay_pos);
        g->VertexAttribPointer((uint32_t)c->overlay_pos,2,0x1406,0,20,NULL);
        g->EnableVertexAttribArray((uint32_t)c->overlay_color);
        g->VertexAttribPointer((uint32_t)c->overlay_color,3,0x1406,0,20,(const void*)(uintptr_t)8);
        g->Uniform3f(c->overlay_controls,c->point_size,(float)pass,0.f);
        g->DrawArrays(pass?0:1,0,count);
    }
}

void posepriv_gl_submit(void *context,const pp_config *c,const pp_frame *f){
    const pp_api *g=&((pp_context*)context)->api;
    g->Disable(0x0B71);g->ClearColor(0.f,0.f,0.f,1.f);g->Clear(0x4000|0x0100);
    g->BindTexture(0x0DE1,c->bg);
    for(uint32_t i=0;i<f->background_count;++i){const pp_upload *u=&f->backgrounds[i];
        g->TexSubImage2D(0x0DE1,0,u->x,u->y,u->width,u->height,0x1908,0x1401,u->pixels);
    }
    quad(g,c,c->bg,c->background_viewport);
    for(uint32_t i=0;i<f->video_count;++i){
        const pp_video *v=&f->videos[i];upload(g,&v->upload);
        quad(g,c,v->upload.texture,v->viewport);overlay(g,c,v);
    }
    if(f->index_update){g->BindBuffer(0x8893,c->iv);g->BufferData(0x8893,f->index_bytes,f->indices,0x88E8);}
    g->Enable(0x0B71);viewport(g,c->avatar_viewport);g->UseProgram(c->mesh);
    g->BindBuffer(0x8892,c->pv);g->BufferSubData(0x8892,0,f->skin_bytes,f->skin);
    g->EnableVertexAttribArray((uint32_t)c->mesh_pos);
    g->VertexAttribPointer((uint32_t)c->mesh_pos,3,0x1402,0,16,NULL);
    for(int i=0;i<4;++i){const pp_attribute *a=&c->attributes[i];
        g->BindBuffer(0x8892,a->buffer);g->EnableVertexAttribArray((uint32_t)a->location);
        g->VertexAttribPointer((uint32_t)a->location,a->size,a->type,0,0,NULL);
    }
    g->UniformMatrix3fv(c->mesh_bones,16,0,f->bones);
    g->Uniform3f(c->mesh_light,f->light[0],f->light[1],f->light[2]);
    g->UniformMatrix4fv(c->mesh_transform,1,0,f->transform);
    g->BindBuffer(0x8893,c->iv);g->DrawElements(4,f->index_count,0x1403,NULL);
    if(f->flush)g->Flush();
}
