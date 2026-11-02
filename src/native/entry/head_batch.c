/* SPDX-License-Identifier: GPL-2.0-only
 * Existing CPython/math.fsum/builtins.sum contract, batched head math.
 * No policy/state/filter ownership here. All state is input/output. */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <math.h>
#include <stdint.h>
#include <string.h>
static PyObject *fsum_fn,*sum_fn;
static int failed;
static double sumof(const double*a,int n,int exact){PyObject*t=PyTuple_New(n);if(!t){failed=1;return 0.;}for(int i=0;i<n;i++)PyTuple_SET_ITEM(t,i,PyFloat_FromDouble(a[i]));PyObject*r=PyObject_CallOneArg(exact?fsum_fn:sum_fn,t);Py_DECREF(t);if(!r){failed=1;return 0.;}double v=PyFloat_AsDouble(r);Py_DECREF(r);return v;}
static double dot(const double*a,const double*b,int n,int exact){double p[4];for(int i=0;i<n;i++)p[i]=a[i]*b[i];return sumof(p,n,exact);}
static int unit(double*v,int n,int exact,double eps){double l=sqrt(dot(v,v,n,exact));if(!isfinite(l)||(exact?l<eps:l<=eps)){PyErr_SetString(PyExc_ValueError,"degenerate head orientation");failed=1;return 0;}for(int i=0;i<n;i++)v[i]/=l;return 1;}
static void cross(const double*a,const double*b,double*c){c[0]=a[1]*b[2]-a[2]*b[1];c[1]=a[2]*b[0]-a[0]*b[2];c[2]=a[0]*b[1]-a[1]*b[0];}
static void mm(const double*a,const double*b,double*out,int ta,int tb){double z[9];for(int r=0;r<3;r++)for(int c=0;c<3;c++){double aa[3],bb[3];for(int k=0;k<3;k++){aa[k]=a[ta?3*k+r:3*r+k];bb[k]=b[tb?3*c+k:3*k+c];}z[3*r+c]=dot(aa,bb,3,1);}memcpy(out,z,sizeof(z));}
static void mv(const double*a,const double*b,double*out){double z[3];for(int r=0;r<3;r++)z[r]=dot(a+3*r,b,3,1);memcpy(out,z,sizeof(z));}
static int rotation(const double*a,double*out){double ax[3][3],len[3];for(int c=0;c<3;c++){for(int r=0;r<3;r++)ax[c][r]=a[3*r+c];len[c]=sqrt(dot(ax[c],ax[c],3,1));}double mn=fmin(len[0],fmin(len[1],len[2])),mx=fmax(len[0],fmax(len[1],len[2]));if(mn<1e-8||mx-mn>.002*mx){PyErr_SetString(PyExc_ValueError,"head rotation must have uniform positive scale");failed=1;return 0;}for(int c=0;c<3;c++)unit(ax[c],3,1,1e-8);double xy=dot(ax[0],ax[1],3,1),xz=dot(ax[0],ax[2],3,1),yz=dot(ax[1],ax[2],3,1);if(fmax(fabs(xy),fmax(fabs(xz),fabs(yz)))>.002){PyErr_SetString(PyExc_ValueError,"head rotation must be orthogonal");failed=1;return 0;}for(int r=0;r<3;r++)ax[1][r]-=xy*ax[0][r];unit(ax[1],3,1,1e-8);double back[3];cross(ax[0],ax[1],back);if(dot(back,ax[2],3,1)<.99){PyErr_SetString(PyExc_ValueError,"reflected head rotation");failed=1;return 0;}for(int r=0;r<3;r++){out[3*r]=ax[0][r];out[3*r+1]=ax[1][r];out[3*r+2]=back[r];}return !failed;}
static void quaternion(const double*m,double*q){double t[3]={m[0],m[4],m[8]},trace=sumof(t,3,1);if(trace>0){double s=2*sqrt(1+trace);q[0]=s/4;q[1]=(m[7]-m[5])/s;q[2]=(m[2]-m[6])/s;q[3]=(m[3]-m[1])/s;}else{int k=0;for(int i=1;i<3;i++)if(m[4*i]>m[4*k])k=i;int j=(k+1)%3,l=(k+2)%3;double s=2*sqrt(fmax(0.,1+m[4*k]-m[4*j]-m[4*l]));q[0]=(m[3*l+j]-m[3*j+l])/s;q[1]=q[2]=q[3]=0.;q[k+1]=s/4;q[j+1]=(m[3*j+k]+m[3*k+j])/s;q[l+1]=(m[3*l+k]+m[3*k+l])/s;}unit(q,4,1,1e-8);}
static void qrotation(double*q,double*m){unit(q,4,1,1e-8);double w=q[0],x=q[1],y=q[2],z=q[3];double a[9]={1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w),2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w),2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)};memcpy(m,a,sizeof(a));}
static double canonical(double x){double v=nearbyint(x*16777216.);return v==0.?0.:v/16777216.;}
static int64_t quant(double x){double s=x*16384.;return(int64_t)(s>=0?floor(s+.5):ceil(s-.5));}
static int64_t roundq(int64_t x){int64_t mag=x<0?-x:x;int64_t r=(mag+8192)>>14;return x<0?-r:r;}
/* constants: bind4x4[16], inverse q14[12], head length, max_rate.
 * input: skinq14[12], neck/pelvis/left/right[12], selected R[9], source body[9], prevrelative[9],scale,dt,hasprev,recovering,wasactive.
 * out: relative9,end3,palette12,error,active,write,priority. */
static PyObject*batch(PyObject*self,PyObject*args){(void)self;PyObject*ci,*ii,*oo;Py_buffer cb={0},ib={0},ob={0};if(!PyArg_ParseTuple(args,"OOO",&ci,&ii,&oo))return NULL;if(PyObject_GetBuffer(ci,&cb,PyBUF_SIMPLE)<0)return NULL;if(PyObject_GetBuffer(ii,&ib,PyBUF_SIMPLE)<0){PyBuffer_Release(&cb);return NULL;}if(PyObject_GetBuffer(oo,&ob,PyBUF_WRITABLE)<0){PyBuffer_Release(&cb);PyBuffer_Release(&ib);return NULL;}failed=0;
 if(cb.len!=30*8||ib.len!=56*8||ob.len!=28*8){PyErr_SetString(PyExc_ValueError,"head batch ABI size");failed=1;goto done;}
 const double*c=cb.buf,*i=ib.buf;double*o=ob.buf;double baseline_in[9],baseline[9],body[9],up[3],x[3],back[3];for(int r=0;r<3;r++)for(int k=0;k<3;k++)baseline_in[3*r+k]=i[4*r+k];if(!rotation(baseline_in,baseline))goto done;
 for(int k=0;k<3;k++){up[k]=i[12+k]-i[15+k];x[k]=i[18+k]-i[21+k];}unit(up,3,1,1e-8);double du=dot(x,up,3,1);for(int k=0;k<3;k++)x[k]-=du*up[k];unit(x,3,1,1e-8);cross(x,up,back);for(int r=0;r<3;r++){body[3*r]=x[r];body[3*r+1]=up[r];body[3*r+2]=back[r];}
 int recovering=i[54]!=0;double target[9],rel_target[9],relative[9],rot[9],tmp[9],error=0.;if(recovering)memcpy(target,baseline,sizeof(target));else{mm(i+33,i+24,tmp,1,0);mm(body,tmp,target,0,0);}mm(body,target,rel_target,1,0);
 if(i[53]==0)memcpy(relative,rel_target,sizeof(relative));else{
  double a[4],b[4];quaternion(i+42,a);quaternion(rel_target,b);double cosine=dot(a,b,4,1);if(cosine<0){for(int k=0;k<4;k++)b[k]=-b[k];cosine=-cosine;}double angle=2*acos(fmax(-1.,fmin(1.,cosine)));error=angle;
  double dt=i[52];if(dt<=0)memcpy(relative,i+42,sizeof(relative));else if(angle<=fmin(acos(-1.)/180.,c[29]*dt)){if(recovering){memcpy(relative,rel_target,sizeof(relative));error=0.;}else memcpy(relative,i+42,sizeof(relative));}
  else{double step=fmin(angle*(1-exp(-2*acos(-1.)*3.*dt)),c[29]*dt),half=angle/2.,q[4];for(int k=0;k<4;k++)q[k]=(sin(half-step/2.)*a[k]+sin(step/2.)*b[k])/sin(half);qrotation(q,relative);error=fmax(0.,angle-step);}}
 mm(body,relative,rot,0,0);double rb[3]={rot[2],rot[5],rot[8]};int priority=!recovering&&dot(rb,back,3,1)<=0.;if(priority){memcpy(relative,rel_target,sizeof(relative));memcpy(rot,target,sizeof(rot));error=0.;}int active=!recovering||(i[55]!=0&&error>1e-7);o[24]=error;o[25]=active;o[26]=!recovering||active;o[27]=priority;
 if(recovering&&!active){memcpy(o,rel_target,sizeof(rel_target));goto done;}
 for(int k=0;k<9;k++)relative[k]=canonical(relative[k]);memcpy(o,relative,sizeof(relative));mm(body,relative,rot,0,0);
 double bindy[3]={c[1],c[5],c[9]},bindx[3]={c[0],c[4],c[8]},y[3],end[3],start[3],finish[3],reference[3];mv(rot,bindy,y);unit(y,3,1,1e-8);double scale=i[51];for(int k=0;k<3;k++){end[k]=i[12+k]+scale*c[28]*y[k];o[9+k]=end[k];start[k]=canonical(i[12+k]/scale);finish[k]=canonical(end[k]/scale);}mv(rot,bindx,reference);for(int k=0;k<3;k++)reference[k]=canonical(reference[k]);
 double dir[3];for(int k=0;k<3;k++)dir[k]=finish[k]-start[k];unit(dir,3,0,1e-10);unit(reference,3,0,1e-10);double proj=dot(reference,dir,3,0),xx[3];for(int k=0;k<3;k++)xx[k]=reference[k]-proj*dir[k];if(sqrt(dot(xx,xx,3,0))<=1e-6){double fx[3]={1.,0.,0.},fz[3]={0.,0.,1.};double*f=fabs(dot(fx,dir,3,0))<=fabs(dot(fz,dir,3,0))?fx:fz;proj=dot(f,dir,3,0);for(int k=0;k<3;k++)xx[k]=f[k]-proj*dir[k];}unit(xx,3,0,1e-10);double zz[3];cross(xx,dir,zz);unit(zz,3,0,1e-10);
 int64_t raw[12],inv[12];for(int r=0;r<3;r++){raw[4*r]=quant(xx[r]*scale);raw[4*r+1]=quant(dir[r]*scale);raw[4*r+2]=quant(zz[r]*scale);raw[4*r+3]=quant(start[r]*scale);}for(int k=0;k<12;k++)inv[k]=(int64_t)c[16+k];for(int r=0;r<3;r++)for(int col=0;col<4;col++){int64_t v=0;for(int k=0;k<3;k++)v+=raw[4*r+k]*inv[4*k+col];if(col==3)v+=raw[4*r+3]*16384;int64_t q=roundq(v);if(q< -32768||q>32767){PyErr_SetString(PyExc_ValueError,"composed affine matrix exceeds signed Q1.14 range");failed=1;goto done;}o[12+4*r+col]=(double)q;}
 done:PyBuffer_Release(&cb);PyBuffer_Release(&ib);PyBuffer_Release(&ob);if(failed)return NULL;Py_RETURN_NONE;}
static PyMethodDef methods[]={{"batch",batch,METH_VARARGS,NULL},{NULL,NULL,0,NULL}};static struct PyModuleDef def={PyModuleDef_HEAD_INIT,"_posepriv_head_batch",NULL,-1,methods};PyMODINIT_FUNC PyInit__posepriv_head_batch(void){PyObject*m=PyImport_ImportModule("math"),*b=PyImport_ImportModule("builtins");if(!m||!b)return NULL;fsum_fn=PyObject_GetAttrString(m,"fsum");sum_fn=PyObject_GetAttrString(b,"sum");Py_DECREF(m);Py_DECREF(b);if(!fsum_fn||!sum_fn)return NULL;return PyModule_Create(&def);}
