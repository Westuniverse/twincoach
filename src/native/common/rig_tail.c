/* SPDX-License-Identifier: GPL-2.0-only
 * One complete13-bone numeric tail + trusted frame-record assembly boundary.
 * Reference: preview_rigid_avatar.apply tail, skeleton_pose and bone_affine.c.
 * Existing CPython C API only; no NumPy C API or new dependency.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>
#include <math.h>
#include <string.h>
#include "rig_tail_bone_math.inc"
#include "limb_axis_math.inc"
#include "rig_tail_palm_axis.inc"

uint32_t posepriv_rig_tail_abi(void) { return 1; }
uint32_t posepriv_rig_tail_axis_batch_abi(void) {
#ifdef POSEPRIV_RIG_AXIS_BATCH
    return 1;
#else
    return 0;
#endif
}
uint32_t posepriv_rig_tail_python_abi(void) { return (PY_MAJOR_VERSION << 16) | PY_MINOR_VERSION; }

enum {NAME,PARENT,CHILD,START,END,LENGTH,CONFIDENCE,VALID,POSITION,RAY_GAP,VALUES,INDEX,MATRIX,NATTR};
static PyObject *attrs[NATTR];
static const char *attrnames[NATTR]={"name","parent","child","start","end","length","confidence","valid","position","ray_gap","values","index","matrix"};
static PyObject *new_record(PyObject *cls,const int *ids,PyObject *const *values,int count){
    PyTypeObject *type=(PyTypeObject*)cls;PyObject *obj=type->tp_alloc(type,0);if(!obj)return NULL;
    for(int i=0;i<count;i++)if(PyObject_GenericSetAttr(obj,attrs[ids[i]],values[i])<0){Py_DECREF(obj);return NULL;}
    return obj;
}
static PyObject *vector_tuple(const double a[3]){return Py_BuildValue("(ddd)",a[0],a[1],a[2]);}
static double canonical(double x){
    double a=x*16777216.,r=round(a);if(fabs(a-r)==.5)r=2.*round(a/2.);
    if(r==0.)r=0.;return r/16777216.;
}
static int read3(PyObject *obj,double out[3]){
    if(PyTuple_Check(obj)){
        if(PyTuple_GET_SIZE(obj)!=3)return invalid("native tail vector needs3 values");
        for(int i=0;i<3;i++){out[i]=PyFloat_AsDouble(PyTuple_GET_ITEM(obj,i));if(PyErr_Occurred())return 0;}
        return 1;
    }
    Py_buffer b;if(PyObject_GetBuffer(obj,&b,PyBUF_STRIDES|PyBUF_FORMAT)<0)return 0;
    int ok=b.ndim==1&&b.itemsize==8&&b.format&&strcmp(b.format,"d")==0&&b.shape[0]==3;
    if(ok)for(int i=0;i<3;i++)memcpy(out+i,(char*)b.buf+i*b.strides[0],8);
    PyBuffer_Release(&b);return ok?1:invalid("native tail vector buffer differs");
}
static int read9(PyObject *obj,double out[9]){
    Py_buffer b;if(PyObject_GetBuffer(obj,&b,PyBUF_STRIDES|PyBUF_FORMAT)<0)return 0;
    int ok=b.ndim==2&&b.itemsize==8&&b.format&&strcmp(b.format,"d")==0&&b.shape[0]==3&&b.shape[1]==3;
    if(ok)for(int i=0;i<3;i++)for(int j=0;j<3;j++)memcpy(out+3*i+j,(char*)b.buf+i*b.strides[0]+j*b.strides[1],8);
    PyBuffer_Release(&b);return ok?1:invalid("native tail rotation buffer differs");
}
static int fsum3(PyObject *fn,double a,double b,double c,double *out){
    PyObject *v=Py_BuildValue("(ddd)",a,b,c);if(!v)return 0;
    PyObject *r=PyObject_CallOneArg(fn,v);Py_DECREF(v);if(!r)return 0;
    *out=PyFloat_AsDouble(r);Py_DECREF(r);return !PyErr_Occurred();
}
static int rig_unit(PyObject *fsum,const double input[3],double out[3]){
    double squared;if(!fsum3(fsum,input[0]*input[0],input[1]*input[1],input[2]*input[2],&squared))return 0;
    double norm=sqrt(squared);if(!isfinite(norm)||norm<1e-7)return invalid("degenerate rig direction");
    for(int i=0;i<3;i++)out[i]=input[i]/norm;return 1;
}
static PyObject *segment(PyObject *cls,PyObject *name,PyObject *parent,PyObject *child,const double a[3],const double b[3],double length,double confidence,int valid){
    PyObject *start=vector_tuple(a),*end=vector_tuple(b),*l=PyFloat_FromDouble(length),*c=PyFloat_FromDouble(confidence);
    if(!start||!end||!l||!c){Py_XDECREF(start);Py_XDECREF(end);Py_XDECREF(l);Py_XDECREF(c);return NULL;}
    const int ids[]={NAME,PARENT,CHILD,START,END,LENGTH,CONFIDENCE,VALID};
    PyObject *values[]={name,parent,child,start,end,l,c,valid?Py_True:Py_False};
    PyObject *result=new_record(cls,ids,values,8);Py_DECREF(start);Py_DECREF(end);Py_DECREF(l);Py_DECREF(c);return result;
}
static PyObject *joint(PyObject *cls,PyObject *name,const double point[3],double confidence){
    PyObject *position=vector_tuple(point),*c=PyFloat_FromDouble(confidence),*gap=PyFloat_FromDouble(INFINITY);
    if(!position||!c||!gap){Py_XDECREF(position);Py_XDECREF(c);Py_XDECREF(gap);return NULL;}
    const int ids[]={NAME,POSITION,CONFIDENCE,RAY_GAP,VALID};PyObject *values[]={name,position,c,gap,Py_True};
    PyObject *result=new_record(cls,ids,values,5);Py_DECREF(position);Py_DECREF(c);Py_DECREF(gap);return result;
}
static PyObject *canonical_array(PyObject *array,const double input[3]){
    double v[3];for(int i=0;i<3;i++)v[i]=canonical(input[i]);PyObject *values=vector_tuple(v);if(!values)return NULL;
    PyObject *result=PyObject_CallOneArg(array,values);Py_DECREF(values);return result;
}
static PyObject *canonical_tuple(const double input[3]){
    double v[3];for(int i=0;i<3;i++)v[i]=canonical(input[i]);return vector_tuple(v);
}
static int bone_matrix(const double start[3],const double end[3],double length,double scale,const double reference[3],long local,const int16_t inverse[12],long output[12]){
    double delta[3],direction[3],x[3],cross[3],z[3];for(int i=0;i<3;i++)delta[i]=end[i]-start[i];
    if(!normalize(delta,direction)||!reference_axis(direction,reference,x))return 0;
    cross[0]=x[1]*direction[2]-x[2]*direction[1];cross[1]=x[2]*direction[0]-x[0]*direction[2];cross[2]=x[0]*direction[1]-x[1]*direction[0];
    if(!normalize(cross,z))return 0;
    double rotation[9]={x[0],direction[0],z[0],x[1],direction[1],z[1],x[2],direction[2],z[2]};const unsigned thickness[6]={0,3,6,2,5,8};
    for(int i=0;i<6;i++)rotation[thickness[i]]*=scale;
    double ls=scale;if(local)ls=length*scale/(local/16384.);
    for(int i=1;i<9;i+=3)rotation[i]*=ls;
    double values[12]={rotation[0],rotation[1],rotation[2],(start[0]-0.)*scale,rotation[3],rotation[4],rotation[5],(start[1]-0.)*scale,rotation[6],rotation[7],rotation[8],(start[2]-0.)*scale};long first[12];
    for(int i=0;i<12;i++)if(!quantize(values[i],first+i))return 0;
    for(int r=0;r<3;r++)for(int c=0;c<4;c++){
        int64_t sum=(int64_t)first[4*r]*inverse[c]+(int64_t)first[4*r+1]*inverse[4+c]+(int64_t)first[4*r+2]*inverse[8+c];if(c==3)sum+=(int64_t)first[4*r+3]*16384;
        int64_t mag=sum<0?-sum:sum;int64_t rounded=(mag+8192)/16384;if(sum<0)rounded=-rounded;
        if(rounded<-32768||rounded>32767)return invalid("composed affine matrix exceeds signed Q1.14 range");output[4*r+c]=(long)rounded;
    }
    return 1;
}
static PyObject *finish(PyObject *self,PyObject *args){
    (void)self;PyObject *rig,*render,*observed,*poses,*rotation,*headref,*inferred,*params;double scale,dt;
    if(!PyArg_ParseTuple(args,"OOOOdOdOOO:finish",&rig,&render,&observed,&poses,&scale,&rotation,&dt,&headref,&inferred,&params))return NULL;
    /* params prepared once: metadata,classes,array,fsum,hand,limb,bindX,inverse,jnames. */
    if(!PyTuple_CheckExact(params)||PyTuple_GET_SIZE(params)!=12){invalid("native tail setup differs");return NULL;}
    PyObject *metadata=PyTuple_GET_ITEM(params,0),*classes=PyTuple_GET_ITEM(params,1),*array=PyTuple_GET_ITEM(params,2),*fsum=PyTuple_GET_ITEM(params,3),*hand=PyTuple_GET_ITEM(params,4),*limb=PyTuple_GET_ITEM(params,5),*bindXobj=PyTuple_GET_ITEM(params,6),*inverseobj=PyTuple_GET_ITEM(params,7),*jnames=PyTuple_GET_ITEM(params,8);
#ifdef POSEPRIV_RIG_AXIS_BATCH
    int batch_axes=PyTuple_GET_ITEM(params,9)==Py_True;
#else
    const int batch_axes=0;
#endif
    PyObject *hypot_fn=PyTuple_GET_ITEM(params,10);
    LimbState limbstate={fsum};
    PyObject *segmentcls=PyTuple_GET_ITEM(classes,0),*jointcls=PyTuple_GET_ITEM(classes,1),*affinecls=PyTuple_GET_ITEM(classes,2),*writecls=PyTuple_GET_ITEM(classes,3);
    PyObject *bonehistory=PyObject_GetAttrString(rig,"_bone_history"),*handhistory=PyObject_GetAttrString(rig,"_hand_history"),*observedjoints=PyObject_GetAttrString(observed,"joints"),*oldwrites=PyObject_GetAttrString(render,"matrix_writes");
    PyObject *writes=oldwrites?PySequence_List(oldwrites):NULL,*joints=PyDict_New(),*segments=NULL,*boneupdates=PyDict_New(),*handupdates=PyDict_New(),*answer=NULL;Py_XDECREF(oldwrites);
    if(!bonehistory||!handhistory||!observedjoints||!writes||!joints||!boneupdates||!handupdates)goto done;
    if(!PyBytes_CheckExact(bindXobj)||PyBytes_GET_SIZE(bindXobj)!=13*3*8||!PyBytes_CheckExact(inverseobj)||PyBytes_GET_SIZE(inverseobj)!=13*12*2){invalid("native tail fixedbind layout differs");goto done;}
    double bindX[39],rot[9];int16_t inverse[13][12];memcpy(bindX,PyBytes_AS_STRING(bindXobj),sizeof bindX);memcpy(inverse,PyBytes_AS_STRING(inverseobj),sizeof inverse);if(!read9(rotation,rot))goto done;
    double joint_positions[32][3],joint_confidence[32];int joint_count=(int)PyTuple_GET_SIZE(jnames);if(joint_count>32){invalid("native tail joint count differs");goto done;}
    for(int slot=0;slot<13;slot++){
        PyObject *meta=PyTuple_GET_ITEM(metadata,slot),*name=PyTuple_GET_ITEM(meta,0),*parent=PyTuple_GET_ITEM(meta,1),*child=PyTuple_GET_ITEM(meta,2);long local=PyLong_AsLong(PyTuple_GET_ITEM(meta,3));int pi=(int)PyLong_AsLong(PyTuple_GET_ITEM(meta,4)),ci=(int)PyLong_AsLong(PyTuple_GET_ITEM(meta,5)),kind=(int)PyLong_AsLong(PyTuple_GET_ITEM(meta,6));
        PyObject *pose=PyDict_GetItemWithError(poses,name);if(!pose){if(!PyErr_Occurred())invalid("native tail pose missing");goto done;}
        double start[3],end[3],cs[3],ce[3],delta[3],squared,length,ref[3],axis[3];
        if(!read3(PyTuple_GET_ITEM(pose,0),start)||!read3(PyTuple_GET_ITEM(pose,1),end))goto done;
        for(int i=0;i<3;i++){delta[i]=end[i]-start[i];cs[i]=canonical(start[i]/scale);ce[i]=canonical(end[i]/scale);}
        if(!fsum3(fsum,delta[0]*delta[0],delta[1]*delta[1],delta[2]*delta[2],&squared))goto done;length=sqrt(squared);
        int is_inferred=PySequence_Contains(inferred,name);if(is_inferred<0)goto done;
        PyObject *seg=segment(segmentcls,name,parent,child,cs,ce,length/scale,is_inferred?0.:1.,1);if(!seg)goto done;
        PyObject *key=PyLong_FromLong(slot),*axisobj=NULL;
        if(!key){Py_DECREF(seg);goto done;}
        if(kind==2){
            PyObject *previous=PyDict_GetItemWithError(handhistory,key);
            if(batch_axes){
                double old_axis[3],old_direction[3];
                if(previous&&(!read3(PyTuple_GET_ITEM(previous,0),old_axis)||!read3(PyTuple_GET_ITEM(previous,1),old_direction))){Py_DECREF(key);Py_DECREF(seg);goto done;}
                if(!batch_palm_axis(hypot_fn,cs,ce,slot==11,previous?old_axis:NULL,previous?old_direction:NULL,12.*dt,axis)){Py_DECREF(key);Py_DECREF(seg);goto done;}
            }else{
                PyObject *kwargs=Py_BuildValue("{sO,sO,sd}","previous_axis",previous?PyTuple_GET_ITEM(previous,0):Py_None,"previous_direction",previous?PyTuple_GET_ITEM(previous,1):Py_None,"max_roll_radians",12.*dt),*callargs=PyTuple_Pack(1,seg);
                if(kwargs&&callargs)axisobj=PyObject_Call(hand,callargs,kwargs);Py_XDECREF(kwargs);Py_XDECREF(callargs);
                if(!axisobj||!read3(axisobj,axis)){Py_XDECREF(axisobj);Py_DECREF(key);Py_DECREF(seg);goto done;}
            }
            double direction[3],dd[3];for(int i=0;i<3;i++)dd[i]=ce[i]-cs[i];
            if(!rig_unit(fsum,dd,direction)){Py_XDECREF(axisobj);Py_DECREF(key);Py_DECREF(seg);goto done;}
            PyObject *a=canonical_tuple(axis),*d=canonical_tuple(direction),*history=a&&d?PyTuple_Pack(2,a,d):NULL;Py_XDECREF(a);Py_XDECREF(d);
            if(!history||PyDict_SetItem(handupdates,key,history)<0){Py_XDECREF(history);Py_XDECREF(axisobj);Py_DECREF(key);Py_DECREF(seg);goto done;}Py_DECREF(history);
        }else if(kind==1){
            PyObject *previous=PyDict_GetItemWithError(bonehistory,key),*newhistory=NULL;
            if(batch_axes&&previous){
                double oldlocal[3],olddirection[3],local[3],direction[3];
                if(!read3(PyTuple_GET_ITEM(previous,0),oldlocal)||!read3(PyTuple_GET_ITEM(previous,1),olddirection)||
                   !limb_axis_history(&limbstate,cs,ce,(const double (*)[3])rot,bindX+3*slot,oldlocal,olddirection,dt,axis,local,direction)){Py_DECREF(key);Py_DECREF(seg);goto done;}
                PyObject *a=canonical_array(array,local),*d=canonical_array(array,direction);
                newhistory=a&&d?PyTuple_Pack(2,a,d):NULL;Py_XDECREF(a);Py_XDECREF(d);
            }else{
                PyObject *callargs=Py_BuildValue("(iOOd)",slot,seg,rotation,dt),*value=callargs?PyObject_CallObject(limb,callargs):NULL;Py_XDECREF(callargs);
                if(!value){Py_DECREF(key);Py_DECREF(seg);goto done;}
                axisobj=Py_NewRef(PyTuple_GET_ITEM(value,0));PyObject *history=PyTuple_GET_ITEM(value,1);newhistory=PyTuple_New(2);
                if(!newhistory||!read3(axisobj,axis)){Py_XDECREF(newhistory);Py_DECREF(value);Py_DECREF(axisobj);Py_DECREF(key);Py_DECREF(seg);goto done;}
                for(int k=0;k<2;k++){
                    double v[3];if(!read3(PyTuple_GET_ITEM(history,k),v)){Py_DECREF(newhistory);newhistory=NULL;break;}
                    PyObject *a=canonical_array(array,v);if(!a){Py_DECREF(newhistory);newhistory=NULL;break;}PyTuple_SET_ITEM(newhistory,k,a);
                }
                Py_DECREF(value);
            }
            if(!newhistory||PyDict_SetItem(boneupdates,key,newhistory)<0){Py_XDECREF(newhistory);Py_XDECREF(axisobj);Py_DECREF(key);Py_DECREF(seg);goto done;}Py_DECREF(newhistory);
        }else{
            if(slot==2){if(!read3(headref,ref)){Py_DECREF(key);Py_DECREF(seg);goto done;}}
            else for(int i=0;i<3;i++)if(!fsum3(fsum,rot[3*i]*bindX[3*slot],rot[3*i+1]*bindX[3*slot+1],rot[3*i+2]*bindX[3*slot+2],ref+i)){Py_DECREF(key);Py_DECREF(seg);goto done;}
            double dd[3],direction[3];for(int i=0;i<3;i++)dd[i]=ce[i]-cs[i];
            if(!normalize(dd,direction)||!reference_axis(direction,ref,axis)){Py_DECREF(key);Py_DECREF(seg);goto done;}
        }
        Py_XDECREF(axisobj);Py_DECREF(seg);
        for(int i=0;i<3;i++)ref[i]=canonical(axis[i]);long values[12];
        if(!bone_matrix(cs,ce,length/scale,scale,ref,local,inverse[slot],values)){Py_DECREF(key);goto done;}
        PyObject *ints=PyTuple_New(12);if(!ints){Py_DECREF(key);goto done;}for(int i=0;i<12;i++){PyObject*v=PyLong_FromLong(values[i]);if(!v){Py_DECREF(ints);ints=NULL;break;}PyTuple_SET_ITEM(ints,i,v);}if(!ints){Py_DECREF(key);goto done;}
        const int aid[]={VALUES};PyObject *av[]={ints};PyObject *matrix=new_record(affinecls,aid,av,1);Py_DECREF(ints);if(!matrix){Py_DECREF(key);goto done;}
        const int wid[]={INDEX,MATRIX};PyObject *wv[]={key,matrix};PyObject *write=new_record(writecls,wid,wv,2);Py_DECREF(matrix);Py_DECREF(key);if(!write)goto done;PyList_SetItem(writes,slot,write);
        double confidence=0.;
        if(!is_inferred){
            PyObject *p=PyObject_GetItem(observedjoints,parent),*c=PyObject_GetItem(observedjoints,child);
            if(!p)PyErr_Clear();if(!c)PyErr_Clear();int found=0;double valuesconf[2];
            if(p){PyObject *q=PyObject_GetAttrString(p,"confidence");if(!q){Py_XDECREF(p);Py_XDECREF(c);goto done;}valuesconf[found++]=PyFloat_AsDouble(q);Py_DECREF(q);}
            if(c){PyObject *q=PyObject_GetAttrString(c,"confidence");if(!q){Py_XDECREF(p);Py_XDECREF(c);goto done;}valuesconf[found++]=PyFloat_AsDouble(q);Py_DECREF(q);}
            Py_XDECREF(p);Py_XDECREF(c);confidence=found?(found==2?fmin(valuesconf[0],valuesconf[1]):valuesconf[0]):0.;
        }
        memcpy(joint_positions[pi],start,24);memcpy(joint_positions[ci],end,24);joint_confidence[pi]=joint_confidence[ci]=confidence;
    }
    for(int i=0;i<joint_count;i++){PyObject *name=PyTuple_GET_ITEM(jnames,i),*j=joint(jointcls,name,joint_positions[i],joint_confidence[i]);if(!j||PyDict_SetItem(joints,name,j)<0){Py_XDECREF(j);goto done;}Py_DECREF(j);}
    segments=PyTuple_New(13);if(!segments)goto done;
    for(int i=0;i<13;i++){
        PyObject *m=PyTuple_GET_ITEM(metadata,i);int p=(int)PyLong_AsLong(PyTuple_GET_ITEM(m,4)),q=(int)PyLong_AsLong(PyTuple_GET_ITEM(m,5));double d[3];for(int k=0;k<3;k++)d[k]=joint_positions[p][k]-joint_positions[q][k];double length=sqrt(sum3(pow(d[0],2.),pow(d[1],2.),pow(d[2],2.)));int valid=length>=1e-6;double confidence=valid?fmin(joint_confidence[p],joint_confidence[q]):0.;
        PyObject *s=segment(segmentcls,PyTuple_GET_ITEM(m,0),PyTuple_GET_ITEM(m,1),PyTuple_GET_ITEM(m,2),joint_positions[p],joint_positions[q],length,confidence,valid);if(!s)goto done;PyTuple_SET_ITEM(segments,i,s);
    }
    {PyObject *tuple=PyList_AsTuple(writes);if(tuple){answer=PyTuple_Pack(5,tuple,joints,segments,boneupdates,handupdates);Py_DECREF(tuple);}}
done:
    Py_XDECREF(bonehistory);Py_XDECREF(handhistory);Py_XDECREF(observedjoints);Py_XDECREF(writes);Py_XDECREF(joints);Py_XDECREF(segments);Py_XDECREF(boneupdates);Py_XDECREF(handupdates);return answer;
}
static PyMethodDef methods[]={{"finish",finish,METH_VARARGS,"One complete13bone numeric tail and final-record boundary."},{NULL,NULL,0,NULL}};
static struct PyModuleDef module={PyModuleDef_HEAD_INIT,.m_name="_posepriv_rig_tail",.m_size=-1,.m_methods=methods};
PyMODINIT_FUNC PyInit__posepriv_rig_tail(void){for(int i=0;i<NATTR;i++){attrs[i]=PyUnicode_InternFromString(attrnames[i]);if(!attrs[i])return NULL;}return PyModule_Create(&module);}
