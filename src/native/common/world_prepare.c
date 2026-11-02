/* SPDX-License-Identifier: GPL-2.0-only
 * Internal world-plan materialization, existing validated 0/33 observations.
 * Original rig/head/body histories and their arithmetic stay in Python.
 */
#define PY_SSIZE_T_CLEAN
#define NPY_NO_DEPRECATED_API NPY_1_7_API_VERSION
#include <Python.h>
#include <structmember.h>
#include <numpy/arrayobject.h>
#include <numpy/arrayscalars.h>
#include <math.h>

enum {NJ=20,NS=32};
typedef struct {PyTypeObject *type;int count;Py_ssize_t offsets[9];} Layout;
typedef struct {
    Layout joint,segment,plan,observation;
    PyObject *names[NJ],*keys[5],*infinity,*zero,*one,*origin_tuple;
    PyObject *writes,*requests,*framing_offset,*fsum,*sum;
    int links,indices[16],parent[NS],child[NS];
    PyObject *linkfields[NS][3];
    double origin[3],torso_length;
} Context;
static const int world_indices[17]={0,2,5,7,8,11,12,13,14,15,16,23,24,25,26,27,28};
static const char *joint_names[NJ]={"nose","left_eye","right_eye","left_ear","right_ear",
    "left_shoulder","right_shoulder","left_elbow","right_elbow","left_wrist","right_wrist",
    "left_hip","right_hip","left_knee","right_knee","left_ankle","right_ankle","neck","pelvis","chest"};

static int bind_layout(Layout *out,PyObject *type,const char **names,int count){
    if(!PyType_Check(type)||((PyTypeObject*)type)->tp_itemsize!=0){PyErr_SetString(PyExc_TypeError,"fixed frozen slot type required");return -1;}
    out->type=(PyTypeObject*)Py_NewRef(type);out->count=count;
    for(int i=0;i<count;i++){
        PyObject *d=PyDict_GetItemString(out->type->tp_dict,names[i]);
        if(!d||Py_TYPE(d)!=&PyMemberDescr_Type||PyDescr_TYPE(d)!=out->type){PyErr_SetString(PyExc_TypeError,"exact frozen object slot required");return -1;}
        PyMemberDef *m=((PyMemberDescrObject*)d)->d_member;
        if((m->type!=T_OBJECT&&m->type!=T_OBJECT_EX)||m->offset<(Py_ssize_t)sizeof(PyObject)||
           m->offset>out->type->tp_basicsize-(Py_ssize_t)sizeof(PyObject*)){PyErr_SetString(PyExc_TypeError,"invalid frozen slot layout");return -1;}
        out->offsets[i]=m->offset;
    }
    return 0;
}
static PyObject *field(PyObject *v,Layout *layout,int i){return *(PyObject**)((char*)v+layout->offsets[i]);}
static PyObject *make_slots(Layout *layout,PyObject **values){
    PyObject *out=layout->type->tp_alloc(layout->type,0);if(!out)return NULL;
    for(int i=0;i<layout->count;i++)*(PyObject**)((char*)out+layout->offsets[i])=Py_NewRef(values[i]);
    return out;
}
static void destroy(PyObject *capsule){
    Context *c=PyCapsule_GetPointer(capsule,"posepriv.world_plan_context");if(!c){PyErr_Clear();return;}
    Py_XDECREF(c->joint.type);Py_XDECREF(c->segment.type);Py_XDECREF(c->plan.type);Py_XDECREF(c->observation.type);
    for(int i=0;i<NJ;i++)Py_XDECREF(c->names[i]);
    for(int i=0;i<5;i++)Py_XDECREF(c->keys[i]);
    for(int i=0;i<c->links;i++)for(int j=0;j<3;j++)Py_XDECREF(c->linkfields[i][j]);
    Py_XDECREF(c->infinity);Py_XDECREF(c->zero);Py_XDECREF(c->one);Py_XDECREF(c->origin_tuple);
    Py_XDECREF(c->writes);Py_XDECREF(c->requests);Py_XDECREF(c->framing_offset);Py_XDECREF(c->fsum);Py_XDECREF(c->sum);
    PyMem_Free(c);
}
static int joint_index(PyObject *name){
    const char *s=PyUnicode_AsUTF8(name);if(!s)return -2;
    for(int i=0;i<NJ;i++)if(strcmp(s,joint_names[i])==0)return i;
    return -1;
}
static PyObject *create(PyObject *self,PyObject *args){
    (void)self;PyObject *joint,*segment,*plan,*observation,*links,*indices,*origin,*torso,*writes,*requests,*framing;
    if(!PyArg_ParseTuple(args,"OOOOOOOOOOO",&joint,&segment,&plan,&observation,&links,&indices,&origin,&torso,&writes,&requests,&framing))return NULL;
    Context *c=PyMem_Calloc(1,sizeof(Context));if(!c)return PyErr_NoMemory();
    PyObject *capsule=PyCapsule_New(c,"posepriv.world_plan_context",destroy);if(!capsule){PyMem_Free(c);return NULL;}
    const char *jf[]={"name","position","confidence","ray_gap","valid"};
    const char *sf[]={"name","parent","child","start","end","length","confidence","valid"};
    const char *pf[]={"source","joints","segments","matrix_writes","matrix_valid","vertex_requests","model_origin","model_scale","pose_preview"};
    const char *of[]={"source","world_landmarks","normalized_landmarks"};
    if(bind_layout(&c->joint,joint,jf,5)<0||bind_layout(&c->segment,segment,sf,8)<0||
       bind_layout(&c->plan,plan,pf,9)<0||bind_layout(&c->observation,observation,of,3)<0)goto fail;
    for(int i=0;i<NJ;i++){c->names[i]=PyUnicode_InternFromString(joint_names[i]);if(!c->names[i])goto fail;}
    const char *keys[]={"x","y","z","presence","visibility"};
    for(int i=0;i<5;i++){c->keys[i]=PyUnicode_InternFromString(keys[i]);if(!c->keys[i])goto fail;}
    c->infinity=PyFloat_FromDouble(INFINITY);c->zero=PyFloat_FromDouble(0.);c->one=PyFloat_FromDouble(1.);
    c->origin_tuple=Py_BuildValue("(ddd)",0.,0.,0.);c->writes=Py_NewRef(writes);c->requests=Py_NewRef(requests);
    c->torso_length=PyFloat_AsDouble(torso);
    for(int i=0;i<3;i++){PyObject *v=PySequence_GetItem(origin,i);if(!v)goto fail;c->origin[i]=PyFloat_AsDouble(v);Py_DECREF(v);}
    if(PyErr_Occurred())goto fail;
    PyObject *math=PyImport_ImportModule("math");if(!math)goto fail;c->fsum=PyObject_GetAttrString(math,"fsum");Py_DECREF(math);if(!c->fsum)goto fail;
    c->sum=Py_NewRef(PyDict_GetItemString(PyEval_GetBuiltins(),"sum"));
    if(framing!=Py_None){c->framing_offset=PyObject_GetAttrString(framing,"offset");if(!c->framing_offset)goto fail;}
    Py_ssize_t n=PySequence_Size(links);if(n<0)goto fail;
    if(n>NS){PyErr_SetString(PyExc_ValueError,"at most32 fixed rig links supported");goto fail;}
    c->links=(int)n;
    for(int i=0;i<c->links;i++){
        PyObject *link=PySequence_GetItem(links,i);if(!link)goto fail;
        const char *lf[]={"name","parent","child"};
        for(int k=0;k<3;k++)c->linkfields[i][k]=PyObject_GetAttrString(link,lf[k]);
        Py_DECREF(link);
        if(PyErr_Occurred())goto fail;
        c->parent[i]=joint_index(c->linkfields[i][1]);c->child[i]=joint_index(c->linkfields[i][2]);
        if(PyErr_Occurred())goto fail;
    }
    if(PySequence_Size(indices)!=13){PyErr_SetString(PyExc_ValueError,"13 fixed topology slots required");goto fail;}
    for(int i=0;i<13;i++){PyObject *v=PySequence_GetItem(indices,i);if(!v)goto fail;long k=PyLong_AsLong(v);Py_DECREF(v);if(k<0||k>=c->links||PyErr_Occurred()){if(!PyErr_Occurred())PyErr_SetString(PyExc_ValueError,"topology outside fixed links");goto fail;}c->indices[i]=(int)k;}
    return capsule;
fail:Py_DECREF(capsule);return NULL;
}
static PyObject *position(double *v,int scaled){
    PyObject *p=PyTuple_New(3);if(!p)return NULL;
    for(int i=0;i<3;i++){
        PyObject *x;
        if(scaled){x=PyArrayScalar_New(Double);if(x)PyArrayScalar_VAL(x,Double)=v[i];}
        else x=PyFloat_FromDouble(v[i]);
        if(!x){Py_DECREF(p);return NULL;}PyTuple_SET_ITEM(p,i,x);
    }
    return p;
}
static PyObject *prepare(PyObject *self,PyObject *args){
    (void)self;PyObject *capsule,*observation;
    if(!PyArg_ParseTuple(args,"OO",&capsule,&observation))return NULL;
    Context *c=PyCapsule_GetPointer(capsule,"posepriv.world_plan_context");if(!c)return NULL;
    if(Py_TYPE(observation)!=c->observation.type){PyErr_SetString(PyExc_TypeError,"exact validated WorldDisplayObservation required");return NULL;}
    PyObject *world=field(observation,&c->observation,1),*source=field(observation,&c->observation,0);
    int count=PyTuple_GET_SIZE(world)?NJ:0;
    double xyz[NJ][3],quality[NJ];int valid[NJ],scaled=0;
    PyObject *confidence[NJ]={0},*positions[NJ]={0},*joints=NULL,*segments=NULL,*matrix_valid=NULL,*proxy=NULL,*output=NULL;
    if(count){
        for(int i=0;i<17;i++){
            PyObject *point=PyTuple_GET_ITEM(world,world_indices[i]);
            double values[5];PyObject *q[2]={NULL,NULL};
            for(int k=0;k<5;k++){
                PyObject *value=PyObject_GetItem(point,c->keys[k]);if(!value){Py_XDECREF(q[0]);Py_XDECREF(q[1]);goto fail;}
                values[k]=PyFloat_AsDouble(value);if(k>=3)q[k-3]=value;else Py_DECREF(value);
                if(PyErr_Occurred()){Py_XDECREF(q[0]);Py_XDECREF(q[1]);goto fail;}
            }
            xyz[i][0]=values[0];xyz[i][1]=-values[1];xyz[i][2]=values[2];
            int pick=values[4]<values[3];confidence[i]=Py_NewRef(q[pick]);quality[i]=values[pick+3];
            Py_DECREF(q[0]);Py_DECREF(q[1]);valid[i]=values[3]>=.5;
        }
        const int parents[3][2]={{5,6},{11,12},{17,18}};
        for(int k=0;k<3;k++){
            int i=17+k,a=parents[k][0],b=parents[k][1];
            for(int j=0;j<3;j++)xyz[i][j]=k<2?(xyz[a][j]+xyz[b][j])*.5:xyz[a][j]+(xyz[b][j]-xyz[a][j])*.30;
            for(int j=0;j<3;j++)if(!isfinite(xyz[i][j])){PyErr_SetString(PyExc_ValueError,"skeleton joint position must be finite");goto fail;}
            int pick=quality[b]<quality[a]?b:a;confidence[i]=Py_NewRef(confidence[pick]);quality[i]=quality[pick];valid[i]=valid[a]&&valid[b];
        }
        if(valid[17]&&valid[18]){
            double v[3];for(int j=0;j<3;j++)v[j]=xyz[17][j]-xyz[18][j];
            PyObject *squares=Py_BuildValue("(ddd)",v[0]*v[0],v[1]*v[1],v[2]*v[2]);if(!squares)goto fail;
            PyObject *sum=PyObject_CallOneArg(c->fsum,squares);Py_DECREF(squares);if(!sum)goto fail;
            double length=sqrt(PyFloat_AS_DOUBLE(sum));Py_DECREF(sum);
            if(length>1e-7){
                double offset[3]={0.,0.,0.};
                if(c->framing_offset){
                    PyObject *value=PyObject_CallOneArg(c->framing_offset,observation);if(!value)goto fail;
                    for(int j=0;j<3;j++){PyObject *x=PySequence_GetItem(value,j);if(!x){Py_DECREF(value);goto fail;}offset[j]=PyFloat_AsDouble(x);Py_DECREF(x);}
                    Py_DECREF(value);if(PyErr_Occurred())goto fail;
                }
                double factor=c->torso_length/length,origin[3],root[3];
                for(int j=0;j<3;j++){origin[j]=xyz[18][j];root[j]=c->origin[j]+offset[j];}
                for(int i=0;i<NJ;i++)for(int j=0;j<3;j++){
                    xyz[i][j]=root[j]+(xyz[i][j]-origin[j])*factor;
                    if(!isfinite(xyz[i][j])){PyErr_SetString(PyExc_ValueError,"skeleton joint position must be finite");goto fail;}
                }
                scaled=1;
            }
        }
    }
    joints=PyDict_New();segments=PyTuple_New(c->links);matrix_valid=PyTuple_New(16);
    if(!joints||!segments||!matrix_valid)goto fail;
    for(int i=0;i<count;i++){
        positions[i]=position(xyz[i],scaled);if(!positions[i])goto fail;
        PyObject *values[]={c->names[i],positions[i],confidence[i],c->infinity,valid[i]?Py_True:Py_False};
        PyObject *joint=make_slots(&c->joint,values);if(!joint)goto fail;
        int rc=PyDict_SetItem(joints,c->names[i],joint);Py_DECREF(joint);if(rc<0)goto fail;
    }
    int segment_valid[NS];
    for(int i=0;i<c->links;i++){
        int a=c->parent[i],b=c->child[i];int present=count&&a>=0&&b>=0;
        PyObject *start=present?positions[a]:c->origin_tuple,*end=present?positions[b]:c->origin_tuple;
        double length=0.;int good=0;
        if(present){
            /* NumPy scalar subtraction/power/add preserves the existing
             * _distance numeric route (including Python-version sum behavior).
             * C merges materialization, not the published scalar math contract. */
            PyObject *squares=PyTuple_New(3),*two=PyLong_FromLong(2);if(!squares||!two){Py_XDECREF(squares);Py_XDECREF(two);goto fail;}
            for(int k=0;k<3;k++){
                PyObject *d=PyNumber_Subtract(PyTuple_GET_ITEM(start,k),PyTuple_GET_ITEM(end,k));
                PyObject *p=d?PyNumber_Power(d,two,Py_None):NULL;Py_XDECREF(d);if(!p){Py_DECREF(squares);Py_DECREF(two);goto fail;}PyTuple_SET_ITEM(squares,k,p);
            }
            Py_DECREF(two);
            /* Exact interpreter's original builtins.sum, including the
             * builtin-float path when torso normalization is unavailable. */
            PyObject *sum=PyObject_CallOneArg(c->sum,squares);if(!sum){Py_DECREF(squares);goto fail;}
            Py_DECREF(squares);length=sqrt(PyFloat_AsDouble(sum));Py_DECREF(sum);if(PyErr_Occurred())goto fail;
            good=valid[a]&&valid[b]&&length>=1e-6;
        }
        segment_valid[i]=good;
        PyObject *length_obj=PyFloat_FromDouble(length);if(!length_obj)goto fail;
        PyObject *q=good?(quality[b]<quality[a]?confidence[b]:confidence[a]):c->zero;
        PyObject *values[]={c->linkfields[i][0],c->linkfields[i][1],c->linkfields[i][2],start,end,length_obj,q,good?Py_True:Py_False};
        PyObject *segment=make_slots(&c->segment,values);Py_DECREF(length_obj);if(!segment)goto fail;PyTuple_SET_ITEM(segments,i,segment);
    }
    for(int i=0;i<16;i++)PyTuple_SET_ITEM(matrix_valid,i,Py_NewRef(i<13&&segment_valid[c->indices[i]]?Py_True:Py_False));
    proxy=PyDictProxy_New(joints);if(!proxy)goto fail;
    PyObject *values[]={source,proxy,segments,c->writes,matrix_valid,c->requests,c->origin_tuple,c->one,Py_True};
    output=make_slots(&c->plan,values);
fail:
    for(int i=0;i<NJ;i++){Py_XDECREF(confidence[i]);Py_XDECREF(positions[i]);}
    Py_XDECREF(joints);Py_XDECREF(segments);Py_XDECREF(matrix_valid);Py_XDECREF(proxy);return output;
}
static PyMethodDef methods[]={{"create",create,METH_VARARGS,"Cold-bind one fixed world-rig plan context."},{"prepare",prepare,METH_VARARGS,"Validated world observation to immutable initial rig plan."},{NULL,NULL,0,NULL}};
static struct PyModuleDef definition={PyModuleDef_HEAD_INIT,"_posepriv_world_prepare",NULL,-1,methods,NULL,NULL,NULL,NULL};
PyMODINIT_FUNC PyInit__posepriv_world_prepare(void){import_array();return PyModule_Create(&definition);}
