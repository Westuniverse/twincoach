/* SPDX-License-Identifier: GPL-2.0-only
 * Same camera ray / torso Rodrigues / 4 limb fits as frozen v2 Python.
 * Plain double batch ABI; no state/filter, no inference, no new dependency. */
#include <math.h>
#include <stdint.h>
#include <string.h>
#define V 2
#define PTS 17
#define W 33
/* params: 2*11 intrinsics/distortion, 2*9 R, 2*9 relative,
 * torso_length then [left upper/lower,right upper/lower], primary index. */
uint32_t posepriv_v2_math_abi(void){return 1;}
static double dot(const double*a,const double*b){return a[0]*b[0]+a[1]*b[1]+a[2]*b[2];}
static void cross(const double*a,const double*b,double*c){c[0]=a[1]*b[2]-a[2]*b[1];c[1]=a[2]*b[0]-a[0]*b[2];c[2]=a[0]*b[1]-a[1]*b[0];}
static int unit(double*v){double l=sqrt(dot(v,v));if(l<1e-10)return 3;for(int i=0;i<3;i++)v[i]/=l;return 0;}
static void mv(const double*A,const double*v,double*out,int transpose){for(int r=0;r<3;r++){double q=0.;for(int k=0;k<3;k++)q+=A[transpose?3*k+r:3*r+k]*v[k];out[r]=q;}}
static void distort(double x,double y,const double*k,double*out){double r=x*x+y*y;double radial=1.+k[0]*r+k[1]*pow(r,2.)+k[4]*pow(r,3.);out[0]=x*radial+2.*k[2]*x*y+k[3]*(r+2.*x*x);out[1]=y*radial+k[2]*(r+2.*y*y)+2.*k[3]*x*y;}
static int ray(const double*p,const double*raw,int v,int i,double cache[2][17][3],int ready[2][17]){
 if(ready[v][i])return ready[v][i]>0?0:-ready[v][i];
 const double*q=raw+3*(17*v+i);int status=0;
 if(q[2]<.5)status=1;else if(!(q[0]>=0.&&q[0]<=1.&&q[1]>=0.&&q[1]<=1.))status=2;
 if(status){ready[v][i]=-status;return status;}
 const double*k=p+11*v;double xd=(q[0]*k[0]-k[4])/k[2],yd=(q[1]*k[1]-k[5])/k[3],x=xd,y=yd;
 const double*d=k+6;int zero=1;for(int j=0;j<5;j++)zero&=d[j]==0.;
 if(!zero){int fallback=0;double projected[2];
  for(int j=0;j<8;j++){double r=x*x+y*y,radial=1.+d[0]*r+d[1]*r*r+d[4]*r*r*r;
   if(fabs(radial)<=1e-10){fallback=1;break;}
   double dx=2.*d[2]*x*y+d[3]*(r+2.*x*x),dy=d[2]*(r+2.*y*y)+2.*d[3]*x*y;
   double nx=(xd-dx)/radial,ny=(yd-dy)/radial;x=nx;y=ny;
   if(!isfinite(x)||!isfinite(y)){fallback=1;break;}}
  if(!fallback){distort(x,y,d,projected);if(hypot(projected[0]-xd,projected[1]-yd)>1e-7)fallback=1;}
  if(fallback){x=xd;y=yd;for(int j=0;j<64;j++){double r=x*x+y*y,radial=1.+d[0]*r+d[1]*r*r+d[4]*r*r*r;
    double nx=(xd-2.*d[2]*x*y-d[3]*(r+2.*x*x))/radial;
    double ny=(yd-d[2]*(r+2.*y*y)-2.*d[3]*x*y)/radial;x=nx;y=ny;}
   distort(x,y,d,projected);if(hypot(projected[0]-xd,projected[1]-yd)>1e-7){ready[v][i]=-9;return 9;}}
 }
 cache[v][i][0]=x;cache[v][i][1]=y;cache[v][i][2]=1.;ready[v][i]=1;return 0;
}
static int axis(const double*p,const double*raw,double cache[2][17][3],int ready[2][17],double shoulder[2][3],double hip[2][3],double up[3],int torso_q_first){
 const int ids[4]={5,6,11,12};double normal[2][3];
 for(int v=0;v<2;v++){
  /* Torso v2 checks all four q values before all four image bounds;
   * the original arm-scale leaf checks sequential rays instead. */
  if(torso_q_first){for(int i=0;i<4;i++)if(raw[3*(17*v+ids[i])+2]<.5)return 1;
   for(int i=0;i<4;i++){const double*q=raw+3*(17*v+ids[i]);if(!(q[0]>=0.&&q[0]<=1.&&q[1]>=0.&&q[1]<=1.))return 2;}}
  for(int i=0;i<4;i++){int e=ray(p,raw,v,ids[i],cache,ready);if(e)return e;}
  for(int k=0;k<3;k++){shoulder[v][k]=(cache[v][5][k]+cache[v][6][k])/2.;hip[v][k]=(cache[v][11][k]+cache[v][12][k])/2.;}
  double c[3];cross(hip[v],shoulder[v],c);mv(p+22+9*v,c,normal[v],0);int e=unit(normal[v]);if(e)return e;}
 cross(normal[0],normal[1],up);int e=unit(up);if(e)return e;double signs[2];
 for(int v=0;v<2;v++){double a[3];mv(p+22+9*v,up,a,1);signs[v]=(a[0]-hip[v][0]*a[2])*(shoulder[v][0]-hip[v][0])+(a[1]-hip[v][1]*a[2])*(shoulder[v][1]-hip[v][1]);}
 if(signs[0]*signs[1]<=0.)return 4;if(signs[0]<0.)for(int k=0;k<3;k++)up[k]=-up[k];return 0;
}
static int solve(double M[3][3],double b[3],double out[3]){
 for(int i=0;i<3;i++){int pivot=i;for(int j=i+1;j<3;j++)if(fabs(M[j][i])>fabs(M[pivot][i]))pivot=j;
  if(M[pivot][i]==0.)return 10;
  if(pivot!=i){for(int k=0;k<3;k++){double t=M[i][k];M[i][k]=M[pivot][k];M[pivot][k]=t;}double t=b[i];b[i]=b[pivot];b[pivot]=t;}
  for(int j=i+1;j<3;j++){double factor=M[j][i]/M[i][i];M[j][i]=0.;for(int k=i+1;k<3;k++)M[j][k]-=factor*M[i][k];b[j]-=factor*b[i];}}
 for(int i=2;i>=0;i--){double s=b[i];for(int k=i+1;k<3;k++)s-=M[i][k]*out[k];out[i]=s/M[i][i];}return 0;
}
int posepriv_v2_math_apply(const double*p,const double*raw,const double*world,double*out,uint64_t*mask,double*meta,int*codes){
 memcpy(out,world,33*3*sizeof(double));*mask=0;for(int i=0;i<28;i++)meta[i]=0.;for(int i=0;i<7;i++)codes[i]=-1;codes[0]=0;
 double cache[2][17][3]={0};int ready[2][17]={0};double shoulder[2][3],hip[2][3],up[3];
 int e=axis(p,raw,cache,ready,shoulder,hip,up,1),primary=(int)p[63];codes[1]=e;
 if(!e){double target[3],pivot[3],original[3];mv(p+22+9*primary,up,target,1);
  for(int k=0;k<3;k++){pivot[k]=(world[23*3+k]+world[24*3+k])/2.;original[k]=(world[11*3+k]+world[12*3+k])/2.-pivot[k];}
  e=unit(original);if(!e){double cr[3],c=dot(original,target);cross(original,target,cr);
   if(c<-.999999)e=5;else{
    double K[9]={0.,-cr[2],cr[1],cr[2],0.,-cr[0],-cr[1],cr[0],0.},R[9];
    for(int i=0;i<3;i++)for(int j=0;j<3;j++){double q=0.;for(int k=0;k<3;k++)q+=K[3*i+k]*K[3*k+j];R[3*i+j]=(i==j?1.:0.)+K[3*i+j]+q/(1.+c);}
    for(int i=0;i<23;i++){double v[3],q[3];for(int k=0;k<3;k++)v[k]=world[3*i+k]-pivot[k];mv(R,v,q,0);for(int k=0;k<3;k++)out[3*i+k]=q[k]+pivot[k];}
    double delta[3];for(int k=0;k<3;k++)delta[k]=(out[33+k]+out[36+k]-world[33+k]-world[36+k])/2.;
    for(int i=0;i<11;i++)for(int k=0;k<3;k++)out[3*i+k]=world[3*i+k]+delta[k];
    codes[0]=1;*mask=(UINT64_C(1)<<23)-1;meta[0]=acos(fmax(-1.,fmin(1.,c)))*180./acos(-1.);
    memcpy(meta+1,R,9*sizeof(double));memcpy(meta+10,pivot,3*sizeof(double));memcpy(meta+13,delta,3*sizeof(double));
   }}codes[1]=e;
 }
 /* Original arms use the same raw image rays and torso scale. */
 e=axis(p,raw,cache,ready,shoulder,hip,up,0);codes[2]=e;if(e)return 0;
 double torso[3];mv(p+22+9*primary,up,torso,1);torso[1]=-torso[1];for(int k=0;k<3;k++)torso[k]*=p[58];double scales[2];
 for(int v=0;v<2;v++){const double*rel=p+40+9*v;double projected[2],actual[2];
  for(int r=0;r<2;r++){double q=0.;for(int k=0;k<3;k++)q+=(rel[3*r+k]-hip[v][r]*rel[6+k])*torso[k];projected[r]=q;actual[r]=shoulder[v][r]-hip[v][r];}
  scales[v]=(projected[0]*actual[0]+projected[1]*actual[1])/(projected[0]*projected[0]+projected[1]*projected[1]);if(scales[v]<=0.){codes[2]=6;return 0;}}
 for(int side=0;side<2;side++){
  int wi[3]={11+side,13+side,15+side},ki[3]={5+side,7+side,9+side};double original[2][3],length[2];int degenerate=0;
  for(int seg=0;seg<2;seg++){for(int k=0;k<3;k++)original[seg][k]=out[3*wi[seg+1]+k]-out[3*wi[seg]+k];length[seg]=sqrt(dot(original[seg],original[seg]));original[seg][1]=-original[seg][1];if(unit(original[seg]))degenerate=1;}
  if(degenerate){codes[3+2*side]=codes[4+2*side]=8;continue;}
  double direction[2][3],weights[2]={0.,0.};
  for(int seg=0;seg<2;seg++){
   int n=2*side+seg;double A[4][3],b[4];e=0;
   for(int v=0;v<2;v++){
    e=ray(p,raw,v,ki[seg],cache,ready);if(e)break;e=ray(p,raw,v,ki[seg+1],cache,ready);if(e)break;
    const double*rel=p+40+9*v;double q=sqrt(fmin(raw[3*(17*v+ki[seg])+2],raw[3*(17*v+ki[seg+1])+2]));
    for(int r=0;r<2;r++){double focal=p[11*v+2+r];for(int k=0;k<3;k++)A[2*v+r][k]=(q*scales[v]*focal)*(rel[3*r+k]-cache[v][ki[seg]][r]*rel[6+k])*p[59+n];b[2*v+r]=(q*focal)*(cache[v][ki[seg+1]][r]-cache[v][ki[seg]][r]);}}
   if(!e){double M[3][3]={{0}},rhs[3]={0},fit[3]={0};
    for(int i=0;i<3;i++){for(int r=0;r<4;r++)rhs[i]+=A[r][i]*b[r];for(int j=0;j<3;j++)for(int r=0;r<4;r++)M[i][j]+=A[r][i]*A[r][j];}
    e=solve(M,rhs,fit);if(!e)e=unit(fit);
    if(!e){double e0=0.,e1=0.;for(int r=0;r<4;r++){double before=dot(A[r],original[seg])-b[r],after=dot(A[r],fit)-b[r];e0+=before*before;e1+=after*after;}
      double w=e0+e1?fmax(0.,(e0-e1)/(e0+e1)):0.;for(int k=0;k<3;k++)direction[seg][k]=(1.-w)*original[seg][k]+w*fit[k];e=unit(direction[seg]);
      if(!e){weights[seg]=w;meta[16+n]=w;meta[20+n]=e0;meta[24+n]=e1;codes[3+n]=7;}}
   }
   if(e){memcpy(direction[seg],original[seg],3*sizeof(double));codes[3+n]=e;}
   direction[seg][1]=-direction[seg][1];
  }
  if(fmax(weights[0],weights[1])==0.)continue;
  double elbow[3],wrist[3],shift[3];for(int k=0;k<3;k++){elbow[k]=out[3*wi[0]+k]+length[0]*direction[0][k];wrist[k]=elbow[k]+length[1]*direction[1][k];shift[k]=wrist[k]-out[3*wi[2]+k];}
  for(int k=0;k<3;k++){out[3*wi[1]+k]=elbow[k];out[3*wi[2]+k]=wrist[k];}
  *mask|=(UINT64_C(1)<<wi[1])|(UINT64_C(1)<<wi[2]);
  for(int i=17+side;i<=21+side;i+=2){for(int k=0;k<3;k++)out[3*i+k]+=shift[k];*mask|=UINT64_C(1)<<i;}
 }
 return 0;
}
