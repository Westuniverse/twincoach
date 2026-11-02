// SPDX-License-Identifier: Apache-2.0
// Copyright 2019-2023 The MediaPipe Authors; calculator math adaptation.
// Derived from the pinned v0.10.18 calculator transcription in
// ../adapter/decoder_reference.cpp. No NN runtime.
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>

namespace {
int finish(float p[39][5],const float*world,float presence,const int8_t*heat,
           const float*heat_lut,const float*rect,const float*pad,float*image,float*outworld) {
 if(!(presence>0.5f))return 0;
 for(int i=0;i<39;++i) {
  const int col=int(p[i][0]*64.0f),row=int(p[i][1]*64.0f);
  if(col<0||col>=64||row<0||row>=64)continue;
  float sum=0,wcol=0,wrow=0,maximum=0;
  const int left=std::max(0,col-3),right=std::min(64,col+4);
  const int top=std::max(0,row-3),bottom=std::min(64,row+4);
  for(int y=top;y<bottom;++y) {
   const int8_t*pixel=heat+(y*64+left)*39+i;
   for(int x=left;x<right;++x,pixel+=39) {
    const float weight=heat_lut[int(*pixel)+128];
    sum+=weight;wcol+=float(x)*weight;wrow+=float(y)*weight;
    maximum=std::max(maximum,weight);
   }
  }
  if(maximum>=0.5f&&sum>0) {p[i][0]=(wcol/64.0f)/sum;p[i][1]=(wrow/64.0f)/sum;}
 }
 const float cosine=std::cos(rect[4]),sine=std::sin(rect[4]);
 const float sx=1.0f-(pad[0]+pad[2]),sy=1.0f-(pad[1]+pad[3]);
 for(int i=0;i<35;++i) {
  const float x=(p[i][0]-pad[0])/sx-0.5f,y=(p[i][1]-pad[1])/sy-0.5f;
  image[i*5]=(cosine*x-sine*y)*rect[2]+rect[0];
  image[i*5+1]=(sine*x+cosine*y)*rect[3]+rect[1];
  image[i*5+2]=(p[i][2]/sx)*rect[2];
  image[i*5+3]=p[i][3];image[i*5+4]=p[i][4];
 }
 for(int i=0;i<33;++i) {
  outworld[i*5]=cosine*world[i*3]-sine*world[i*3+1];
  outworld[i*5+1]=sine*world[i*3]+cosine*world[i*3+1];
  outworld[i*5+2]=world[i*3+2];
  outworld[i*5+3]=p[i][3];outworld[i*5+4]=p[i][4];
 }
 return 1;
}
}

// Production raw heads: LUTs use the existing NumPy float32 sigmoid once at
// cold initialization. All S16/S8 indices retain their original signed Q.
extern "C" int tail4_decode_s16(const int16_t*raw,const int16_t*world_raw,
 const int16_t*presence_raw,const int8_t*heat,const float*heat_lut,
 const float*logit_q5_lut,const float*logit_q8_lut,const float*rect,const float*pad,
 float*image,float*outworld,float*presence) {
 *presence=logit_q8_lut[int(presence_raw[0])+32768];
 if(!(*presence>0.5f))return 0;
 float points[39][5],world[117];
 for(int i=0;i<39;++i) {
  for(int j=0;j<3;++j)points[i][j]=(float(raw[i*5+j])/32.0f)/256.0f;
  points[i][3]=logit_q5_lut[int(raw[i*5+3])+32768];
  points[i][4]=logit_q5_lut[int(raw[i*5+4])+32768];
 }
 for(int i=0;i<117;++i)world[i]=float(world_raw[i])/16384.0f;
 return finish(points,world,*presence,heat,heat_lut,rect,pad,image,outworld);
}

// Exact cached float heads/probability reference entry: never inverse-sigmoid
// an old probability to manufacture an S16 logit. Used only by cached8 tests.
extern "C" int tail4_decode_f32(const float*raw,const float*world,float presence,
 const int8_t*heat,const float*heat_lut,const float*rect,const float*pad,
 float*image,float*outworld) {
 if(!(presence>0.5f))return 0;
 float points[39][5];
 for(int i=0;i<39;++i) {
  for(int j=0;j<3;++j)points[i][j]=raw[i*5+j]/256.0f;
  points[i][3]=1.0f/(1.0f+std::exp(-raw[i*5+3]));
  points[i][4]=1.0f/(1.0f+std::exp(-raw[i*5+4]));
 }
 return finish(points,world,presence,heat,heat_lut,rect,pad,image,outworld);
}

namespace {
using Vec=std::array<double,3>;
double dot(const Vec&a,const Vec&b) {
 // Match Python's fsum of three already-rounded float64 products. Extended
 // accumulation avoids changing the basis when signed terms nearly cancel.
 const double x=a[0]*b[0],y=a[1]*b[1],z=a[2]*b[2];
 return double((static_cast<long double>(x)+static_cast<long double>(y))+static_cast<long double>(z));
}
bool unit(Vec&v) {
 const double length=std::sqrt(dot(v,v));
 if(!std::isfinite(length)||length<1e-8)return false;
 for(double&x:v)x/=length;
 return true;
}
Vec cross(const Vec&a,const Vec&b) {return {a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]};}
bool normalize_rotation(Vec x,Vec y,Vec z,double*out) {
 const double lx=std::sqrt(dot(x,x)),ly=std::sqrt(dot(y,y)),lz=std::sqrt(dot(z,z));
 const double minimum=std::min({lx,ly,lz}),maximum=std::max({lx,ly,lz});
 if(!std::isfinite(minimum)||!std::isfinite(maximum)||minimum<1e-8||maximum-minimum>.002*maximum)return false;
 if(!unit(x)||!unit(y)||!unit(z))return false;
 if(std::max({std::abs(dot(x,y)),std::abs(dot(x,z)),std::abs(dot(y,z))})>.002)return false;
 const double along=dot(x,y);
 for(int i=0;i<3;++i)y[i]-=along*x[i];
 if(!unit(y))return false;
 const Vec back=cross(x,y);
 if(dot(back,z)<.99)return false;
 for(int i=0;i<3;++i) {out[i*3]=x[i];out[i*3+1]=y[i];out[i*3+2]=back[i];}
 return true;
}
int basis(const float*world,const std::array<int,4>&indices,bool body,double*out) {
 Vec p[4];
 for(int k=0;k<4;++k) {
  const float*v=world+indices[k]*5;
  if(v[4]<.5f)return 1; // Original current-world admission, never confidence fill.
  p[k]={double(v[0]),-double(v[1]),double(v[2])};
 }
 Vec lateral{},up{};
 for(int i=0;i<3;++i) {lateral[i]=p[0][i]-p[1][i];up[i]=((p[0][i]+p[1][i])-p[2][i]-p[3][i])/2.0;}
 Vec x=lateral,y=up;
 if(body) {
  if(!unit(y))return 2;
  const double along=dot(lateral,y);
  for(int i=0;i<3;++i)x[i]=lateral[i]-along*y[i];
  if(!unit(x))return 2;
 } else {
  if(!unit(x))return 2;
  const double along=dot(up,x);
  for(int i=0;i<3;++i)y[i]=up[i]-along*x[i];
  if(!unit(y))return 2;
 }
 return normalize_rotation(x,y,cross(x,y),out)?0:3;
}
}

// One native interface boundary retains the WorldDisplayObservation finite/
// separate-confidence checks and CurrentHeadOrientation's exact admission,
// norm, uniform-scale, orthogonality and proper-rotation thresholds. No Python
// second scan/renormalization is needed after this succeeds.
extern "C" int tail4_validate_world_and_head(const float*image,const float*world,double*head) {
 for(int i=0;i<35;++i)for(int j=0;j<5;++j) {
  const float v=image[i*5+j];
  if(!std::isfinite(v)||(j>=3&&(v<0.0f||v>1.0f)))return -1;
 }
 for(int i=0;i<33;++i)for(int j=0;j<5;++j) {
  const float v=world[i*5+j];
  if(!std::isfinite(v)||(j>=3&&(v<0.0f||v>1.0f)))return -1;
 }
 int result=basis(world,{2,5,9,10},false,head);
 if(result)return result;
 return basis(world,{11,12,23,24},true,head+9);
}
