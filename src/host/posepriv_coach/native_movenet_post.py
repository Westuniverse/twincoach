"""Explicit fixed-graph native raw-head post; calibrated on the target Builtin.

No imports of production worker modules, no model/dependency installation, and
no device access. Each return is an independently owned float32 point array.
"""
import ctypes as c
from pathlib import Path
import numpy as np

ROLES=('center','heatmap','regression','offset')
POST_ROLES=dict(center='center_logits',heatmap='keypoint_heatmap_logits',
    regression='keypoint_regression_yx',offset='keypoint_offsets_yx')
CHANNELS=dict(center=1,heatmap=17,regression=34,offset=34)
FIX_POINTS=dict(center=1,heatmap=2,regression=1,offset=3)
OPS=('LOGISTIC LOGISTIC MUL RESHAPE ARG_MAX FLOOR_DIV CAST RESHAPE MUL SUB CAST RESHAPE PACK '
     'GATHER_ND RESHAPE UNPACK ADD ADD SUB MUL SUB MUL ADD SQRT ADD DIV RESHAPE ARG_MAX '
     'FLOOR_DIV CAST RESHAPE MUL SUB CAST RESHAPE PACK GATHER_ND RESHAPE CONCATENATION '
     'GATHER_ND RESHAPE GATHER_ND UNPACK ADD ADD PACK MUL CONCATENATION').split()


class NativeRawPost:
    def __init__(self,library,model,post_mapping,metadata,cnn_mapping,contract,Interpreter,OpResolverType):
        self.library_path=Path(library).resolve(strict=True)
        if not isinstance(metadata,dict)or not isinstance(cnn_mapping,dict):raise ValueError('Raw metadata and CNN mapping must be dictionaries')
        if metadata.get('backend')!='vart'or metadata.get('dtype')!='int8'or metadata.get('layout')!='NHWC':
            raise ValueError('Actual VART raw int8 NHWC required')
        rows=metadata.get('heads')
        if not isinstance(rows,list)or len(rows)!=4 or any(not isinstance(d,dict)or d.get('role')not in ROLES for d in rows):
            raise ValueError('Exactly four named head dictionaries required')
        heads={d['role']:d for d in rows}
        if set(heads)!=set(ROLES):raise ValueError('Exactly four unique roles required')
        if not isinstance(cnn_mapping.get('heads'),dict)or set(cnn_mapping['heads'])!=set(ROLES):raise ValueError('Exactly four CNN mapping roles required')
        self.factors={}
        for role in ROLES:
            detail=heads[role];fix=detail.get('fix_point')
            expected=cnn_mapping['heads'][role]
            if not isinstance(expected,dict)or type(fix)is not int or type(expected.get('fix_point'))is not int or fix!=FIX_POINTS[role]or fix!=expected['fix_point']:
                raise ValueError('Unsupported exact fix point')
            if not isinstance(expected.get('name'),str)or not expected['name']or detail.get('name')!=expected['name']:raise ValueError('Exact CNN head name mismatch')
            shape=detail.get('shape')
            if type(shape)is not list or any(type(d)is not int for d in shape)or shape!=[1,48,48,CHANNELS[role]]:
                raise ValueError('Head shape mismatch')
            if type(detail.get('bytes'))is not int or detail['bytes']!=2304*CHANNELS[role]:raise ValueError('Head bytes mismatch')
            self.factors[role]=np.float32(2.**-fix)
        # Exact bytes, not a new hash gate: reject another graph/options/constants
        # before using its tensor indices. This private fixture is generated once
        # from the already accepted 37,648-byte post artifact, never from a new model.
        with np.load(contract,allow_pickle=False)as expected:
            if expected['schema'].item()!='posepriv.native_raw_post.contract.v1':raise ValueError('Model contract schema mismatch')
            if expected['model_bytes'].dtype!=np.uint8 or expected['model_bytes'].ndim!=1:raise ValueError('Model contract bytes must be a uint8 vector')
            if Path(model).read_bytes()!=expected['model_bytes'].tobytes():raise ValueError('Pinned post model bytes mismatch')
            constants={index:expected[f'constant_{index}'].copy()for index in range(4,21)}
        # This extra CPU Interpreter only calibrates tables. It creates no DPU
        # runner and is released before ordinary native calls begin.
        table_model=Interpreter(model_path=str(model),num_threads=2,
            experimental_op_resolver_type=OpResolverType.BUILTIN_WITHOUT_DEFAULT_DELEGATES,
            experimental_preserve_all_tensors=True)
        table_model.allocate_tensors()
        if [o['op_name']for o in table_model._get_ops_details()]!=OPS:raise ValueError('Pinned post graph op sequence mismatch')
        inputs=table_model.get_input_details()
        outputs=table_model.get_output_details()
        if len(inputs)!=4 or not isinstance(post_mapping,list)or len(post_mapping)!=4 or any(not isinstance(m,dict)for m in post_mapping):raise ValueError('Four exact post inputs required')
        if len(outputs)!=1 or outputs[0]['index']!=70 or tuple(outputs[0]['shape'])!=(1,1,17,3)or outputs[0]['dtype']!=np.float32:
            raise ValueError('Pinned float32 post output mismatch')
        for index,expected in constants.items():
            actual=table_model.get_tensor(index)
            if actual.dtype!=expected.dtype or actual.shape!=expected.shape or actual.tobytes()!=expected.tobytes():
                raise ValueError('Pinned graph constant mismatch at tensor '+str(index))
        for index,role in enumerate(ROLES):
            entries=[m for m in post_mapping if m.get('role')==POST_ROLES[role]]
            if len(entries)!=1 or inputs[index]['name']!=entries[0].get('tflite_name')or inputs[index]['index']!=index:
                raise ValueError('Pinned exact role/name/index mismatch')
            if tuple(inputs[index]['shape'])!=(1,48,48,CHANNELS[role])or inputs[index]['dtype']!=np.float32:
                raise ValueError('Pinned float32 post shape mismatch')
            raw=np.resize(np.arange(-128,128,dtype=np.int8),tuple(inputs[index]['shape']))
            table_model.set_tensor(index,raw.astype(np.float32)*self.factors[role])
        table_model.invoke();self.tables=[]
        for index in (21,22):
            values=table_model.get_tensor(index).ravel();table=values[:256].copy()
            if values.dtype!=np.float32 or table.shape!=(256,)or values.tobytes()!=np.resize(table,values.shape).tobytes()or not np.isfinite(table).all()or not ((table>=0)&(table<=1)).all():
                raise ValueError('Sigmoid lookup depends on tensor position or is nonfinite')
            table.flags.writeable=False;self.tables.append(table)
        prior=table_model.get_tensor(10);normalizer=table_model.get_tensor(5);bias=table_model.get_tensor(13)
        if prior.dtype!=np.float32 or prior.shape!=(48,48,1)or not np.isfinite(prior).all()or not ((prior>=0)&(prior<=1)).all():
            raise ValueError('Invalid float32 center prior')
        if normalizer.dtype!=np.float32 or normalizer.shape!=(1,1,1,2)or not np.isfinite(normalizer).all()or not (normalizer>0).all()or normalizer.flat[0]!=normalizer.flat[1]:
            raise ValueError('Invalid shared float32 coordinate normalizer')
        if bias.dtype!=np.float32 or bias.shape!=()or not np.isfinite(bias)or bias.item()<=0:raise ValueError('Invalid positive float32 distance bias')
        self.prior=prior.ravel().copy();self.normalize=normalizer.flat[0];self.bias=bias.item()
        self.prior.flags.writeable=False
        del table_model
        self.library=c.CDLL(str(self.library_path))
        self.library.native_post_abi.argtypes=[];self.library.native_post_abi.restype=c.c_int
        if self.library.native_post_abi()!=1:raise ValueError('Native post ABI mismatch')
        self.native=self.library.native_post
        self.native.argtypes=[c.c_void_p]*7+[c.c_float]*4+[c.c_void_p];self.native.restype=c.c_int

    def __call__(self,heads):
        if not isinstance(heads,dict)or set(heads)!=set(ROLES):raise ValueError('Exactly four raw roles required')
        values=[]
        for role in ROLES:
            value=heads[role]
            if isinstance(value,(bytes,bytearray,memoryview)):raw=np.frombuffer(value,np.int8)
            else:
                raw=np.asarray(value)
                if raw.shape!=(1,48,48,CHANNELS[role]):raise ValueError('Raw head array shape mismatch')
            if raw.dtype!=np.int8 or raw.size!=2304*CHANNELS[role]or not raw.flags.c_contiguous:
                raise ValueError('Raw head byte length/dtype/contiguity mismatch')
            values.append(raw)
        output=np.empty((1,1,17,3),np.float32)
        result=self.native(*(v.ctypes.data for v in values),self.tables[0].ctypes.data,self.tables[1].ctypes.data,
            self.prior.ctypes.data,self.factors['regression'],self.factors['offset'],self.normalize,self.bias,output.ctypes.data)
        if result!=0 or not np.isfinite(output).all():raise ValueError('Native post failed')
        return output
