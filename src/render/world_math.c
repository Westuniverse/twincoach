/* SPDX-License-Identifier: GPL-2.0-only
 * Opt-in Full33 world/head leaf. Existing CPython public buffer API (PSF), NumPy,
 * and actual math.fsum on the same three rounded scalar products. No FMA.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <math.h>
#include <string.h>

typedef struct {
 PyObject *fsum,*array,*array_type,*f64_type,*rotation,*unit,*matmul;
 unsigned long long rotations,units,matmuls,matrices,vectors,rf,uf,mf;
} State;

static int array_read(State*s,PyObject*o,int dimensions,double*out) {
 if((PyObject*)Py_TYPE(o)!=s->array_type)return 0;
 Py_buffer b;
 if(PyObject_GetBuffer(o,&b,PyBUF_FORMAT|PyBUF_STRIDES)<0){PyErr_Clear();return 0;}
 int ok=b.ndim==dimensions&&b.itemsize==8&&b.format&&strcmp(b.format,"d")==0;
 if(ok)for(int i=0;i<dimensions;i++)if(b.shape[i]!=3)ok=0;
 if(ok)for(int i=0;i<3;i++)for(int j=0;j<(dimensions==2?3:1);j++)
  memcpy(out+i*(dimensions==2?3:1)+j,(char*)b.buf+i*b.strides[0]+(dimensions==2?j*b.strides[1]:0),8);
 PyBuffer_Release(&b);return ok;
}

static int flat_tuple(State*s,PyObject*o,int count,double*out) {
 if(!PyTuple_CheckExact(o)||PyTuple_GET_SIZE(o)!=count)return 0;
 for(int i=0;i<count;i++) {
  PyObject*v=PyTuple_GET_ITEM(o,i);
  if(!PyFloat_CheckExact(v)&&!PyLong_CheckExact(v)&&!PyBool_Check(v)&&(PyObject*)Py_TYPE(v)!=s->f64_type)return 0;
 }
 for(int i=0;i<count;i++) {
  out[i]=PyFloat_AsDouble(PyTuple_GET_ITEM(o,i));
  if(PyErr_Occurred()){PyErr_Clear();return 0;}
 }
 return 1;
}

static int dot(State*s,const double*a,const double*b,double*out) {
 /* Products round independently to binary64, then the original CPython fsum
  * handles cancellation/overflow/nonfinite exactly as the Python oracle. */
 PyObject*values=Py_BuildValue("(ddd)",a[0]*b[0],a[1]*b[1],a[2]*b[2]);
 if(!values)return 0;
 PyObject*value=PyObject_CallOneArg(s->fsum,values);Py_DECREF(values);
 if(!value)return 0;
 *out=PyFloat_AsDouble(value);Py_DECREF(value);return !PyErr_Occurred();
}

static int normalize(State*s,double*v,double length) {
 (void)s;
 if(!isfinite(length)||length<1e-8){PyErr_SetString(PyExc_ValueError,"degenerate head orientation");return 0;}
 for(int i=0;i<3;i++)v[i]/=length;
 return 1;
}

static PyObject*make_array(State*s,const double*values,int rows,int columns) {
 PyObject*outer=PyTuple_New(rows);if(!outer)return NULL;
 for(int i=0;i<rows;i++) {
  PyObject*item;
  if(columns==1)item=PyFloat_FromDouble(values[i]);
  else {
   item=PyTuple_New(columns);
   if(item)for(int j=0;j<columns;j++) {
    PyObject*number=PyFloat_FromDouble(values[i*columns+j]);
    if(!number){Py_DECREF(item);item=NULL;break;}
    PyTuple_SET_ITEM(item,j,number);
   }
  }
  if(!item){Py_DECREF(outer);return NULL;}PyTuple_SET_ITEM(outer,i,item);
 }
 PyObject*result=PyObject_CallOneArg(s->array,outer);Py_DECREF(outer);return result;
}

static PyObject*unit(PyObject*self,PyObject*arg) {
 State*s=PyModule_GetState(self);double value[3],square;
 if(!s->unit){PyErr_SetString(PyExc_RuntimeError,"setup required");return NULL;}
 if(!array_read(s,arg,1,value)&&!flat_tuple(s,arg,3,value)){s->uf++;return PyObject_CallOneArg(s->unit,arg);}
 if(!dot(s,value,value,&square)||!normalize(s,value,sqrt(square)))return NULL;
 s->units++;return make_array(s,value,3,1);
}

static PyObject*rotation(PyObject*self,PyObject*arg) {
 State*s=PyModule_GetState(self);double value[9],axes[3][3],lengths[3],sum;
 if(!s->rotation){PyErr_SetString(PyExc_RuntimeError,"setup required");return NULL;}
 if(!array_read(s,arg,2,value)&&!flat_tuple(s,arg,9,value)){s->rf++;return PyObject_CallOneArg(s->rotation,arg);}
 for(int i=0;i<9;i++)if(!isfinite(value[i])){PyErr_SetString(PyExc_ValueError,"nonfinite head rotation");return NULL;}
 for(int col=0;col<3;col++) {
  for(int row=0;row<3;row++)axes[col][row]=value[row*3+col];
  if(!dot(s,axes[col],axes[col],&sum))return NULL;
  lengths[col]=sqrt(sum);
 }
 double minimum=lengths[0],maximum=lengths[0];
 for(int k=1;k<3;k++){if(lengths[k]<minimum)minimum=lengths[k];if(lengths[k]>maximum)maximum=lengths[k];}
 if(minimum<1e-8||maximum-minimum>.002*maximum){PyErr_SetString(PyExc_ValueError,"head rotation must have uniform positive scale");return NULL;}
 /* Original _unit recomputes the identical norm. Reuse its exact scalar value. */
 for(int k=0;k<3;k++)if(!normalize(s,axes[k],lengths[k]))return NULL;
 double xy,xz,yz;
 if(!dot(s,axes[0],axes[1],&xy)||!dot(s,axes[0],axes[2],&xz)||!dot(s,axes[1],axes[2],&yz))return NULL;
 if(fabs(xy)>.002||fabs(xz)>.002||fabs(yz)>.002){PyErr_SetString(PyExc_ValueError,"head rotation must be orthogonal");return NULL;}
 for(int k=0;k<3;k++)axes[1][k]=axes[1][k]-xy*axes[0][k];
 if(!dot(s,axes[1],axes[1],&sum)||!normalize(s,axes[1],sqrt(sum)))return NULL;
 double back[3]={axes[0][1]*axes[1][2]-axes[0][2]*axes[1][1],
                 axes[0][2]*axes[1][0]-axes[0][0]*axes[1][2],
                 axes[0][0]*axes[1][1]-axes[0][1]*axes[1][0]};
 if(!dot(s,back,axes[2],&sum))return NULL;
 if(sum<.99){PyErr_SetString(PyExc_ValueError,"reflected head rotation");return NULL;}
 for(int row=0;row<3;row++){value[row*3]=axes[0][row];value[row*3+1]=axes[1][row];value[row*3+2]=back[row];}
 s->rotations++;return make_array(s,value,3,3);
}

static PyObject*matmul(PyObject*self,PyObject*args) {
 State*s=PyModule_GetState(self);PyObject*a,*b;double left[9],right[9],out[9];int vector;
 if(!PyArg_ParseTuple(args,"OO:matmul",&a,&b))return NULL;
 if(!s->matmul){PyErr_SetString(PyExc_RuntimeError,"setup required");return NULL;}
 vector=array_read(s,b,1,right);
 if(!array_read(s,a,2,left)||(!vector&&!array_read(s,b,2,right))){s->mf++;return PyObject_CallObject(s->matmul,args);}
 for(int row=0;row<3;row++)for(int col=0;col<(vector?1:3);col++) {
  double column[3]={right[col],right[(vector?1:3)+col],right[(vector?2:6)+col]};
  if(!dot(s,left+row*3,column,out+row*(vector?1:3)+col))return NULL;
 }
 s->matmuls++;if(vector)s->vectors++;else s->matrices++;
 return make_array(s,out,3,vector?1:3);
}

static PyObject*setup(PyObject*self,PyObject*args) {
 State*s=PyModule_GetState(self);PyObject*r,*u,*m;
 if(!PyArg_ParseTuple(args,"OOO:setup",&r,&u,&m))return NULL;
 if(!PyCallable_Check(r)||!PyCallable_Check(u)||!PyCallable_Check(m)){PyErr_SetString(PyExc_TypeError,"original fallbacks required");return NULL;}
 Py_XSETREF(s->rotation,Py_NewRef(r));Py_XSETREF(s->unit,Py_NewRef(u));Py_XSETREF(s->matmul,Py_NewRef(m));Py_RETURN_NONE;
}
static PyObject*stats(PyObject*self,PyObject*unused) {
 (void)unused;State*s=PyModule_GetState(self);
 return Py_BuildValue("{sK,sK,sK,sK,sK,sK,sK,sK}","rotation",s->rotations,"unit",s->units,"matmul",s->matmuls,
                     "matrix3",s->matrices,"matvec3",s->vectors,
                     "rotation_fallback",s->rf,"unit_fallback",s->uf,"matmul_fallback",s->mf);
}
static int traverse(PyObject*self,visitproc visit,void*arg) {
 State*s=PyModule_GetState(self);Py_VISIT(s->fsum);Py_VISIT(s->array);Py_VISIT(s->array_type);Py_VISIT(s->f64_type);
 Py_VISIT(s->rotation);Py_VISIT(s->unit);Py_VISIT(s->matmul);return 0;
}
static int clear(PyObject*self) {
 State*s=PyModule_GetState(self);Py_CLEAR(s->fsum);Py_CLEAR(s->array);Py_CLEAR(s->array_type);Py_CLEAR(s->f64_type);
 Py_CLEAR(s->rotation);Py_CLEAR(s->unit);Py_CLEAR(s->matmul);return 0;
}
static PyMethodDef methods[]={{"rotation",rotation,METH_O,NULL},{"unit",unit,METH_O,NULL},{"matmul",matmul,METH_VARARGS,NULL},
 {"setup",setup,METH_VARARGS,NULL},{"stats",stats,METH_NOARGS,NULL},{NULL,NULL,0,NULL}};
static struct PyModuleDef module={PyModuleDef_HEAD_INIT,.m_name="_posepriv_world_math",.m_size=sizeof(State),
 .m_methods=methods,.m_traverse=traverse,.m_clear=clear};
PyMODINIT_FUNC PyInit__posepriv_world_math(void) {
 PyObject*r=PyModule_Create(&module);if(!r)return NULL;State*s=PyModule_GetState(r);
 PyObject*m=PyImport_ImportModule("math");if(m){s->fsum=PyObject_GetAttrString(m,"fsum");Py_DECREF(m);}
 PyObject*n=PyImport_ImportModule("numpy");if(n){s->array=PyObject_GetAttrString(n,"array");s->array_type=PyObject_GetAttrString(n,"ndarray");s->f64_type=PyObject_GetAttrString(n,"float64");Py_DECREF(n);}
 if(!s->fsum||!s->array||!s->array_type||!s->f64_type){Py_DECREF(r);return NULL;}return r;
}
