/* SPDX-License-Identifier: GPL-2.0-only
 * Original PosePriv project implementation of the WorldAvatar final
 * confidence=0 metadata boundary. Receives validated frozen
 * SkeletonJoint3D/BoneSegment3D from WorldAvatar's final output boundary only.
 * No arithmetic, original rig history or raw observation is modified.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <math.h>
#include <structmember.h>
#include <stddef.h>

static PyTypeObject *joint_type,*segment_type;
static PyObject *joint_fields[5],*segment_fields[8],*zero;
static Py_ssize_t joint_offsets[5],segment_offsets[8];

static int bind_offsets(PyTypeObject *type,PyObject **fields,Py_ssize_t *offsets,int count){
    if(type->tp_itemsize!=0){PyErr_SetString(PyExc_TypeError,"fixed frozen slot type required");return -1;}
    for(int i=0;i<count;i++){
        PyObject *descriptor=PyDict_GetItemWithError(type->tp_dict,fields[i]);
        if(!descriptor||Py_TYPE(descriptor)!=&PyMemberDescr_Type||PyDescr_TYPE(descriptor)!=type){
            if(!PyErr_Occurred())PyErr_SetString(PyExc_TypeError,"exact class member descriptor required");
            return -1;
        }
        PyMemberDef *member=((PyMemberDescrObject*)descriptor)->d_member;
        if((member->type!=T_OBJECT&&member->type!=T_OBJECT_EX)||
                member->offset<(Py_ssize_t)sizeof(PyObject)||
                member->offset>type->tp_basicsize-(Py_ssize_t)sizeof(PyObject*)||
                member->offset%(Py_ssize_t)_Alignof(PyObject*)!=0){
            PyErr_SetString(PyExc_TypeError,"object slot offset outside validated fixed instance layout");return -1;
        }
#ifdef Py_RELATIVE_OFFSET
        if(member->flags&Py_RELATIVE_OFFSET){PyErr_SetString(PyExc_TypeError,"absolute object slots required");return -1;}
#endif
        for(int j=0;j<i;j++)if(offsets[j]==member->offset){PyErr_SetString(PyExc_TypeError,"distinct frozen object slots required");return -1;}
        offsets[i]=member->offset;
    }
    return 0;
}

static PyObject *setup(PyObject *self,PyObject *args){
    (void)self;PyObject *joint,*segment;
    if(!PyArg_ParseTuple(args,"OO",&joint,&segment))return NULL;
    if(!PyType_Check(joint)||!PyType_Check(segment)){
        PyErr_SetString(PyExc_TypeError,"exact frozen display classes required");return NULL;
    }
    if(joint_type||segment_type){
        if((PyObject*)joint_type!=joint||(PyObject*)segment_type!=segment){
            PyErr_SetString(PyExc_RuntimeError,"display metadata classes already bound");return NULL;
        }
        Py_RETURN_NONE;
    }
    const char *j[5]={"name","position","confidence","ray_gap","valid"};
    const char *s[8]={"name","parent","child","start","end","length","confidence","valid"};
    for(int i=0;i<5;i++){joint_fields[i]=PyUnicode_InternFromString(j[i]);if(!joint_fields[i])return NULL;}
    for(int i=0;i<8;i++){segment_fields[i]=PyUnicode_InternFromString(s[i]);if(!segment_fields[i])return NULL;}
    if(bind_offsets((PyTypeObject*)joint,joint_fields,joint_offsets,5)<0||
       bind_offsets((PyTypeObject*)segment,segment_fields,segment_offsets,8)<0)return NULL;
    zero=PyFloat_FromDouble(0.);if(!zero)return NULL;
    Py_INCREF(joint);Py_INCREF(segment);joint_type=(PyTypeObject*)joint;segment_type=(PyTypeObject*)segment;
    Py_RETURN_NONE;
}

static PyObject *clone_confidence(PyObject *value,PyTypeObject *type,Py_ssize_t *offsets,int count,int confidence){
    if(Py_TYPE(value)!=type){PyErr_SetString(PyExc_TypeError,"only exact validated frozen display metadata supported");return NULL;}
    PyObject *old_confidence=*(PyObject**)((char*)value+offsets[confidence]);
    /* Reuse immutable positive float0; -0/int0 must still become the original
     * explicit float0 to preserve even the prior field's bit/type semantics. */
    int reuse=PyFloat_CheckExact(old_confidence)&&PyFloat_AS_DOUBLE(old_confidence)==0.&&!signbit(PyFloat_AS_DOUBLE(old_confidence));
    if(reuse){Py_INCREF(value);return value;}
    PyObject *result=type->tp_alloc(type,0);if(!result)return NULL;
    for(int i=0;i<count;i++){
        PyObject *field=i==confidence?zero:*(PyObject**)((char*)value+offsets[i]);
        Py_INCREF(field);
        *(PyObject**)((char*)result+offsets[i])=field;
    }
    return result;
}

static PyObject *batch(PyObject *self,PyObject *args){
    (void)self;PyObject *joints,*segments;
    if(!PyArg_ParseTuple(args,"OO",&joints,&segments))return NULL;
    if(!joint_type||!segment_type){PyErr_SetString(PyExc_RuntimeError,"bind display metadata classes first");return NULL;}
    PyObject *result_joints=PyDict_New(),*result_segments=NULL,*iterator=NULL,*key=NULL;
    if(!result_joints)return NULL;
    iterator=PyObject_GetIter(joints);if(!iterator)goto fail;
    while((key=PyIter_Next(iterator))){
        PyObject *old=PyObject_GetItem(joints,key);if(!old)goto fail;
        PyObject *next=clone_confidence(old,joint_type,joint_offsets,5,2);Py_DECREF(old);if(!next)goto fail;
        int rc=PyDict_SetItem(result_joints,key,next);Py_DECREF(next);Py_CLEAR(key);if(rc<0)goto fail;
    }
    if(PyErr_Occurred())goto fail;
    Py_CLEAR(iterator);
    if(!PyTuple_CheckExact(segments)){PyErr_SetString(PyExc_TypeError,"canonical frozen segment tuple required");goto fail;}
    Py_ssize_t n=PyTuple_GET_SIZE(segments);result_segments=PyTuple_New(n);if(!result_segments)goto fail;
    for(Py_ssize_t i=0;i<n;i++){
        PyObject *next=clone_confidence(PyTuple_GET_ITEM(segments,i),segment_type,segment_offsets,8,6);if(!next)goto fail;
        PyTuple_SET_ITEM(result_segments,i,next);
    }
    PyObject *result=PyTuple_Pack(2,result_joints,result_segments);Py_DECREF(result_joints);Py_DECREF(result_segments);return result;
fail:
    Py_XDECREF(key);Py_XDECREF(iterator);Py_XDECREF(result_joints);Py_XDECREF(result_segments);return NULL;
}

static PyMethodDef methods[]={{"setup",setup,METH_VARARGS,"Cold bind exact public frozen classes."},{"batch",batch,METH_VARARGS,"Clone validated display metadata changing confidence only."},{NULL,NULL,0,NULL}};
static struct PyModuleDef definition={PyModuleDef_HEAD_INIT,"_posepriv_world_metadata",NULL,-1,methods,NULL,NULL,NULL,NULL};
PyMODINIT_FUNC PyInit__posepriv_world_metadata(void){return PyModule_Create(&definition);}
