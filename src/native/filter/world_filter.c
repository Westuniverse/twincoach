/* SPDX-License-Identifier: GPL-2.0-only
 * Native leaf of this project's WorldPointStabilizer.apply. No other library
 * or algorithm is incorporated. Canonical state remains Python tuple/list.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <math.h>

static PyObject *unsupported, *sum_fn, *names[5];
static const double pi_value=3.141592653589793;

static int number(PyObject *v,double *out){
    if(!PyFloat_CheckExact(v)&&!PyLong_CheckExact(v)){
        PyErr_SetString(unsupported,"native world filter supports internal float/int values only");return -1;
    }
    *out=PyFloat_AsDouble(v);return PyErr_Occurred()?-1:0;
}

static PyObject *triple(const double v[3]){
    return Py_BuildValue("(ddd)",v[0],v[1],v[2]);
}

static PyObject *filter(PyObject *self,PyObject *args){
    (void)self;
    PyObject *owner,*observation;
    if(!PyArg_ParseTuple(args,"OO",&owner,&observation))return NULL;
    PyObject *source=NULL,*states=NULL,*points=NULL,*key=NULL,*timestamp=NULL;
    PyObject *view=NULL,*width=NULL,*height=NULL,*rotation=NULL,*old=NULL,*delta=NULL;
    PyObject *saved=NULL,*result=NULL,*state=NULL,*value=NULL;
    double minimum,beta,derivative;
    source=PyObject_GetAttrString(observation,"source");
    states=PyObject_GetAttrString(owner,"states");
    points=PyObject_GetAttrString(observation,"world_landmarks");
    if(!source||!states||!points)goto fail;
    if(!PyDict_CheckExact(states)||!PyTuple_CheckExact(points)||(PyTuple_GET_SIZE(points)!=0&&PyTuple_GET_SIZE(points)!=33)){
        PyErr_SetString(unsupported,"native world filter requires canonical states and zero/33 points");goto fail;
    }
    view=PyObject_GetAttrString(source,"view");width=PyObject_GetAttrString(source,"width");
    height=PyObject_GetAttrString(source,"height");rotation=PyObject_GetAttrString(source,"rotation_degrees");
    timestamp=PyObject_GetAttrString(source,"timestamp_ns");
    if(!view||!width||!height||!rotation||!timestamp)goto fail;
    key=PyTuple_Pack(4,view,width,height,rotation);if(!key)goto fail;
    old=PyDict_GetItemWithError(states,key);if(!old&&PyErr_Occurred())goto fail;
    if(PyTuple_GET_SIZE(points)==0){
        if(old&&PyDict_DelItem(states,key)<0)goto fail;
        Py_INCREF(Py_None);result=Py_None;goto success;
    }
    value=PyObject_GetAttrString(owner,"min_cutoff");if(!value||number(value,&minimum)<0)goto fail;Py_CLEAR(value);
    value=PyObject_GetAttrString(owner,"beta");if(!value||number(value,&beta)<0)goto fail;Py_CLEAR(value);
    value=PyObject_GetAttrString(owner,"derivative_cutoff");if(!value||number(value,&derivative)<0)goto fail;Py_CLEAR(value);
    if(derivative==0.){PyErr_SetString(unsupported,"zero derivative cutoff uses original owner path");goto fail;}
    int continuing=0;double dt=0.,ad=0.;PyObject *prior_list=NULL;
    if(old){
        if(!PyTuple_CheckExact(old)||PyTuple_GET_SIZE(old)!=2||!PyList_CheckExact(PyTuple_GET_ITEM(old,1))||PyList_GET_SIZE(PyTuple_GET_ITEM(old,1))!=33){
            PyErr_SetString(unsupported,"native world filter requires canonical prior state");goto fail;
        }
        delta=PyNumber_Subtract(timestamp,PyTuple_GET_ITEM(old,0));if(!delta)goto fail;
        if(!PyLong_CheckExact(delta)){
            PyErr_SetString(unsupported,"native world filter requires integer source ns");goto fail;
        }
        int overflow=0;long long n=PyLong_AsLongLongAndOverflow(delta,&overflow);
        if(PyErr_Occurred())goto fail;
        continuing=!overflow&&n>0&&n<=200000000;
        if(continuing){dt=(double)n*1e-9;ad=dt/(dt+1./((2.*pi_value)*derivative));}
        prior_list=PyTuple_GET_ITEM(old,1);
    }
    saved=PyList_New(33);result=PyTuple_New(33);if(!saved||!result)goto fail;
    for(Py_ssize_t i=0;i<33;i++){
        PyObject *p=PyTuple_GET_ITEM(points,i),*raw=NULL,*filtered=NULL,*velocity=NULL;
        PyObject *record=NULL,*out=NULL,*prior=continuing?PyList_GET_ITEM(prior_list,i):NULL;
        double raw_v[3],v[3]={0.,0.,0.},f[3],confidence[2];
        raw=PyTuple_New(3);if(!raw)goto item_fail;
        for(int c=0;c<5;c++){
            PyObject *x=PyObject_GetItem(p,names[c]);if(!x)goto item_fail;
            double n;if(number(x,&n)<0){Py_DECREF(x);goto item_fail;}
            if(c<3){raw_v[c]=n;PyTuple_SET_ITEM(raw,c,x);}else{confidence[c-3]=n;Py_DECREF(x);}
        }
        int valid=(confidence[0]<confidence[1]?confidence[0]:confidence[1])>=.5;
        int prior_valid=0;
        if(prior){
            if(!PyTuple_CheckExact(prior)||PyTuple_GET_SIZE(prior)!=4){
                PyErr_SetString(unsupported,"native world filter requires canonical point state");goto item_fail;
            }
            prior_valid=PyObject_IsTrue(PyTuple_GET_ITEM(prior,3));if(prior_valid<0)goto item_fail;
        }
        if(valid&&prior&&prior_valid){
            for(int c=0;c<3;c++){
                PyObject *r=PyTuple_GET_ITEM(prior,0),*d=PyTuple_GET_ITEM(prior,2);
                if(!PyTuple_CheckExact(r)||PyTuple_GET_SIZE(r)!=3||!PyTuple_CheckExact(d)||PyTuple_GET_SIZE(d)!=3){
                    PyErr_SetString(unsupported,"native world filter requires canonical vectors");goto item_fail;
                }
                double old_raw,old_velocity;
                if(number(PyTuple_GET_ITEM(r,c),&old_raw)<0||number(PyTuple_GET_ITEM(d,c),&old_velocity)<0)goto item_fail;
                v[c]=old_velocity+ad*((raw_v[c]-old_raw)/dt-old_velocity);
            }
            /* Use the interpreter's unchanged sum implementation, starting at
             * integer0: Python3.12 compensates float sum, Python3.10 does not.
             * This small builtin is C as well and preserves both exact paths. */
            double squares[3]={v[0]*v[0],v[1]*v[1],v[2]*v[2]};
            PyObject *square_tuple=triple(squares);if(!square_tuple)goto item_fail;
            PyObject *total=PyObject_CallOneArg(sum_fn,square_tuple);Py_DECREF(square_tuple);if(!total)goto item_fail;
            double sum_value=PyFloat_AsDouble(total);Py_DECREF(total);if(PyErr_Occurred())goto item_fail;
            double cutoff=minimum+beta*sqrt(sum_value);
            if(cutoff==0.){PyErr_SetString(unsupported,"zero adaptive cutoff uses original owner path");goto item_fail;}
            double alpha=dt/(dt+1./((2.*pi_value)*cutoff));
            PyObject *prior_filtered=PyTuple_GET_ITEM(prior,1);
            if(!PyTuple_CheckExact(prior_filtered)||PyTuple_GET_SIZE(prior_filtered)!=3){
                PyErr_SetString(unsupported,"native world filter requires canonical filtered vector");goto item_fail;
            }
            for(int c=0;c<3;c++){
                double old_filtered;if(number(PyTuple_GET_ITEM(prior_filtered,c),&old_filtered)<0)goto item_fail;
                f[c]=old_filtered+alpha*(raw_v[c]-old_filtered);
            }
            filtered=triple(f);velocity=triple(v);
        }else{filtered=raw;Py_INCREF(filtered);velocity=triple(v);}
        if(!filtered||!velocity)goto item_fail;
        record=PyTuple_Pack(4,raw,filtered,velocity,valid?Py_True:Py_False);if(!record)goto item_fail;
        out=PyDict_New();if(!out||PyDict_Update(out,p)<0)goto item_fail;
        for(int c=0;c<3;c++)if(PyDict_SetItem(out,names[c],PyTuple_GET_ITEM(filtered,c))<0)goto item_fail;
        PyList_SET_ITEM(saved,i,record);record=NULL;
        PyTuple_SET_ITEM(result,i,out);out=NULL;
        Py_DECREF(raw);Py_DECREF(filtered);Py_DECREF(velocity);
        continue;
item_fail:
        Py_XDECREF(raw);Py_XDECREF(filtered);Py_XDECREF(velocity);Py_XDECREF(record);Py_XDECREF(out);goto fail;
    }
    state=PyTuple_Pack(2,timestamp,saved);if(!state||PyDict_SetItem(states,key,state)<0)goto fail;
success:
    Py_XDECREF(source);Py_XDECREF(states);Py_XDECREF(points);Py_XDECREF(key);Py_XDECREF(timestamp);
    Py_XDECREF(view);Py_XDECREF(width);Py_XDECREF(height);Py_XDECREF(rotation);Py_XDECREF(delta);
    Py_XDECREF(saved);Py_XDECREF(state);Py_XDECREF(value);
    return result;
fail:
    Py_XDECREF(result);result=NULL;goto success;
}

static PyMethodDef methods[]={{"filter",filter,METH_VARARGS,"Apply one33point display filter using canonical owner state."},{NULL,NULL,0,NULL}};
static struct PyModuleDef definition={PyModuleDef_HEAD_INIT,"_posepriv_world_filter",NULL,-1,methods,NULL,NULL,NULL,NULL};
PyMODINIT_FUNC PyInit__posepriv_world_filter(void){
    PyObject *module=PyModule_Create(&definition);if(!module)return NULL;
    unsupported=PyErr_NewException("_posepriv_world_filter.UnsupportedInput",PyExc_TypeError,NULL);
    if(!unsupported||PyModule_AddObject(module,"UnsupportedInput",unsupported)<0){Py_XDECREF(unsupported);Py_DECREF(module);return NULL;}
    Py_INCREF(unsupported);
    sum_fn=PyDict_GetItemString(PyEval_GetBuiltins(),"sum");if(!sum_fn){Py_DECREF(module);return NULL;}Py_INCREF(sum_fn);
    const char *strings[5]={"x","y","z","visibility","presence"};
    for(int i=0;i<5;i++){names[i]=PyUnicode_InternFromString(strings[i]);if(!names[i]){Py_DECREF(module);return NULL;}}
    return module;
}
