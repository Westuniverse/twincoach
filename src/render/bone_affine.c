/* SPDX-License-Identifier: GPL-2.0-only */
/* CPU-only port of this project's skeleton_pose.py reference-axis branch.
 * Source/license: PosePriv-Coach skeleton_pose.py, GPL-2.0-only.
 * Existing dependency: CPython public C API (PSF license); no new package.
 * sum3 follows the host interpreter's three-float sum semantics. The 3.12/3.13
 * compensated algorithm was checked against CPython (PSF), not replaced by fsum:
 * https://github.com/python/cpython/blob/v3.12.3/Python/bltinmodule.c
 * https://docs.python.org/3/license.html
 * Compile without fast math/FMA and retain libm pow(x,2.0), as Python x**2 does.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <math.h>
#include <stdint.h>

uint32_t posepriv_bone_affine_abi(void) { return 1; }
uint32_t posepriv_bone_affine_python_abi(void)
{ return ((uint32_t)PY_MAJOR_VERSION << 16) | (uint32_t)PY_MINOR_VERSION; }

static int safe_float(PyObject *value, double *out)
{
    if (!PyFloat_CheckExact(value)) return 0;
    *out = PyFloat_AS_DOUBLE(value);
    /* Unusual magnitudes retain Python's pow overflow/error ordering. */
    return isfinite(*out) && fabs(*out) <= 1.0e100;
}

static int vector(PyObject *value, double out[3])
{
    if (!PyTuple_CheckExact(value) || PyTuple_GET_SIZE(value) != 3) return 0;
    for (unsigned i = 0; i < 3; ++i)
        if (!safe_float(PyTuple_GET_ITEM(value, i), &out[i])) return 0;
    return 1;
}

static double sum3(double a, double b, double c)
{
    double result = 0.0 + a;
#if PY_MAJOR_VERSION == 3 && PY_MINOR_VERSION >= 12
    double correction = 0.0;
    const double remaining[2] = {b, c};
    for (unsigned i = 0; i < 2; ++i) {
        double item = remaining[i];
        double next = result + item;
        if (fabs(result) >= fabs(item)) correction += (result - next) + item;
        else correction += (item - next) + result;
        result = next;
    }
    if (correction != 0.0 && isfinite(correction)) result += correction;
#else
    result += b;
    result += c;
#endif
    return result;
}

static double distance_zero(const double value[3])
{
    double dx = value[0] - 0.0, dy = value[1] - 0.0, dz = value[2] - 0.0;
    return sqrt(sum3(pow(dx, 2.0), pow(dy, 2.0), pow(dz, 2.0)));
}

static int invalid(const char *message)
{ PyErr_SetString(PyExc_ValueError, message); return 0; }

static int normalize(const double value[3], double out[3])
{
    double magnitude = distance_zero(value);
    if (magnitude <= 1.0e-10) return invalid("bone direction must have non-zero length");
    for (unsigned i = 0; i < 3; ++i) out[i] = value[i] / magnitude;
    return 1;
}

static double dot(const double a[3], const double b[3])
{ return sum3(a[0]*b[0], a[1]*b[1], a[2]*b[2]); }

static void project(const double axis[3], const double direction[3], double out[3])
{
    double factor = dot(axis, direction);
    for (unsigned i = 0; i < 3; ++i) out[i] = axis[i] - direction[i]*factor;
}

static int reference_axis(const double direction[3], const double reference[3], double out[3])
{
    double length = distance_zero(reference), unit[3], projection[3];
    if (length <= 1.0e-10) return invalid("bone local X reference must have non-zero length");
    for (unsigned i = 0; i < 3; ++i) unit[i] = reference[i] / length;
    project(unit, direction, projection);
    if (distance_zero(projection) <= 1.0e-6) {
        const double x[3] = {1.0, 0.0, 0.0}, z[3] = {0.0, 0.0, 1.0};
        /* Python min selects X on a tie. */
        const double *fallback = fabs(dot(z, direction)) < fabs(dot(x, direction)) ? z : x;
        project(fallback, direction, projection);
    }
    return normalize(projection, out);
}

static int quantize(double value, long *out)
{
    if (!isfinite(value)) return invalid("bone matrix coefficient must be finite");
    double scaled = value * 16384.0;
    double rounded = scaled >= 0.0 ? floor(scaled + 0.5) : ceil(scaled - 0.5);
    if (rounded < -32768.0 || rounded > 32767.0)
        return invalid("bone matrix coefficient exceeds signed Q1.14 range");
    *out = (long)rounded;
    return 1;
}

static PyObject *build_values(PyObject *self, PyObject *args)
{
    PyObject *start_arg, *end_arg, *length_arg, *scale_arg, *origin_arg, *ref_arg, *local_arg;
    double start[3], end[3], origin[3], reference[3], length, scale;
    (void)self;
    if (!PyArg_ParseTuple(args, "OOOOOOO:build", &start_arg, &end_arg, &length_arg,
                          &scale_arg, &origin_arg, &ref_arg, &local_arg)) return NULL;
    /* New interpreter summation changes require explicit validation first. */
#if PY_MAJOR_VERSION != 3 || PY_MINOR_VERSION < 8 || PY_MINOR_VERSION > 13
    Py_RETURN_NOTIMPLEMENTED;
#endif
    if (!vector(start_arg, start) || !vector(end_arg, end) ||
        !safe_float(length_arg, &length) || !safe_float(scale_arg, &scale) ||
        !vector(origin_arg, origin) || !vector(ref_arg, reference) ||
        (local_arg != Py_None && !PyLong_CheckExact(local_arg))) Py_RETURN_NOTIMPLEMENTED;
    if (scale <= 0.0) {
        invalid("world_to_model_scale must be finite and positive"); return NULL;
    }
    double delta[3], direction[3], local_x[3], cross[3], local_z[3];
    for (unsigned i = 0; i < 3; ++i) delta[i] = end[i] - start[i];
    if (!normalize(delta, direction) || !reference_axis(direction, reference, local_x)) return NULL;
    cross[0] = local_x[1]*direction[2] - local_x[2]*direction[1];
    cross[1] = local_x[2]*direction[0] - local_x[0]*direction[2];
    cross[2] = local_x[0]*direction[1] - local_x[1]*direction[0];
    if (!normalize(cross, local_z)) return NULL;
    double rotation[9] = {local_x[0], direction[0], local_z[0],
                          local_x[1], direction[1], local_z[1],
                          local_x[2], direction[2], local_z[2]};
    const unsigned thickness[6] = {0, 3, 6, 2, 5, 8};
    for (unsigned i = 0; i < 6; ++i) rotation[thickness[i]] *= scale;
    double length_scale = scale;
    if (local_arg != Py_None) {
        int overflow = 0;
        long local = PyLong_AsLongAndOverflow(local_arg, &overflow);
        if (PyErr_Occurred()) return NULL;
        if (overflow || local <= 0 || local > 32767) {
            invalid("local bone length must be positive signed Q1.14"); return NULL;
        }
        double local_length = local / 16384.0;
        length_scale = length * scale / local_length;
    }
    for (unsigned i = 1; i < 9; i += 3) rotation[i] *= length_scale;
    double values[12] = {rotation[0], rotation[1], rotation[2], (start[0]-origin[0])*scale,
                         rotation[3], rotation[4], rotation[5], (start[1]-origin[1])*scale,
                         rotation[6], rotation[7], rotation[8], (start[2]-origin[2])*scale};
    long output[12];
    for (unsigned i = 0; i < 12; ++i) if (!quantize(values[i], &output[i])) return NULL;
    PyObject *result = PyTuple_New(12);
    if (!result) return NULL;
    for (unsigned i = 0; i < 12; ++i) {
        PyObject *value = PyLong_FromLong(output[i]);
        if (!value) { Py_DECREF(result); return NULL; }
        PyTuple_SET_ITEM(result, i, value);
    }
    return result;
}

#include "limb_axis.inc"

static PyMethodDef methods[] = {
    {"limb_axis",limb_axis_values,METH_VARARGS,"Exact history-branch limb axis with interpreter fsum."},
    {"build", build_values, METH_VARARGS, "Strict reference-axis bone matrix; unsupported inputs return NotImplemented."},
    {NULL, NULL, 0, NULL}
};
static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "_posepriv_bone_affine", .m_size = sizeof(LimbState), .m_methods = methods,
    .m_traverse=limb_traverse, .m_clear=limb_clear
};
PyMODINIT_FUNC PyInit__posepriv_bone_affine(void) {
    PyObject *result=PyModule_Create(&module);
    if (!result)return NULL;
    LimbState *state=PyModule_GetState(result);
    PyObject *math=PyImport_ImportModule("math");
    if (!math) {Py_DECREF(result);return NULL;}
    state->fsum=PyObject_GetAttrString(math,"fsum");Py_DECREF(math);
    if (!state->fsum) {Py_DECREF(result);return NULL;}
    return result;
}
