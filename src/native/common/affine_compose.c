/* SPDX-License-Identifier: GPL-2.0-only */
/* CPU-only exact Q1.14 composition; no MMIO, DMA or floating point. */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>
#include <string.h>

uint32_t posepriv_affine_compose_abi(void) { return 1; }
uint32_t posepriv_affine_python_abi(void)
{ return ((uint32_t)PY_MAJOR_VERSION << 16) | (uint32_t)PY_MINOR_VERSION; }

uint32_t posepriv_affine_compose_v1(const int16_t *left, const int16_t *right,
                                  int16_t *output)
{
    int16_t temporary[12];
    if (!left || !right || !output) return 0;
    for (unsigned row = 0; row < 3; ++row) {
        for (unsigned column = 0; column < 4; ++column) {
            int64_t sum = (int64_t)left[row*4] * right[column]
                        + (int64_t)left[row*4+1] * right[4+column]
                        + (int64_t)left[row*4+2] * right[8+column];
            if (column == 3) sum += (int64_t)left[row*4+3] * INT64_C(16384);
            int64_t magnitude = sum < 0 ? -sum : sum;
            int64_t rounded = (magnitude + INT64_C(8192)) / INT64_C(16384);
            if (sum < 0) rounded = -rounded;
            if (rounded < INT16_MIN || rounded > INT16_MAX) return 0;
            temporary[row*4+column] = (int16_t)rounded;
        }
    }
    memcpy(output, temporary, sizeof temporary);
    return 1;
}

static PyObject *compose_values(PyObject *self, PyObject *args)
{
    PyObject *a, *b;
    int16_t left[12], right[12], output[12];
    (void)self;
    if (!PyArg_ParseTuple(args, "OO:compose", &a, &b)) return NULL;
    if (!PyTuple_CheckExact(a) || !PyTuple_CheckExact(b) ||
        PyTuple_Size(a) != 12 || PyTuple_Size(b) != 12) Py_RETURN_NOTIMPLEMENTED;
    for (unsigned i = 0; i < 12; ++i) {
        PyObject *av = PyTuple_GetItem(a, i), *bv = PyTuple_GetItem(b, i);
        if (!PyLong_CheckExact(av) || !PyLong_CheckExact(bv)) Py_RETURN_NOTIMPLEMENTED;
        long al = PyLong_AsLong(av);
        if (PyErr_Occurred()) return NULL;
        long bl = PyLong_AsLong(bv);
        if (PyErr_Occurred()) return NULL;
        if (al < INT16_MIN || al > INT16_MAX || bl < INT16_MIN || bl > INT16_MAX) {
            PyErr_SetString(PyExc_ValueError, "native affine input exceeds signed 16 bit");
            return NULL;
        }
        left[i] = (int16_t)al; right[i] = (int16_t)bl;
    }
    if (!posepriv_affine_compose_v1(left, right, output)) {
        PyErr_SetString(PyExc_ValueError, "composed affine matrix exceeds signed Q1.14 range");
        return NULL;
    }
    PyObject *result = PyTuple_New(12);
    if (!result) return NULL;
    for (unsigned i = 0; i < 12; ++i) {
        PyObject *value = PyLong_FromLong(output[i]);
        if (!value) { Py_DECREF(result); return NULL; }
        PyTuple_SET_ITEM(result, i, value);
    }
    return result;
}

static PyMethodDef methods[] = {
    {"compose", compose_values, METH_VARARGS, "Exact signed Q1.14 affine tuple composition."},
    {NULL, NULL, 0, NULL}
};
static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "_posepriv_affine", .m_size = -1, .m_methods = methods
};
PyMODINIT_FUNC PyInit__posepriv_affine(void) { return PyModule_Create(&module); }
