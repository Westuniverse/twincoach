/* SPDX-License-Identifier: GPL-2.0-only
 * Optional 16-slot float32 cofactor/inverse prep from field_preview_gles.py.
 * No NumPy C API / new dependency: existing CPython API (PSF), standard libm.
 * Keep final rotation@cof in NumPy to retain target matmul rounding semantics.
 * Compile -fno-fast-math -ffp-contract=off; every original ufunc rounds float32.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <math.h>
#include <float.h>
#include <stdint.h>
#ifdef __FAST_MATH__
#error "GLES normal prep requires strict float32, not fast math"
#endif
_Static_assert(sizeof(float)==4 && FLT_RADIX==2 && FLT_MANT_DIG==24 &&
               FLT_MAX_EXP==128 && FLT_MIN_EXP==-125 && FLT_EVAL_METHOD==0,
               "GLES normal prep requires IEEE binary32 without excess precision");

uint32_t posepriv_gles_prep_abi(void) { return 1; }
uint32_t posepriv_gles_prep_python_abi(void)
{ return ((uint32_t)PY_MAJOR_VERSION<<16) | (uint32_t)PY_MINOR_VERSION; }

static PyObject *inverse_values(PyObject *self,PyObject *rows)
{
    (void)self;
    long q[16][12];
    if (!PyTuple_CheckExact(rows) || PyTuple_GET_SIZE(rows)!=16)Py_RETURN_NOTIMPLEMENTED;
    for (int bone=0;bone<16;++bone) {
        PyObject *row=PyTuple_GET_ITEM(rows,bone);
        if (!PyTuple_CheckExact(row) || PyTuple_GET_SIZE(row)!=12)Py_RETURN_NOTIMPLEMENTED;
        for (int i=0;i<12;++i) {
            PyObject *item=PyTuple_GET_ITEM(row,i);
            if (!PyLong_CheckExact(item))Py_RETURN_NOTIMPLEMENTED;
            int overflow=0;
            q[bone][i]=PyLong_AsLongAndOverflow(item,&overflow);
            if (PyErr_Occurred())return NULL;
            if (overflow || q[bone][i]<-32768 || q[bone][i]>32767)Py_RETURN_NOTIMPLEMENTED;
        }
    }
    float packed[16*9];
    float *dst=packed;
    for (int bone=0;bone<16;++bone) {
        float m[3][3],cof[3][3];
        for (int r=0;r<3;++r)for (int col=0;col<3;++col)m[r][col]=(float)q[bone][4*r+col]/16384.0f;
        for (int col=0;col<3;++col) {
            int left=(col+1)%3,right=(col+2)%3;
            cof[0][col]=m[1][left]*m[2][right]-m[2][left]*m[1][right];
            cof[1][col]=m[2][left]*m[0][right]-m[0][left]*m[2][right];
            cof[2][col]=m[0][left]*m[1][right]-m[1][left]*m[0][right];
        }
        float p0=m[0][0]*cof[0][0],p1=m[1][0]*cof[1][0],p2=m[2][0]*cof[2][0];
        float det=(p0+p1)+p2;
        int usable=fabsf(det)>1.0e-8f;
        for (int r=0;r<3;++r)for (int col=0;col<3;++col)
            dst[bone*9+r*3+col]=usable?cof[r][col]/det:(r==col?1.0f:0.0f);
    }
    return PyBytes_FromStringAndSize((const char*)packed,sizeof(packed));
}
static PyObject *abi(PyObject *self,PyObject *unused)
{(void)self;(void)unused;return Py_BuildValue("(iii)",1,PY_MAJOR_VERSION,PY_MINOR_VERSION);}
static PyMethodDef methods[]={
    {"inverse_values",inverse_values,METH_O,"Strict Q14 rows to float32 inverse-transpose bases, before unchanged NumPy rotation."},
    {"abi",abi,METH_NOARGS,"Prototype ABI and CPython major/minor."},
    {NULL,NULL,0,NULL}
};
static struct PyModuleDef module={PyModuleDef_HEAD_INIT,.m_name="_posepriv_gles_prep",.m_size=-1,.m_methods=methods};
PyMODINIT_FUNC PyInit__posepriv_gles_prep(void){return PyModule_Create(&module);}
