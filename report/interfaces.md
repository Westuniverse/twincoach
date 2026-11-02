# 接口登记表

ROI1-SHARED-RX 1.0：独立0xa2040000 AXI-Lite／ID0x524f4931，128bit只读AXI4；
BGR8原图和256×256 packed Q5 map→196608B NHWC RGB Q7。复用S2MM及同一内核mutex，
独立1MiB coherent源池，保留静态mesh；64B用户描述符仅含用户指针/尺寸/ticket。
cap2f的PPS4保留op0–3/5、无op4，DT以roi-sampler phandle绑定新owner。CANCEL排空
真实AR/R；READ_QUIESCED不等同DONE。实际上板数值通过、整链退化，默认关闭。
见RTL契约（原工程参考：`roi_sampler_rtl.md`）、Linux ABI（原工程参考：`roi1_linux_runtime.md`）与实际结果（原工程参考：`decisions/0230-roi1-independent-crop-and-shared-rx.md`）。

FULL16-AXI-TILE 0.1（独立RTL原型）：`full16_axi_cached_pw`一总header驱动CHWc16输入
块读取、16通道W/post配置、缓存重放及结果写回。像素32byte，W128bit／post低73bit，
输出256bit拆低/高128词，AXI单outstanding INCR、最多256拍且4KiB分割。header的
full_pixels/offset/pixels定义完整平面stride；底层DMA地址/完整区域/一致性由调用者
维护。像素tag16与job tag16分开；最终完成等写B及计算排空，错误/取消排空AXI后
才局部清计算状态。外部reset需要共同AXI系统reset；失败输出前缀丢弃，cancel仅busy
有效。未连接现役PPS4或真实PS总线，见完整契约（原工程参考：`full16_axi_tile_20261030.md`）。

FULL16-TENSOR-ALU 0.1（独立RTL研究）：`tensor_alu16_s16`以四tap×16lane signed16
及2bit Resize奇偶相位、16bit像素tag输入，输出16lane signed16与原tag。Add用tap0/1，
Resize用TL/TR/BL/BR且分母16，MaxPool用四有符号最大值；独立Qa/Qb/Qout为0..15，
组合后一次向正无穷半舍入，再饱和或Relu6。六级弹性流水II1，输出反压冻结；配置
仅在全空边界接收，op3拒绝后需合法配置恢复，复位清valid不清宽数据。取tap、空间
边界、帧ID/ns、DDR与任务排空在外部；未改现役ABI。见
算术/PPA记录（原工程参考：`full16_tensor_arithmetic_20261030.md`）。
`tensor_alu16_folded_s16`保持同外部记录/数值契约，LANES=4或8内部复用，输入II4/II2；
配置还需等待holding记录为空，输出组装完整16lane后才valid，反压冻结holding、流水
及完整输出，tag逐chunk随流水。仅4lane完成独立PPA，默认面积优先候选，非板端选择。

FULL16-LAYER-TILE 0.1（独立RTL研究）：`full16_layer_tile`以编译参数选择DW/PW，
128bit流按PE-major/group-minor配置W16权重；DW的HWC16经3/5核、s1/2及显式pad
窗口输出独立8tap，PW从低128bit接共享8项输入。同像素各组tag一致，缓存内部
生成MAC first/last；16×48bit累加进入8lane后端，配置每PE48bit bias及右移0..31，
向正无穷半舍入后signed16饱和或Relu6。配置仅在相应模块排空边界进行，复位清
valid/权重资格，RAM不清；window_done只表示窗口流排空，MAC/post仍需独立排空。
帧/源ID与整数ns由外部NN任务所有者保存，pixel tag不代替源身份。无现役PPS4 ABI
或位流改变，见数据流与实现记录（原工程参考：`full16_layer_dataflow_20261030.md`）。

NATIVE-RIG-TAIL 1（0216，显式CPU候选）：`preview_rig_tail_library`为None或非空绝对库路径。
构造处匹配CPython主次版本/ABI1与固定13骨拓扑，默认保留原Python尾部；开启后一次
调用完成13骨端点规范化、骨仿射、Q1.14组合和最终记录。原IK/轴连续性回调、历史
提交、拒绝清理、原点/评分和DMA/GLES保持；`rig_tail_backend=native_cpu_13_bone`。
串行和几何子进程使用同路径，组件CPU耗时不代替整链帧率。见0216（原工程参考：`decisions/0216-batch-complete-rig-tail.md`）。

MAC16X8-S16 0.1（独立RTL研究）：16输出PE×8项signed16并行乘法，逐段first/last、
16bit tag、48bit累加结果；输出反压停完整流水，复位清valid。每段至多144组，
编译参数`INDEPENDENT_FEATURES=0`共享8项输入，`=1`为每PE独立8tap，均128乘法器；
bias/尺度、存储与DMA在本原语之外，无现役PPS4 opcode变更。见
数值和架构记录（原工程参考：`stable33_fixedpoint_architecture_20261030.md`）。

FIELD-DISPLAY-STABILIZATION 1（0214，显式候选）：
`preview_display_stabilization=true`在原scorer之后、rig之前生成独立平滑显示点。
原视频/原识别PoseFrame/confidence/源ID和整数ns及评分保持，纯跟踪帧仍不推进神经评分。
向量自适应5Hz/beta30/速度1Hz，两路独立；缺失点不生成观测，不反馈神经或LK历史。
200ms间断、尺寸或明确epoch变化清滤波历史，rig保持固定骨长并标注显示推断；缺省关闭。
详见0214（原工程参考：`decisions/0214-adaptive-display-joint-stabilization.md`）。
启用时几何回包另带当前`display_poses`供左侧“稳定显示点位”，原始`work['poses']`
及评分不变。显式`preview_head_world_stabilization=true`要求该显示消抖开启，
只在最终显示世界坐标对head方向应用12Hz/beta2/速度1Hz自适应响应，保持当前颈部
原点、轴尺度、非head矩阵/D/关节和原rig历史。见0215（原工程参考：`decisions/0215-final-world-head-and-stable-point-overlay.md`）。

FIELD-IMAGE-TRACKED-PREVIEW 0.1（0213，隔离实验、默认关闭、整链未验收）：
`preview_image_tracking`须精确bool，原CNN原源/解码点/conf与显示像素跟踪分离。
BODY17库现要求独立ABI2；`track_current_v2`一次完成当前金字塔、双向LK、资格及
commit，原四点marker ABI不变。原生端统一资格判断，Python不重复逐点校验；
完整诊断按需展开，在线仅传递接受点、来源与原CNN评分输入。
当前图像结果使用独立类型、显式显示mask和推断标记，没有当前CNN置信度；只有真实
CNN帧的原PoseFrame可用于评分。几何spec显式`scoring_input_mode=neural_only_v1`，
ready必须读回吻合；每请求`neural_score_poses`必为明确None或身份/尺寸与当前对一致
的两路原CNN点，缺失或不符拒绝。旧模式无此字段时保持原评分，禁止误把显示mask送
旧child评分。源/物理角色/整数ns不改，两路独立owner/有界历史，失败点不复制旧值；
CNN forwards、图像测量、有效新姿态源对和GPU确认分别计数。原门禁/18窗/600秒效果
标准保持，不把图像跟踪计数称作CNN帧率。详细约束见
0213（原工程参考：`decisions/0213-current-image-tracking-and-neural-score-separation.md`）。

ACTIVATION-LUT-AXIS 1：独立64bit/8lane逐byte表，cfg为256拍8bit顺序配置，只有末拍last；
数据keep/last关联、反压稳定、帧间配置及短/长包drain/reset见
专用契约（原工程参考：`rtl/activation_lut_axis.md`）。此原语未加入现役PPS4 opcode，不改变现有ABI。
实际M61个unary边界的生成器/厂商表对拍不代表M INT8质量通过。

FIELD-STRONGER-MODEL 1（0200）：显式`preview_pose_backend=rtmpose_m_cpu|rtmpose_m_remote`
加载本地RTMPose-M FP32及paired194监督动作头；缺省仍为movenet。物理front/side输入、
原ID/整数ns/源尺寸保留，50ms配对且两路均前进，raw SimCC响应不映射为概率；
动作特征只在canonical模块逐链raw>=0.5门控/归一化一次。2秒历史/500ms间断未知，
OTHER≠UNKNOWN。display-only角色映射在共享图像快照之后，不交换物理分类输入。
显示二值权重仅为观测mask，新后端关闭旧规范度评分，类别/logits不称运动标准度。
CPU帧另计，DPU计数0；`total_pose_frames`为清理后的总推理量，最后显示帧计数保持源绑定。
remote仅127.0.0.1、SSH反向转发、原JPEG及有界单请求/响应，不改写源时钟，不自动回退。
显式双session并发仅并行姿态骨干，observe/分类仍按物理front后side串行，不共享ORT会话。
FPGA蒙皮和Mali显示沿用现役；无新INT8/xmodel或CNN已上DPU的承诺。

FIELD-LIVE-PREVIEW 0.2：原MJPEG录制协议保持includes_pose_inference=false，表示
原片本身未经推理/绘制；可选旁路预览状态在footer diagnostics.live_preview单列。
采集是唯一相机所有者，每路容量一队列只丢预览，原片仍显式失败或完整提交。
原ID/读取时钟不改写，配对不超过80ms、两路都必须更新、过旧输入不处理；不是
曝光同步。子进程独占DPU/PPS4/GLES并在完成后关闭；原监督入口恢复0600。
HTTP只读loopback，经SSH本地转发，浏览器独立轮询源图像年龄并淡化过旧图像。
raw status与preview status分离，预览失败不伪装成功，也不让原片变成预览压缩图。
已登记HDMI旧片仅核对精确脚本/影片及用户后暂停，收尾恢复原argv。见
0174（原工程参考：`decisions/0174-field-recording-live-preview.md`）。
0.2向后兼容/frame.jpg，新增只读/stream.mjpg持续multipart（boundary=posepriv，
每部分Content-Type/Content-Length）；只发送新序号且源年龄≤1000ms的JPEG。
状态文字仍独立轮询；session_id只标识服务实例，下一次take/失败重连，不改源时钟。
HTTP慢写释放生产者锁，完成/失败关闭流，原片协议/满队列丢预览规则保持。
预览文字区域有界缓存须保持像素/状态语义；显式preview_decode_scale=1/2/4，
默认1；hdmi_display=true跳过JPEG/读回，浏览器仅状态，该模式本轮未做显示验收。


FIELD-LIVE-PREVIEW 0.3（0180/0184/0185/0186）：原录制协议不变；独立
show_hdmi_camera入口只做双摄当前预览，默认port=None无HTTP/录制/音频。
原JPEG读取时钟非曝光同步，frame_id对每次成功grab/retrieve占号，坏JPEG不重编号。
每路最新预览有界；模型进程独占一个native DPU/解释器，合成进程独占PPS4/GLES，
pose/图像/原ID/源尺寸整组发布，偏差≤80ms且两路都更新。三槽arena只覆盖READY，
借读至GPU上传返回；异常/死亡锁清理有界，不把缺失诊断当成功。
可选decode_prefetch + demand_pipeline让一组解码与当前模型重叠，未消费项存在时
让出CPU，不能阻塞原片录制。可选late_finish + swap_before_finish先提交窗口、
下一轮CPU准备后glFinish确认，只有确认才发布前帧计数/样本，GL复用前必须排空。
swap返回、GPU确认和物理扫描分开。原生画布按实际屏幕比例适配，无输入超1秒
显示等待、清旧画面/旧FPS，状态页不计新帧；HUD源龄取最近实际确认帧。
显式半尺寸解码仅预览，原片不改；CPU预缩小上传候选默认关闭，不冒充神经超分。
相机和录像驱动、组件与整链测量边界见10/28结果（原工程参考：`hdmi_live_pipeline_20261028.md`）。

PREVIEW-NATIVE-BONE（0187，默认Python）：三个planner可显式bone_affine_builder
注入目标CPython的CPU参考轴骨矩阵扩展，rig向heading传递同一builder。非精确内建
类型/极端数值/非支持分支沿原Python；支持路径保持pow、sum版本、尺度、量化和
异常顺序，不能放宽容差或修改拒绝后历史。构建/加载核对ABI与Python主次版本，
严格禁FMA/fastmath；配置preview_bone_affine_library不改变DMA或PL算子。

FIELD-GEOMETRY-PIPELINE 0.1（0189）：显式preview_geometry_pipeline=true，默认关闭，
要求原生HDMI及独立模型进程。单几何线程独占评分/planner/rig历史，在原arena唯一
借读期间完整复制两路图像为私有只读C连续快照，然后唯一release；主线程不再归还
该lease。单待取结果背压，结果包含原pose/ID/ns/尺寸及本帧计划、体架、计次/评分
快照。主线程独占DMA/GLES，仍仅GPU确认后计新帧；原500ms输入期限与1秒等待提示
保持。geometry_output_wait、geometry_image_snapshot与实际skin执行分别计时。
退出先join几何线程再释放模型/arena，异常明确失败；不增加DPU/PL或改评分算式。

FIELD-GEOMETRY-PROCESS 0.1（0196）：显式preview_geometry_process=true，默认关闭，
要求HDMI/独立推理进程，且与preview_geometry_pipeline互斥。一个spawn owner顺序
执行原scorer/planner/rig，父进程只做同图快照/一次release、有界dispatcher和原DMA/
GLES；READY未消费不再领取下一lease。socketpair协议posepriv.geometry.v1最多64KiB，
精确generation/双源身份、16×12 int16/valid mask、有限D、五种原reason、推断槽、
完整冻结ExerciseScore及原时间/顺序保持，静态vertex_requests原tuple不进入IPC。
重建的RenderExecutionPlan仅支持现场实际draw/execute消费者，不冒充完整关节计划。
启动响应driver stop；关闭按cancel→dispatcher join→geometry close→inference/arena，
仅GPU成功确认后计帧/发布评分/可选播报。异常不重放或自动切回串行；最终非正常
child关闭标失败并保留原错误，geometry_process统计与GPU完成数独立。

MOVENET-RAW-POST 0.1（0190）：native_post_library与native_post_contract成对显式
注入；预览对应preview_native_post_library/preview_native_post_contract，默认Builtin。
仅当前可信固定图/常量、四头实际名称/尺寸/字节数/int8/NHWC/固定量化点合格。
目标Builtin现场校准LUT，首个真实metadata到达后构造一次，无第二DPU；标量及显式
AArch64 NEON均保持首tie/YX/float32字节，独立输出，禁FMA/fastmath。失败明确退出
原owned transport，不自动回退/伪称Builtin。native_raw_post计完整调用（首帧含校准），
dequant_set=0；post_backend=native_raw_exact。原源身份、预处理/逆padding及评分保持。
新图另行资格验证，不得以任意新contract代替算法验证。私有model/contract不入Git。

GLES-LINKED-LOCATIONS 0.1（0191）：preview_gl_locations_cache/参数cache_locations
须精确bool，默认false。每Painter/上下文分别缓存attribute/uniform的(program,name)，
各最多32项；负位置亦保持，关闭清空，新上下文重新查询。uniform值与上传/绘制/
完成流程不缓存或改变。presentation.gl_locations_cache单列实际选项。

NATIVE-LIMB-AXIS 0.1（0192，默认Python）：preview_native_limb_axis须bool且要求
preview_bone_affine_library包含limb_axis方法；原骨ABI1/build保持。显式builder接受
slot/segment/rotation/dt/只读bind和history，支持有历史的原生float64 ndarray与
builtin float数据，返回独立原类型axis/(local,direction)；其他值NotImplemented回
原oracle，不更新历史。三项dot使用目标math.fsum，保持GIL/float顺序/第二投影，
源/评分及末尾历史提交不变；limb_axis_backend=native_cpu_history明确含原首次回退。

GLES-NORMAL-PREP 0.1（0195，默认NumPy）：显式preview_normal_prep_library加载
ABI1/同CPython主次版本的CPU扩展。仅精确tuple16×12与builtin int16域接受；其他
行返回NotImplemented并以同一份一次性物化rows走原NumPy，不coerce或消费两次。
输出独立bytes持有的只读float32[16,3,3]逆转置基础矩阵，保持原cross/det/1e-8
阈值及奇异identity规则，禁fastmath/FMA；最终rotation@cof、转置及连续布局仍由
原NumPy完成。骨矩阵/位置/源/评分与GLES提交确认语义不变，normal_prep_backend
区分native_cpu_cofactor与numpy；CPU组件收益不代表新PL或物理HDMI扫描性能。

HDMI-CAMERA-SESSION 0.1：显式后台会话JSON的schema为posepriv.hdmi_camera_session.v1，
包含精确整数pid>1/start_ticks>=0、boot_id和完整command argv。stop_hdmi_camera仅对
当前boot、同UID、start_ticks和cmdline完全匹配的项目Bash监督器发送一次INT；pidfd
绑定进程避免PID复用，最多等待20秒，不升级信号。旧boot或已退出报告already_ended，
身份不符明确失败；前台启动仍由终端Ctrl+C退出，不扫描或接管其他会话。

HDMI-SPEECH 0.1：show_hdmi_camera显式--speak-feedback才给renderer传speech_volume，
默认None不构建语音后端。音量为Speech Dispatcher相对整数-100..100，CLI默认-80。
仅确认后的新鲜（源龄≤1秒）帧推进准备/完整深蹲完成反馈，停止/过旧时不新播并丢弃
待播；最多一条最新待播、最短4秒、待播年龄>5秒丢弃。语音错误单独报告，不改评分
或视频状态；客户端等待≥20秒请求停止该客户端并禁用语音，帧循环不等待终止。
speech_feedback.audibility_verified始终false，实际听音另行确认。
相机报告audio/speech_feedback_requested为输出请求开关，audio_recording始终false。
configure_coach_audio默认只读；显式选择只允许当前USB输出，先降音量再选择/解除
静音，永不播放测试音。新候选验证不构成启用现役声音的授权。

OPTIONAL-MULTI-ACTION-REFERENCE（离线研究）：RTMPoseONNXReference只显式CPU加载
本地模型/输入框，RawRTMPoseFrame保持原source坐标和未经概率校准的SimCC响应；
不注册现役PoseEstimator。独立三动作CLI只接受带显式源尺寸的pose_trace v2或声明的
raw schema，ID/ns/dims须精确int且逐view递增；全局legacy trace reader不改。
SourceTimeSeconds为离线秒接口保存原整数ns，间隔/积分先整数相减，整500ms与偏移
不误判，500ms+1ns仍拒绝；Causal*的真实ns/特征schema与旧名义60帧权重分开。
缺失、长间隔/覆盖不足不产生整体规则参考分；UNKNOWN失败的训练头不接生产。
extract_action_features的显式anatomical_view_mapping=swapped仅交换局部人体角色，
physical view/ID/ns/dims/左右标签/标定保持；默认device_names不变，不自动猜朝向。
CLI非默认报告逐观察记录实际角色来源；有状态会话中映射须固定，改变须重建历史。
改变取源不得直接复用旧归一化器/权重作精度结论。
来源/许可及阶段限制见研究说明（原工程参考：`model_actions_reference_20261026.md`）。


SKINNING-MATRIX-ALIASES 0.1：`AuthoredRig.matrix_aliases()`只将具有相同绝对线性
表达式的父子合为完整仿射组，非零规则保持独立，不跨不同锚点猜相等。输出骨序
保持，`SkinningMatrixAliases.compress`逐帧核对完整矩阵值后才返回代表骨；不符
在DMA前拒绝。位置索引映射到组，原全部Q0.15权重保留并交原计划合并重复索引；
法线仍用原骨序/权重/顺序，原driver/D/state/图像/评分不变。显式
`--authored-alias-rig`需全骨缓存及同输出骨序的rig schema1，默认原139骨位置计划
保持；position_matrix_count/wire_vertices/matrix_aliases单列，见0173（原工程参考：`decisions/0173-exact-authored-matrix-aliases.md`）。

AUTHORED-GLES-CONSUMER 0.1：显式`--authored-rig-cache`声明schema1 mesh/chapters清单，
需world cache、lighting及native-palette-library。`authored_weighted_mesh`包含来源/
许可、完整骨序及全部Q0.15影响，绑定顶点/面/颜色必须与当前资产完全相同；
`host_authored_rig_preview`骨序和原world report一致，driver/state/D逐字节相同。
保留原图/源/推断与评分，旧16槽缓存仍为这些字段的权威。位置通过原局部调色板
和PPS4完整权重通路，PS `WeightedNormalPlan`按全部同骨逆转置混合法线，Mali只
作视角旋转、归一化和原照明。法线不是LBS表面精确微分，不使用旧四影响/16骨
代替全骨；活动骨奇异/反射在本帧DMA前拒绝，完成全部包后才发布/复用。位置
和法线复用共同的完整矩阵字节，reset清除；默认入口保持。法线/位置和准备耗时
分别记录，均不包含CNN/FK缓存准备，见0172（原工程参考：`decisions/0172-authored-rig-gles-consumer.md`）。
可选native-normal-library绑定同一`WeightedNormalPlan`，独立CPU C normal ABI1只做
全权重混合，Python仍生成/验证逆转置；double运算/禁乘加收缩/末尾float32，须
同NumPy字节对拍。矩阵/权重/法线/骨序不变，不自动选择或回退，旧整数palette
ABI1保留。normal_backend与组件计时明确区分NumPy和CPU C。

AUTHORED-RIG 0.1：`AuthoredRig`以显式父子骨名、共同绑定空间Q1.14枢轴和驱动
映射，把原显示仿射矩阵转为全局全骨变形矩阵。子枢轴在量化前跟随父骨；最终
Q1.14矩阵各项独立舍入，父/子变换同枢轴的每坐标差界为
`(1 + sum(abs(pivot_q1_14))/16384) / 16384`，不是线端完全相等。
`RotationBlend`分配旋转并保留起始驱动的正伸缩，`AxialTwist`只提取指定共同
绑定轴的相对扭转；半周纯swing无定义时保留起始驱动并返回明确ambiguous_twists。
极分解采用8轮标量迭代/旋转Q24规范，末尾一次矩阵Q1.14量化；不接设备、不
增加时序滤波，不生成观测/评分。输出已含inverse-bind，不得再乘一次。
JSON schema1存完整层级/输出顺序/规则；显式离线export_authored_rig_preview入口
保留原states/display/driver字节，新增全骨矩阵与单独推断状态。原16槽模型/现役
缓存保持，来源仍需原调用链验证，见0168（原工程参考：`decisions/0168-authored-rig-retargeting.md`）。

SKINNING-PALETTES 0.1：显式`GlobalSkinningVertex`/`SkinningPalettePlan`将全局四影响
顶点确定性重映射到局部≤16骨、≤8192点的完整旧蒙皮包，成功后按原顶点顺序返回
一个`PackedSkinningOutputBatch`。`WeightedSkinningVertex`/`WeightedSkinningPlan`
接受全部已量化影响；合并重复骨后≤4走原PL融合，>4仅将单骨变换交PL、PS按全部
Q0.15权重作整数混合。I/O前按每骨bbox证明宽影响中间值可无损表为16bit，否则
拒绝帧；对应无限制18bit变换/末尾一次舍入参考，不能悄悄提前饱和或Top4截断。
矩阵已含inverse-bind，旧包/16槽`AvatarMeshAsset`保持；无观测/评分/源时间改变，
无自动重试或部分帧发布。正式全骨资产/FK/法线/显示仍需接入，见
0167（原工程参考：`decisions/0167-local-skinning-palettes.md`）。
执行时可显式复用既有`NativeSkinningValidator`，默认Python校验保持；两者仍校验
完整输出长度/保留字段，不移除旧传输层的校验或故障生命周期。
可选`NativeSkinningPalettes(plan, library)`绑定该静态计划，CPU C ABI1完成顶点
重排和全部影响的整数混合；加载/ABI/计划不符直接失败，不自动回退。Python范围
证明、矩阵前导、传输和原默认算法保持；C只读已验证的16字节步长点，并产生
同一Q1.14/保留零字节输出。此为PS代码，不是新增FPGA算子或真实姿态模型。

播放报告保持原累计schema，后台单待写快照/原子替换、退出排空和错误传播的约定见
0162（原工程参考：`decisions/0162-asynchronous-movie-report.md`）。这不改变媒体帧、PTS或评分时钟。

两个独立开发模块产生依赖前，必须先在这里登记接口。

| ID | 生产者 | 消费者 | 负载 | 版本 | 状态 |
| --- | --- | --- | --- | --- | --- |
| WORLD-AVATAR-CACHE | 显式世界姿态计划导出及schema1章节清单 | PPS4/GLES实验入口 | 每章缓存或null，绑定元数据严格一致；原ID/时间/旋转/显示时钟关联因果原图，全13槽推断/原观测空；16槽Q1.14及float32 D，显示点位另绑定同图；原score工作与image工作单列；排除现场CNN推理与缓存准备时间 | 0.1 | 主机6/13/23项、ARM7278帧、三姿态6笔真实DMA及实际PPS4/Mali/HDMI首轮通过；预计算源/取景/性能边界见结果（原工程参考：`world_display_gpu_20261027.md`） |
| IMAGE-DISPLAY-SCHEDULE | 原章显示时钟及独立MJPEG索引 | 显式latest-raw显示/模型参考 | 每视角timestamp≤显示时间的最新图；不借未来、保留真实空档；不含pair/raw_index，原raw_pairs/评分保持；默认paired入口不改 | 0.1 | 15项调度测试中新增3项，六章8934逐时刻审计，前两章797个正面时刻恢复≤100ms，见0157（原工程参考：`decisions/0157-independent-current-image-display.md`） |
| IMAGE-AVATAR-PLAN | 已复核单视角源身份与33点世界姿态 | 显式WorldAvatarPreview/主机显示 | ImageAvatarFramePlan无pair、points为空，全部13槽推断/观测置信为0；保留16槽Q1.14/原源时间；固定比例，显式站姿图像参考保留XY位移但不估根深度/接地；>250ms/reset清历史，同源重复不推进；recorded_hold仅显式文件策略 | 0.1 | 15项主机测试、7278完整计划/重复字节与三资产帧整数黄金；显式缓存已接实际PPS4/GLES，原评分保持独立，见板端结果（原工程参考：`world_display_gpu_20261027.md`） |
| WORLD-HEAD-PITCH-REFERENCE | 独立复核的正立平视原图窗口 | 显式WorldAvatarPreview/缓存导出 | ≥3独立可见源、同图身体相对俯仰中位数；只右乘固定局部逆俯仰，再走原头滤波；不改原点位/评分/非head槽/D/头长/颈根，全推断；相机几何须一致，运动reset保留参考；默认不启用 | 0.1 | 主机22项/完整3601帧、ARM完整字节、三姿态6笔实际DMA/490帧实际GPU/980帧HDMI首轮通过；朝向偏差有改善，去抖未完成，见结果（原工程参考：`head_neutral_pitch_20261027.md`）和0159（原工程参考：`decisions/0159-world-head-neutral-pitch.md`） |
| WORLD-FULL-SHOULDER-BASIS | 同一原图世界姿态的完整肩线/颈骨盆方向 | 显式WorldAvatarPreview | full-body-basis才使用完整肩线正交基，绑定基底/固定骨长和原头/整体滤波保持；不压水平偏航；退化仍走有标签参考，全部推断/评分不改，默认旧路径 | 0.1 | 主机26+37项、默认3601控制/ARM7202字节/6笔实际DMA/1000帧实际GPU通过；第一章审看片HDMI两轮通过，近地失败与步长增加保留，见结果（原工程参考：`full_shoulder_basis_20261027.md`）及0161（原工程参考：`decisions/0161-world-full-shoulder-basis.md`） |
| PREVIEW-CURRENT-HEAD-ORIENTATION | 同一前视源图的头旋转及必需的源身体旋转 | 显式完整体架后处理 | 原ID/时间严格关联，≤100ms，face_transform/world_face_plane；源身体相对头姿重定向到展示身体，SO(3)3Hz/默认2rad/s、可显式4；1°死区，>250ms/reset/参考身体复位；仅head槽、固定头长/颈锚点，始终推断，不升级原有效位/评分/D | 0.1 | 主机确定性/1430帧离线原型；4rad/s取舍见0155（原工程参考：`decisions/0155-current-face-head-reference.md`），未接现役GLES |
| PREVIEW-CURRENT-BODY-ROTATION | 显式完整体架及原display_transform | 整个数字人展示矩阵 | 当前展示旋转6Hz/4rad/s，前视源时钟；重复源不推进，>250ms/reset/参考身体清历史；绕展示骨盆统一左乘D，全部Q1.14矩阵/位置锚点/尺度/推断/原观测/评分不改 | 0.1 | 主机7项及1430帧A/B/C；原参考恢复状态机保持；无本轮实际GPU/DMA验证，见0156（原工程参考：`decisions/0156-current-body-display-rotation.md`） |
| OFFLINE-FACE-HEAD-REFERENCE | 主机Face Landmarker与同源世界姿态JSONL | 私有头姿参考JSONL | schema1；原图旋转/裁剪ROI、源ID/时间/显示时间、同图复用、原4×4脸变换及裁剪内bbox；关闭表情系数、不输出身份；返回结果不等于可见真人脸 | 0.1 | 6项、1430行及5行最终CLI精确核对；专用脸模型未选入本轮演示，见0155（原工程参考：`decisions/0155-current-face-head-reference.md`） |
| OFFLINE-WORLD-POSE-REFERENCE | 显式主机MediaPipe及索引录像 | 私有JSONL/离线显示实验 | schema1；源图ID/时间、原显示时间、旋转/预热、逐图复用/250ms源间隔复位、33图像/世界点及独立presence/visibility；不写POSE-FRAME/评分 | 0.1 | 7项主机测试、1650行模型参考及5行最终入口严格核对；未部署，见记录（原工程参考：`world_pose_reference_20261027.md`） |
| SHARED-POSE-DMA | 单设备内核事务/PS 批次 | 共享关节、运动摘要、校正后三角测量 RTL | 32-byte arm64 ioctl；配置后 S2MM→MM2S，完整 16-byte 拍，输出 TLAST 握手后才能切换 | 0.1 | 配置/背压自检、主机 ABI、目标内核编译、统一实现及匹配 Linux 候选通过；新镜像启动、板端对拍及 P95 未证明；见 `rtl/shared_pose_dma.md` |
| SHARED-SKINNING | 精细网格整帧包/共享RTL选择器 | 复用蒙皮算子5及有界分批适配器 | PPS4 ID=0x50505334、完整六算子能力0x3f；每批512B前导+16N输入/16N输出，N<=8192；最终输出握手前持锁，整帧校验后分批并完整汇合；显式exchange_skinning_packet借同fd | 0.2 | PPS4联合/板端黄金及原生检查通过；preview可借应用fd接实际点到投影/op4，136对姿态/423笔实际DMA生成三张完整画布与软件像素参考全相等，见显示消费（原工程参考：`shared_skinning_display_20261025.md`）。默认CPU保持，实时性能仍待投影/面准备优化 |
| PREVIEW-ROTATE-RESIZE-CPU | 原索引JPEG BGR8与显式CPU库 | GLES视频纹理准备 | ABI1新增create_rotated_v1；正步长原图、0/90/180/270逻辑取样、调用方输出ROI；每视角最多2计划 | 0.1 | 默认关闭；每视角锁覆盖调用，创建线程在pool.shutdown后close；原评分/点位/alpha保持，见0132（原工程参考：`decisions/0132-native-rotated-video-resize.md`）；ARM44项/完整8934状态/3张无损画布通过，稳定24未完成 |
| NATIVE-MOVIE-REPLAY | 既有1280×720 MJPG AVI/GStreamer RGB16 | 原生Mali/EGL恒等纹理窗口 | PTS逐帧节奏、appsink队列1、无丢帧、每帧GPU完成与swap返回统计；原生成/评分不参与 | 0.1 | 显式独立入口，3张RGB565黄金、退出控制、正式9300帧/首轮8934循环和现役通过；仅文件播放，见0134（原工程参考：`decisions/0134-native-egl-movie-presentation.md`） |
| RECORDED-AVATAR-FREEZE | 同一录像图像/姿态源身份及完整体架结果 | 显式playlist数字人消费 | 只缓存<=100ms的tracked/partial完整显示；原图/姿态身份全部不变且原体架因年龄转neutral时冻结对应快照，recorded_hold及13槽全推断、源年龄持续增长、原观测/评分不变；换源/换章/拒绝清缓存 | 0.1 | 默认关闭，freeze-record-gaps要求playlist及rigid-avatar；仅文件回放，不扩展live观测保持，见0153（原工程参考：`decisions/0153-recorded-avatar-freeze.md`） |
| GLES-MJPEG-EXPORT | 实际GLES画布读回BGR8 | 静音1280×720 MJPG AVI | record-backend默认auto，显式opencv-mjpeg固定质量100；原源时钟/评分保持，转换/编码/release在生成total_ns外单列，wall包含编码；不同后端有损结果不等价 | 0.1 | 使用现有OpenCV，见0148（原工程参考：`decisions/0148-recording-backend.md`）；组件测量不代表实时生成或文件扫描输出 |
| PREVIEW-RELATIVE-ARMS | 当前观测计划及原连续性后的显示计划 | 显式数字人手臂显示滤波 | 同一pair/16槽有效位，6Hz肩部相对肘腕；只处理当前完整upper/lower/hand；原观测/保持/评分不改。组合palm_roll_stability时，后滤波坐标独立掌心历史12rad/s、源间隔>100ms/缺失/拒绝清历史；不组合保留0.1原路径 | 0.2 | 默认关闭；组合修正与取舍见0140（原工程参考：`decisions/0140-mesh-jump-attribution.md`），吞吐和旧几何证据见长片记录（原工程参考：`image_lookahead_20261026.md`） |
| PREVIEW-RIGID-AVATAR | 同一pair的原观测与显示计划、13骨绑定及私人固定比例 | GLES显式完整数字人体架 | 原observed_valid/源/评分不改；单一身体坐标系和统一尺度、手臂两骨IK、腿按当前两段方向连接固定长度、头部朝向；render_valid与inferred_slots分开，100ms身体保持后有标签的参考姿态，>250ms源间隔/reset清历史；当前关节点可重建长度拒绝的手臂，参考/恢复按固定骨长连续过渡；head3Hz/2rad/s，鼻点缺失仍可消费可信脸部轴，保留推断标记；腿参考近直立，允许脚端相对原观测有偏差；参考模型保持最近展示锚点，恢复按6Hz/4rad每秒/2显示单位每秒/log尺寸1.5每秒有界跟随，原观测保持仍限100ms；原几何/蒙皮保持，消费端将可选行优先float32 display_transform合入GPU投影并旋转光照，display_body_scale单列 | 0.6 | 默认关闭，显式rigid-avatar需schema1 rig-profile及中文状态层；与独立torso-heading互斥。基础见0144（原工程参考：`decisions/0144-complete-rigid-avatar.md`），腿的角度/位置取舍见0149（原工程参考：`decisions/0149-leg-direction-retarget.md`）、参考锚点见0150（原工程参考：`decisions/0150-body-reference-frame.md`）、统一GPU帧见0152（原工程参考：`decisions/0152-gpu-whole-body-frame.md`），拟合不是实体标定或动作真值 |
| VIDEO-STREAM | 视频采集 | 预处理/显示 | AXI4-Stream 视频；格式待摄像头审计 | 0.0 | 待定 |
| CAPTURE-FRAME | 主机摄像头适配器 | 姿态模型 | 视角、帧号、原始接收时间戳、同步修正时间戳、实际尺寸、像素格式、不透明像素负载 | 0.3 | OpenCV/V4L2 适配器、稳定设备路径、双路并发采集泵与显式时间偏移已实现；实体设备待测 |
| VIDEO-REPLAY | 本地双路录像 | 姿态模型/同步器 | 文件 FPS 生成的媒体时间戳、显式两路固定偏移、实际帧尺寸和 BGR8 负载 | 0.1 | TFLite 双路 12 对冒烟通过；离线报告明确不作为实时性能 |
| POSE-FRAME | 单视角姿态模型 | 同步器/时序引擎 | 摄像机视角、源图尺寸、归一化关键点、置信度、帧号、同步时间戳 | 0.4 | MoveNet RGB 补边预处理、坐标逆变换、显式同步修正和宽高比安全的指标坐标转换已实现 |
| COACH-PIPELINE | 最新帧调度/姿态模型 | 动作会话 | CAPTURE-FRAME → POSE-FRAME → 可选 EXERCISE-EVENT；逐帧推理/会话/端到端耗时、近期 P95、采集覆盖和同步统计 | 0.4 | 无依赖主机编排、确定性计时和有界 P95 窗口已实现；实体双摄/DPU 待测 |
| POSE-PAIR | 同步器 | 指标提取 | 正面/侧面姿态帧、时间偏差、配对/淘汰统计 | 0.2 | 主机配对与窗口化偏差统计已实现 |
| TRIANGULATION-3D | 双视角姿态 + AVATAR-CALIBRATION | 3D 骨架/蒙皮 | 版本化相机内参（含可选五项畸变）、camera-to-world 外参、可选极线校正旋转、同名关键点、置信度；输出世界坐标、射线间距和有效位 | 0.4 | 通用浮点黄金模型与已校正双摄的 Q1.14 参考路径均已接入 `AvatarFramePlanner`；标定脚本可生成 rectification 段，五个主机/板端入口可用同一 CLI 开关选择并写入报告，真实 C920e 误差对拍和板端 DMA 待后续 |
| SKELETON-3D | 3D 关键点 | 数字人骨骼/渲染适配器 | 关节位置、置信度、有效位和固定骨骼拓扑；输出虚拟胸口/骨盆及骨段起止点、长度和质量 | 0.1 | 软件参考、缺失/低置信度传播、骨段矩阵和数字人显示 smoke 已实现；真实双摄标定与板端证据待后续版本 |
| AVATAR-JOINT-FILTER | SKELETON-3D + 同步对时间戳 | 骨段矩阵/数字人显示 | 每关节三维位置、有效位、单调时间戳；输出 One Euro 风格自适应低通位置，长间隔复位且不伪造无效关节 | 0.1 | `AdaptiveJointFilter`、默认参数、CLI 开关和单元测试已实现；真实 C920e 静止标准差与动作延迟基线待标定后补充 |
| AVATAR-VIEW | 3D 骨骼段/蒙皮结果 | OpenCV/framebuffer/HDMI 渲染器 | 世界坐标骨段与可选蒙皮顶点、观察相机 yaw/pitch/zoom/pan、目标视口尺寸；输出带名称的像素线段和三角形 | 0.1 | 无依赖正交投影、自动居中、旋转缩放、低多边形三角形投影和中文状态栏已实现；真实合法网格材质与板端渲染后端待后续版本 |
| POSE-TRACE | 本地记录器 | 确定性回放 | 带版本的 JSONL 头和交错 POSE-FRAME；每帧保存源图尺寸，不含原始像素 | 0.2 | schema 2 已实现，并兼容读取缺少尺寸的 schema 1 |
| EXERCISE-SCORE | 评分引擎 | 界面/会话 | 可选 0～100 规则符合度、置信度、覆盖率；每个分项含观测值、单位、视角、理想范围、容差和权重 | 0.2 | 可解释主机基线已实现 |
| EXERCISE-EVENT | 动作引擎 | 界面/TTS/会话 | 实际使用的同步姿态对、阶段、次数、实时评分、可选整次评分和纠正提示 | 0.2 | 主机基线已实现；显示可按精确同步对刷新 |
| COACH-DISPLAY | 应用编排/统计 | 终端、OpenCV 窗口或 HDMI 渲染器 | 中文阶段、次数、规范度、置信度、覆盖率、反馈、FPS、端到端平均/P95 延迟、同步偏差和分类丢帧 | 0.2 | 渲染无关状态模型、骨架/对比画布统计已实现；实体 HDMI 待测 |
| SESSION-REPORT | 动作会话/运行统计 | 本地实验分析 | 版本化 JSONL；完整动作分项、纠正建议、FPS、全会话平均/最大与近期 P95 延迟、采集/同步、数字人和关节滤波统计；不含像素和关键点 | 0.3 | 写入器、确定性回放、参考/RTL 比较、数字人 packet 字节和滤波样本/修正距离摘要已实现；实机基线待补 |
| HOME-EVENT | 事件总线 | 可选适配器 | MQTT/HTTP 场景事件 | 0.0 | 延后 |
| JOINT-METRICS | 关键点传输/RTL | PS 动作指标层 | 128 bit AXI4-Stream；同步双视角的三点关节批次；等比例图像坐标 Q1.14 输入；点积、两个模平方及关节/视角元数据输出 | 0.5 | PS 在量化前消除非正方形画面的轴比例差；ABI、RTL 和批量后端已验证，真实板卡数值/延迟待测 |
| AVATAR-AFFINE-3D | 3D 骨架/网格前端 | RTL 仿射变换基元 | 三维点和 3×4 矩阵为有符号 Q1.14；输出为 34 bit Q2.28；valid/ready 单拍事务 | 0.1 | 算术核和反压自检已实现；矩阵缓存、蒙皮权重、AXI 包装及板端验证待后续版本 |
| AVATAR-SKIN-BLEND | 骨骼变换通道 | RTL 蒙皮融合基元 | 四组 18 bit Q3.14 三维点、四个无符号 Q0.15 权重（总和 32768）；对称四舍五入并饱和为 Q1.14 | 0.2 | 位精确软件参考和 RTL 反压/连续吞吐自检已实现；18 bit 输入使每个权重乘法只使用一个 DSP；上游仿射量化、矩阵缓存、AXI 包装和板端验证待后续版本 |
| AVATAR-SKIN-LANE | 绑定姿态网格/骨骼矩阵 | RTL 完整单顶点蒙皮通道 | 一个 Q1.14 顶点、四组 Q1.14 3×4 矩阵和四个 Q0.15 权重；五级弹性流水输出 Q1.14 顶点，II=1 | 0.1 | 软件位精确参考、Verilator 自检及 K26 200 MHz OOC 已通过；矩阵驻留/AXI 批处理已由 packet wrapper 串接，板端 DMA 数值仍待实测 |
| AVATAR-SKIN-AXIS | PS/DMA 顶点流 | RTL 矩阵驻留与蒙皮流 | 16 项矩阵表；128 bit 顶点输入（位置、4 个索引、4 个权重）；64 bit Q1.14 顶点输出；TLAST FIFO 保持批次边界 | 0.1 | Python 位打包/解包、参考/DMA 后端、Verilator 5.032 自检和 Vivado 2026.1 K26 OOC 已通过；4955 LUT、4132 FF、48 DSP、WNS +0.030 ns；板端 DMA 待测 |
| AVATAR-SKIN-SERIAL | PS/DMA 顶点格式的独立实验入口 | 复用算术单元的 RTL 蒙皮核 | 同 AXIS 128 bit 顶点/64 bit 输出与16项矩阵表；一次仅一个顶点；矩阵只在空闲时写入并优先于顶点；输出反压保持data/last；复位取消在途顶点 | 0.1 | 独立Python定点黄金、自检RTL；输出首次有效13周期、最小输入间隔14周期。独立OOC记录见0083（原工程参考：`decisions/0083-reused-skinning-geometry-kernel.md`）；尚未接共享DMA/板端 |
| AVATAR-FRAME-PLAN | POSE-PAIR/标定 | A53 蒙皮调度与渲染层 | 三角测量点、人体相对模型原点、固定骨段、16 项 row-major 3×4 矩阵、有效位、资产骨骼到姿态拓扑索引、逐骨骼绑定长度、inverse-bind 和预绑定顶点批次；每帧可交给 CPU/Verilator/DMA 后端 | 0.4 | 已修正平移 3/7/11 的矩阵 ABI；以骨盆为原点；A53 按名称映射不同资产骨骼顺序并组合 inverse-bind，参考/packet/DMA 包格式不变；任意骨骼名称/节点层级重定向与板端联合镜像仍待接入 |
| AVATAR-DISPLAY | AVATAR-FRAME-PLAN/RESULT + AVATAR-MESH-ASSET | OpenCV、framebuffer 或 HDMI 合成器 | 人体相对原点/比例、面板尺寸、相机 yaw/pitch/zoom/pan；输出同空间自动居中的像素骨段/三角形、有效骨段/关节统计、置信度和蒙皮顶点数 | 0.2 | 骨架与蒙皮顶点已统一到人体相对模型空间，并有世界平移不变回归；真实合法模型、字体和板端实机验证待后续版本 |
| AVATAR-MESH-ASSET | 离线 GLB/glTF 导入器 | A53/Verilator/AXI-DMA 蒙皮批次 | Q1.14 顶点、四个矩阵索引、四个 Q0.15 权重、逐骨骼 inverse-bind、三角形索引、来源/许可证元数据 | 0.4 | 固定点 JSON 往返、glTF/GLB inverse-bind、显式骨骼名称映射及运行时组合已实现；资产骨骼顺序可不同，自动语义猜测、材质路径与真实合法高密度资产待后续版本 |
| AVATAR-SKIN-PACKET | A53 DDR/DMA | KV260 AXI4-Stream 蒙皮包装 | 每帧固定 32-beat 矩阵前导 + 128 bit 顶点流输入；128 bit 补齐输出；TLAST 只标记最后顶点并令解析器回到下一帧矩阵 0 | 0.3 | 默认并行核及旧验证保留；显式RESOURCE_SHARED=1选择复用核，下一帧首半矩阵可缓冲，第二半等旧顶点完成；复位不接受输入。两种字节桥保留各自反压断言，新核包OOC/完整精细网格逐字节对拍见0084（原工程参考：`decisions/0084-serial-skinning-packet-backend.md`）；共享系统/板端接入待完成 |
| RECTIFIED-TRIANGULATION | 校正后的同步双视角关键点 | `rectified_triangulation_axis`/3D 骨架 | 128 bit AXI；Q1.14 front/side 坐标、基线、焦距、置信度和 joint id；输出 32 bit Q1.14 X/Y/Z、valid、视差和 TLAST | 0.4 | Python 位精确参考、主机批处理后端、Linux 单设备/dma-proxy 传输适配器、`AvatarFramePlanner` 与五个入口已接入；A53 可直接使用 `posepriv_rectified_stereo_packet.h` 的显式小端打包/解包；RTL 输出消费与下一点接收支持同拍无气泡；真实双 C920e 校正误差、设备树节点和板端 DMA 仍待验证 |

每个被接受的接口都必须定义单位、坐标系、精度、valid/ready 或所有权语义、错误行为和版本兼容规则。

主机基础数据契约定义在 `src/posepriv_coach/contracts.py`。摄像头时间戳使用同一主机单调时钟产生的整数纳秒值；配对不使用墙上时钟。

连续深蹲事件契约定义在 `src/posepriv_coach/session.py`。每个 `SquatSessionUpdate` 显式携带本次评分实际使用的 `SyncedPosePair`，显示层不得用“最近看到的两路帧”重新拼接画面。只有完整动作评分有效时才生成纠正提示；提示携带指标标识、相对配置范围的方向、观测值、分项分数和置信度。中文表达和 TTS 频率限制属于下游策略。

规则文件 schema 2 要求每项指标显式登记单位。`ScoreComponent` 保存观测值、摄像头视角、单位、理想上下限、容差、权重、分项分数和置信度，因此满分项目也可以解释和复查，而不是只有出错时才知道原始指标。

中文展示适配器定义在 `src/posepriv_coach/feedback_zh.py`。它只在一次深蹲完成后生成一条可直接交给本地 TTS 的中文文本，包含动作次数、规范度、观测置信度以及优先级最高的一条纠正建议；实时帧不生成语音，避免重复播报。当前模块只负责文本，不引入语音引擎或联网依赖。

主机采集和回放边界定义在 `src/posepriv_coach/capture.py` 与 `src/posepriv_coach/pose_trace.py`。CAPTURE-FRAME 的 `timestamp_ns` 表示主机后端返回一帧的时刻，不代表传感器曝光时刻；`sync_timestamp_ns` 默认与其相同，也可以加上用户明确配置的固定偏移。姿态模型必须将采集帧的视角、帧号、同步时间戳和实际源图尺寸复制到 POSE-FRAME。采集到结果的端到端延迟仍以未修正的 `timestamp_ns` 计算，时间偏移不会伪造更低或更高的处理延迟。

原生 C++ VART 探针遵循同一边界：`grab()` 与 `retrieve()` 均成功、完整图像可用后记录主机单调时间，并在统计行声明 `capture_timestamp_semantics=host_monotonic_after_retrieve`。旧日志缺少该声明时只能按对应源码版本解释；任何主机返回时间都不能表述为曝光时间。

`DualCameraCapturePump` 为正面和侧面各运行一个常驻采集线程，将新帧直接提交给线程安全的最新帧调度器，不在采集层建立无界队列。任一路读帧失败会停止采集并保留原始异常；上层必须显式检查并显示故障，不能继续复用旧帧。

`DualViewSynchronizer` 在有积压时选择当前队列中时间偏差最小且不超过阈值的一对，而不是接受第一对勉强合格的队首；被更近匹配越过的旧帧不会再次使用。它分别统计队列覆盖、超过时间阈值淘汰和被更近匹配取代三类丢帧，不能把它们合并解释为摄像头故障。累计配对/丢帧计数覆盖整个会话；平均、P95 和最大偏差基于最近 2048 个成功配对，避免长期运行时无界保存样本。这组统计通过 `PipelineStats.pose_sync` 暴露给界面与实验记录。

`LatestFrameScheduler` 对每个视角只保留一帧；同一视角的新帧会替换尚未推理的旧帧。两路同时就绪时交替选择，防止单路饥饿。`decode_movenet_singlepose` 按 COCO 17 点顺序把 MoveNet 的 `[y, x, confidence]` 输出转换为 POSE-FRAME。标准 192×192 `resize_with_pad` 路径由 `ResizeWithPadTransform` 将模型坐标还原为原始帧归一化坐标；黑边内预测的置信度置零。原始归一化坐标继续供骨架显示使用；计算角度和送入 PL 前，PS 使用源图宽高把两个轴缩放到同一物理像素尺度，避免 16:9 画面把关节角扭曲。

`OpenCVMoveNetPreprocessor` 是开发主机的像素预处理参考：只接收 `bgr8`，依次执行 BGR→RGB、双线性等比例缩放和零补边，返回 RGB uint8 图像及同一个 `ResizeWithPadTransform`。`PreprocessedMoveNetRunner` 把像素推理结果与本帧实际变换封装为 `MoveNetInference`，解码器优先消费这个显式变换，不再根据默认尺寸重复猜测。它不猜测 DPU 输入的 int8 零点和缩放因子；板端运行器必须从 VART 输入张量读取量化元数据后完成最后的数据类型转换。

原生 Lightning CNN worker 显式使用 `resize_before_color=True`（仅 pad）：先以同一 `INTER_LINEAR` 尺寸缩小 BGR，再转换 RGB 和零补边。默认参考与 stretch 顺序保持；该候选以 RGB/Q7 字节、变换及源 17 点严格一致为接受条件，实测边界见PS 运行时记录（原工程参考：`runtime_performance_20261023.md`）。

`load_movenet_vart_contract` 保留给旧的单输出 MoveNet 基线；它固定恰好一个 DPU 子图、一个 `[1,H,W,3]` 输入、一个 `[1,1,17,3]` 输出，并读取双方 `fix_point`。B3136 专用的四输出模型使用 `inspect_movenet_ntd_vart_runner`/`load_movenet_vart_ntd`，检查 `heatmap`、`center`、`reg`、`offset` 四个 HWC 张量，随后由 `decode_movenet_ntd_outputs` 还原 17 个 `[y,x,confidence]` 点。两条边界都不会从文件名猜输入量化，也不会把 `fix_point` 当成完整的像素归一化说明。

`VartMoveNetRunner` 已实现第二道边界：调用者显式提供 `模型值 = RGB uint8 × scale + offset`，运行器再按输入 `fix_point` 量化为 int8，通过 `execute_async`/`wait` 执行，并按输出 `fix_point` 反量化。它与 TFLite 路径共享 `PreprocessedMoveNetRunner`、坐标逆变换和后续会话；具体 scale/offset 仍必须由实际模型转换记录和静态图像对照确定。

`VartMoveNetNtdRunner` 使用 B3136 模型的真实参数：输入 prototxt 的
`mean=127.5`、`scale=0.00784314` 与输入 `fix_point=6` 对应
`模型值=(RGB-127.5)/127.5` 后量化为 int8；四个输出分别按各自 `fix_point` 乘
`2**(-fix_point)`。官方 Movenet C++ 实现采用 192×192 直接缩放，因此接入 NTD 时预处理
必须明确选择直接缩放；现有 TFLite 基线的等比例补边不能悄悄复用为同一模型语义。

`DualViewCoachPipeline` 把最新帧调度、可替换 `PoseEstimator` 和 `SquatSessionProcessor` 串成同步主机管线。`MoveNetPoseEstimator` 默认按 MoveNet Lightning 的 192×192 补边坐标还原；模型运行器负责实际的 BGR→RGB、缩放补边、数据类型转换和推理。只有明确返回原图归一化坐标的假后端或其他模型才关闭该还原。姿态估计器必须保留采集帧的视角、帧号和单调时间戳，避免双路同步使用错误身份。每个 `PipelineStep` 分别记录推理、动作会话和采集到结果的耗时；累计统计同时报告正/侧面覆盖丢帧、同步结果数与测量窗口处理 FPS。全会话累计值用于平均/最大延迟，三个近期 P95 使用默认 1024 帧的有界最近秩窗口，并同时暴露实际样本数和窗口容量。

主机端到端回归使用一组完整深蹲序列，把两路合成 CAPTURE-FRAME 依次送入 MoveNet 解码、最新帧调度、POSE-PAIR、批量 JOINT-METRICS 位精确参考后端、EXERCISE-EVENT 和中文反馈。该测试证明适配器之间的契约能够闭合，但不替代实体摄像头、DPU 推理或真实 DMA 的性能与功能验证。

JOINT-METRICS 的位精确主机契约定义在 `src/posepriv_coach/pl_metrics.py`，中文说明位于 `docs/ps_pl_joint_metrics.md`。PS 将一个同步对的正面/侧面关节合并成一次有序事务，保留每项帧号和置信度；PL 返回的关节编号、视角和最终帧尾用于关联确认。协议不使用哈希或数据签名。

`SquatSessionProcessor` 通过可替换的 `JointMetricBackend` 消费该接口。默认路径使用浮点几何；批量 PL 参考路径在一次调用中产生侧面跟踪角和正面对称性，并已经经过相同的双摄同步、动作状态机和评分会话测试。板端 DMA 只需实现等长字节传输边界，不改变上层事件契约。

`VerilatorAxisTransport` 实现同一个等长字节边界，可把负载发送给真实 Verilated AXI bridge；旧名称 `VerilatorJointMetricTransport` 仍作为兼容别名保留。它用于主机软件与 RTL 的跨语言 ABI 回归，验证算术和事务格式，不模拟 DDR、缓存一致性或板端 DMA 延迟。

`CoachSessionReportWriter` 只写完整动作和会话结束摘要。报告头固定动作、规则集版本、关节指标后端和性能测量有效标志；每次动作保留双路帧号、同步偏差、时长、总分、覆盖率、置信度、全部分项及纠正建议；摘要分别保留总/正面/侧面处理 FPS、同步对率、推理/会话/端到端平均与近期 P95、端到端最大值、P95 样本窗口、采集数量和同步分类丢帧。它不写原始像素或关键点坐标。关键点回放必须把性能测量标志设为假。

`CoachRuntime` 是薄应用编排层：从 `DualViewCoachPipeline` 逐项取出结果，将每个完整动作恰好写入一次会话报告，再调用可选界面回调；`on_synced_pair` 只在同步器真正产生一对姿态时触发，可直接绑定 `AvatarRuntime.process_pair`，不会把两个独立到达的单路帧误拼成数字人。`drain()` 只处理当前已经就绪的最新帧，不自行建立队列。会话结束后由同一入口写入管线与采集摘要。

`CoachDisplayModel` 将管线步骤和累计统计转换为渲染无关的 `CoachDisplayState`。单路帧到达但尚未形成新同步对时保持上一份有效动作状态；未曾同步、关键点不足和采集故障使用不同中文状态。`format_display_lines_zh` 提供终端基线，OpenCV/HDMI 渲染器必须消费同一状态，不能重新计算评分。

实时摄像头、双路录像和去像素关键点回放现在都通过 `CoachRuntime.on_synced_pair` 把同一份
`SyncedPosePair` 交给 `AvatarRuntime`。录像入口启用 `--avatar-calibration` 后会把
`avatar_runtime`、关节滤波、显示计时以及可用的 VART 计时写入同一会话报告；因此离线调试
不会另建一套三角测量、蒙皮或显示状态机。三种入口还共用 `load_avatar_runtime_setup()`，
确保标定加载、网格选择、时序滤波及蒙皮后端名称的含义完全一致。

完整人体离线转换候选遵守上述16槽与Q1.14/Q0.15接口，既有11骨段可在视频预览中
增加左右掌到13段，使用共同世界绑定空间加每骨inverse-bind。掌段是展示约束，不
增加原POSE-FRAME的观测或评分点。头段只转向、不按鼻-颈估计距离缩放头颅，见0068（原工程参考：`decisions/0068-independent-hands-and-rigid-head.md`）。
个人profile在应用中只用于配色和比例，不把私人人脸或衣服照片
贴到肢体；外部候选由 `--avatar-mesh` 显式选择，录像comparison也可不带标定使用。
纹理计算RTL另为未集成候选，接口、四级valid/ready/last、非法shade和整数舍入契约
见0066（原工程参考：`decisions/0066-avatar-texture-hardware-candidate.md`），尚无现役DMA操作码。


0070/0071新增独立渲染候选接口（未分配现役DMA操作码）：

| 接口 | 版本 | 契约与当前范围 |
| --- | --- | --- |
| AVATAR-RASTER-TEXTURE | 0.1 | 三角形32×32以内bbox、三边有符号增量与Q16 UV平面；256×512 RGB8四bank纹理；输出RGB/tag和每三角形独立结束marker，valid/ready反压；范围/上传错误见0070（原工程参考：`decisions/0070-raster-texture-pipeline.md`）。软件黄金、RTL和K26独立OOC通过，未部署新PL。 |
| AVATAR-TILE-RGBX | 0.1 | 32×32 RGB8，12bit全局origin、背景清除、按序覆盖；finish后256拍128bit输出，每拍四个RGBX8888、低字节R/X=0、行优先，只有最终拍last；粘滞error固定到末拍。反压、复位、越界和纹理互斥见0071（原工程参考：`decisions/0071-tile-framebuffer.md`）。尚非既有DMA/视频TLAST ABI，无Z缓冲/混合。 |

AVATAR-RASTER-PACKET 0.1为0070/0071的独立128bit包前端：PXT1请求支持固定纹理上传、
N个五拍三角形命令和tile背景/原点；PXR1响应含状态和0/256拍像素。格式错误排空到
TLAST并废弃纹理，正常tile复用静态纹理；输出反压期间全部字段稳定，复位废弃事务。
位域与状态见0072（原工程参考：`decisions/0072-textured-tile-packet.md`）。PS允许任意1..4096视口，
边缘tile补齐/裁剪；包RTL及真实模型三帧全tile字节对照、独立K26 OOC通过。
现役PPS2仍仅0..3，尚未把该包分配给共享DMA算子或完成板端验证。

SHARED-POSE-AXIS v3/PPS3（候选）：`INCLUDE_RASTER=1`新增算子4=PXT1纹理tile；默认0
仍PPS2四算子。0x00c能力0x1f、0x004新增texture_valid[2]，配置锁到最终输出握手；
纹理跨旧算子切换驻留，旧0..3字节协议不变。新BD模式使用20bit DMA长度。PPS3
新驱动仅目标编译，尚未部署；现役旧PPS2模块会拒绝此ID。完整契约见0073（原工程参考：`decisions/0073-shared-raster-candidate.md`）。

PPS3 Linux显式入口沿用原子32字节copy ioctl；TX上限2^20-16、RX128KiB，旧ID容量
保持。exchange_raster_packet与旧0..3入口共用同fd/锁；内核与Python分别验证长度、
符号范围、成功响应和RGBX padding。模块仅ARM编译/CPU测试通过，尚无新真实DMA。
见0074（原工程参考：`decisions/0074-pps3-linux-transport.md`）。

10/24后续：PPS3已启动并完成真实混合算子与三帧整图字节验证；上述“未部署/未运行”
为接口形成时的阶段状态，最新边界见板端功能记录（原工程参考：`pps3_board_validation_20261024.md`）
及新服装模型验证（原工程参考：`garment_avatar_20261024.md`），不据功能对照外推实时性能。

AVATAR-MESH-ASSET兼容扩展：可选boolean `soft_lighting`缺省false，仅控制PS显示光照。
true时按变形几何的面积加权顶点法线计算每面光照均值/环境光/gamma近似；不得与旧PL
flat lighting backend混用。固定点顶点、13/16矩阵槽、蒙皮批次与PXT1/PXR1 wire ABI不变，
PPS3继续接收每面0..256 shade。旧资产序列化在false时不增加字段，见0077（原工程参考：`decisions/0077-recorded-garment-avatar.md`）。

NATIVE-RASTER-CHECK v1：显式CPU库导出`posepriv_raster_validator_abi()=1`和
`posepriv_raster_validate_v1(const unsigned char *,uint32_t bytes)`，无效返回0，合法返回
16或4112。借用不可变bytes、完整长度和原契约；transport显式注入请求validator，
默认Python保持，旧算子/响应校验/32字节ioctl/故障状态不变，见0078（原工程参考：`decisions/0078-native-raster-preflight.md`）。

共享DMA内核缓存优化不改ABI：CPU cached包缓冲不得交DMA或作为coherent内存的缓存
别名；TX必须校验同一快照后再复制到原coherent，RX必须在完成/dma_rmb后复制再检查。
原长度/低2GiB/锁/故障生命周期不变，见0079（原工程参考：`decisions/0079-cached-kernel-payload.md`）。

EXERCISE-SCORE/EVENT过程扩展（0080）：旧配置/旧得分保持，显式process_time_v1配置
启用阶段适用时间汇总。ExerciseScore可选aggregation（不可变dataclass）保留算法、时间
窗口/参数、时间覆盖/无效原因、分项阶段/方向统计；coverage_percent仍是权重覆盖。
ScoreComponent.observed_value是有效区间上的原始时间均值，不能把它当过程分的唯一输入；
得分使用逐样本分数的积分/底部时间中位数。阶段具体范围在aggregation.metrics.phases。
SquatSessionUpdate追加scoring_algorithm/live_cues，完成cues可追加sustained_seconds/phases。
实时提示不触发完成TTS，结果保持沿用0076。JSONL容器schema1增加可选score.aggregation，
旧报告缺该字段视为旧算法；比较器检查版本/参数与聚合统计。详见
0080（原工程参考：`decisions/0080-process-squat-scoring.md`），硬件数据面未改。

AVATAR-FRAME-PAINTER v0.1（0081）：比较界面可借用同步`(BGR8 canvas, panel_rect, frozen
AvatarDisplayState)`回调，与CPU triangle/texture/lighting路径互斥。PPS3 consumer复用
投影/面色/稳定面序，只在所有tile成功后写数字人ROI；失效不重试/不输出半帧。
SharedRasterFrameRenderer借用transport，由调用方保证PPS3身份与独占op4纹理所有权，
缓存内容变化时上传，0..3旧操作不清纹理。返回不可变RGBX及准备/交换/拼图计时；
启用独立软件黄金时外层exchange包含黄金，只有actual_exchange_ns排除黄金，不称实时FPS。
当前对比回调只接无照片纹理角色，默认CPU路径保留。见
0081（原工程参考：`decisions/0081-shared-raster-display-consumer.md`）。

FLAT-TILE-PLAN ABI1（0082）：CPU库输入有序N×9 int32（signed16 XY六项、材质texel XY、
Q8 shade），返回私有job的连续PXT1包/offsets/命令数，copy后显式free；不接照片纹理。
Python NativeFlatTilePlanner保留稳定排序、严格整数输入，C两遍过程使用私有快照，
每tile<=13106命令、整帧默认64MiB容量，失败不发布job/不发DMA。消费者可显式借用
packet_planner，默认原参考，输出为同一TilePacketPlan；包、驱动、PL接口不变。
见0082（原工程参考：`decisions/0082-native-flat-tile-planner.md`）。

SharedRasterAvatarPainter显式`flat_rows=True`可直接准备既有平面材质规划器ABI1行，
要求借用renderer具有原生`plan_rows`。稳定深度序、坐标舍入、最终面色和调色板首见顺序
不变；不创建第二组ProjectedTriangle。默认路径、全部成功才写画布及故障锁定保持。
`render_flat_rows`只接受完整不可变材质纹理，原生规划器私有复制/范围检查后才发包，
不自动回退。见0091（原工程参考：`decisions/0091-flat-row-display-preparation.md`）。

AVATAR-DISPLAY显式`resolve_batch()`可返回ProjectedTriangleBatch Sequence：私有bytes、
固定68字节记录，完整深度/光照/XY/padding校验，XY为-2^30,2^30)，无照片纹理。
可按需索引为原ProjectedTriangle；native flat rows直接读只读view，保持原面序/灯光/
元数据。默认tuple及snapshot不变；比较器需显式batch-capable painter，见
[0092（原工程参考：`decisions/0092-batched-projected-triangles.md`）。

GLES-AVATAR实验（非默认）：实际PPS4 op5的16-byte stride XYZ→GL_SHORT，uint16源网格
和matrix_valid控制可见性；GPU处理视图、顶点法线、深度、视频和字形，不重算蒙皮位置。
两个独立位置/索引/视频缓冲组限制在途工作，glFinish分组/最终排空计完成吞吐；新算法
不声称旧op4像素等价。X11窗口使用EGL原生visual，独立资源关闭，评分保留全部原始
观测。依赖/边界见0093（原工程参考：`decisions/0093-mali-avatar-generation-candidate.md`）。

GLES-AVATAR视频格式候选（0129）：显式`--rgba-video`只将既有BGR末端转换与纹理
上传改为RGBA8；可见RGB、点位、旋转/黑边和缓存所有权保持，alpha恒255，缺路为
不透明黑色。默认RGB、背景/字形、GPU着色器及PL/DMA包不变；见
0129（原工程参考：`decisions/0129-native-image-pipeline-candidates.md`）。

PREVIEW-CONTINUITY显式候选：对pose_preview计划最多100ms历史有效骨段保持，当前
父关节有限有效且位移≤0.1展示单位才跟随平移；不延长真实观察时间、不合成缺失父关节，
超时/大位移/定点越界放弃。返回observed_plan、render_plan和held/expired槽；正常有效
矩阵与源pair不变，评分不可消费render_plan。默认路径不变，见
0094（原工程参考：`decisions/0094-preview-mesh-continuity.md`）。

EXERCISE-SCORE可选候选`depth_angle_time_v1`（默认不启用）：沿用四分项和80%覆盖协议，
躯干项按侧面深度比和区间左端阶段查询分段线性角度允许带；两输入均可信且在模型域内
才有效。SESSION-REPORT的aggregation显式保存`depth_angle_model`、原始`phase_seconds`及
互斥主因的`depth_angle_invalid_seconds`，旧算法序列化不增加这些键。完成躯干分项和阶段
ideal_min/max为模型范围包络，原始平均角度不是得分输入；实时分项保存当前条件边界。
模型参数比较严格相等，不使用观测容差。见候选契约（原工程参考：`decisions/depth-angle-scoring-candidate-20261025.md`）。

成形评分v1显式配置`side_metric_policy=per_metric_confidence_v1`：深度只比较各侧实际
髋/膝/踝的最小置信度，躯干只比较肩/髋；几何无效链不选、同分先左，不参考分数或
允许带选侧，不插补、不改变跟踪。过程报告保存非默认`side_metric_policy`，比较器严格
区分；默认`legacy_chain_sum_v0`不新增该键。离线四臂实验与独立HTML/JSON报告复用同一
会话链，包含派生动作指标但无像素/关键点坐标。见评分v1使用说明（原工程参考：`depth_angle_scoring_v1.md`）。

NATIVE-REPLAY实验：私有清单`pose_trace/raw_indexed`两种章，双摄轨迹按两源时间最大值
ready作因果CFR，保留原front/side时间与源ID；原片不补姿态/评分。换章重置规划器、
深度/骨段历史和评分，完整原始对只消费一次。报告分别给出各章GPU完成率，见0095（原工程参考：`decisions/0095-native-recording-playlist.md`）。

PREVIEW-DEPTH显式`depth_stability=True`：0.5进入/0.35持续的侧点置信度滞回，最多100ms
原时间Z输入保持，前视当前点缺失不补点；>250ms间隔/尺寸/前向变化清历史。仅显示，
原始pair/评分不变，见0096（原工程参考：`decisions/0096-preview-depth-stability.md`）。

PREVIEW-SCORE-PRESENTATION：原始完成结果对象/有效性和次数保持，常规阶段确认120ms、
提示阅读1200ms、指标采样250ms；无效立即标注，媒体时间距最近双摄ready>500ms清当前
指导。原片和换章清空，上次结果明确历史语义，见0097（原工程参考：`decisions/0097-stable-preview-score-presentation.md`）。

SCORE-PROCESS实验协议v1：READY协商版本；本机继承FD仅连接自家worker，最多一个
pending(chapter_id, chapter_slot, local_frame)。父/子在评分前拒绝同id换slot或回退帧；
每次完整消费到当前原始对，收到匹配的原CoachDisplayState后才画同帧反馈。原片None、
新章重置；错误不回旧状态，关闭仅影响自家child。默认串行，见0105（原工程参考：`decisions/0105-parallel-score-process-candidate.md`）。

GLES窗口实验显式`--msaa-samples {0,4,16}`：按EGL采样数/采样缓冲精确选择，当前
GL_SAMPLES回读必须与请求相同；报告保存实际RGBA/depth/visual/config与采样数。
仅window支持；省略保留旧首项选择，不静默切换。改变抗锯齿会改变边缘像素，不能声称
GPU像素等价；不改变源输入/评分/PL算术或网格。见0119（原工程参考：`decisions/0119-explicit-native-msaa-selection.md`）。

NATIVE-REPLAY可选`--record-range START:STOP`只限制读回/编码帧，全部前滚和后续状态仍执行；
需record-video、合法半开区间且不超过总frames。报告分别保存总frames、recorded_frame_indices
和encoded_video_frames；有录制的完成时间包含区间内编码，不当无编码性能。有效头部另存
12项head_matrix_q1_14及可选head_target_error_rad，原始几何仅留私有build，见0121。

PREVIEW-HEAD显式head-stability更新0122：颈根有效时短时缺鼻可保留最近完整有效方向
最多100ms，缺鼻仍原样缺失，缺失/重复帧不刷新有效方向时刻；缺颈/退化/超时清历史。
恢复使用当前锚点/长度和原4rad/s约束，计次评分不消费这些显示关节；默认仍关闭。

PREVIEW-CORE-SUPPORT显式core_view_support（0123，默认关闭）：仅当前4个正面肩/髋
confidence>=0.30且几何在工程范围、缺门限点的当前侧面同名点及双髋>=0.50、前向
符号已知且display-ready<=100ms时原子加入当前原坐标；不补缺坐标、不升confidence。
build需显式display_timestamp_ns，原trusted tracked/joint_filter/depth_tracker独立，
新增点用独立同参数滤波及当前侧深度，不进入原始评分。native报告core_supported_joints、
core_support_reason/age与模式；候选observed_valid按新显示接受规则，不能当高置信测量。

PREVIEW-HEAD-FACE-AXIS显式head_face_axis（0124，默认关闭）：与head_depth互斥，仅
当前颈/鼻存在且侧鼻/同侧耳眼可信时用实际宽高比的耳眼方向估计显示俯仰；前向符号
已知、前向距离/长边>=0.015、角度<=70度，两侧同时有效须相差<=20度。build需显示
时刻且0<=display-ready<=100ms。保留鼻颈长度、正面X分量及confidence，只改显示
nose/head，原pair/身体/评分不变。无当前支持则原路径，不存脸点历史；可单独启用
head_stability限速。native另报head_face_axis模式和reason/sides/age_ns/pitch_rad，
不当标定头姿或精确角度测量。

PREVIEW-NATIVE-AFFINE显式`--native-affine`（0128，默认Python）：加载当前包
runtime/libposepriv_affine.so的CPU整数ABI1；3×4 signed16输入、int64乘加、半LSB离零
舍入、signed16越界原子拒绝。仅内置int不可变矩阵走原生，其他旧类型沿原算术；
缺库/ABI错误不当成原生成功。planner显式affine_composer注入，组合仍在原try内，
成功后才更新轴历史；原观测/模型/评分和DMA ABI不改。主调用为CPython tuple扩展，
加载前核对CPython主/次版本；用目标Python运行scripts/build_native_affine.py构建。
旧ctypes数组入口保留为底层C ABI测试，主调用不再分配这些数组或缓存右矩阵。
此为CPU准备优化，不是新PL算子。

WORLD-UPRIGHT-DISPLAY-REFERENCE1（0171）：仅显式WorldAvatarPreview.upright_reference
或export_world_avatar_preview的--upright-start/--upright-frames启用。调用者选择已审看
正立窗口，至少3张同相机/尺寸/旋转的独立肩髋可信原图；同图换时钟/坐标拒绝，
用单位躯干方向的分量中位数归一化拟合。固定最小旋转将其对齐显示+Y，不是外参/
重力或人体真值；各相机单独拟合，不能自动把片头当正立。只绕当前展示骨盆左乘
整人D，原Q1.14矩阵/所有源状态/头部相对姿态/评分不改，无新时间滤波；原canonical
neutral保持，重复图/录像保持不重复套旋转。报告保存原窗口、up_direction和独立源数。
显式apply_world_upright_reference可处理已验证缓存：原float32 D定义输入边界，组合
后仍写float32，M/states复制原字节；参考报告需同章/同相机几何，已带参考的缓存
拒绝再应用。它不解码原片、不重新推理/求体架。不能将这一路与原CLI内部float64
D的末位差当作跨平台字节容差；主机/ARM必须对同输入严格比较。

OFFLINE-TEMPORAL-POSE-REFERENCE1（0170）：显式主机工具读取一章/一相机/同一旋转的
schema1 offline_world_pose_reference二维记录，固定实际宽高，按原图身份去重；同图
换坐标/时钟、混相机/尺寸或非有限置信拒绝。缺人物/源间隔>100ms切段，最多243个
真实源/121步进，不重采样/padding；各窗还原crop尺度后混合估计。输出H36M17根相对
像素等效XYZ及同图预测XY＋学习Z数组，顺序为髋、右髋膝踝、左髋膝踝、脊柱、胸、
鼻、头代理、左肩肘腕、右肩肘腕；0/7/8/10为派生，头代理为眼耳中点。不是原33点、
标定米制3D或observed stereo。无人物数组NaN且元数据available=false；源/窗口文件
保留frame_id/timestamp、参与窗口、上下文起止/未来量和模型入口置信。全量推断不接
原评分/HDMI；非因果未来上下文不能当live零延迟。上游/检查点由调用者显式提供，
strict加载仅验架构，不据shape断言权重身份；默认应用不加载Torch/连接设备。

NATIVE-IMAGE-LOOKAHEAD0.1：显式`--image-lookahead {1,2}`，默认1保留原调度，2需
`--prefetch-images --parallel-images`。每视角单worker按同一work顺序调用原图像函数，
当前两路future均成功领取后才前移队列，最多两对未来图像加当前消费者图像；不提前
计算评分/姿态/蒙皮。换章/缺路缓存语义和原RGBA保持。失败不复用旧图，退出先join
worker再关闭reader/文件/原生缩放计划。报告image_lookahead和原重叠计时边界，见0139。

CURRENT-HEAD-CUE0.1（0197/0198）：preview_head_current_axes和preview_head_cue_hold均为
严格bool、默认false。前者只将head yaw的横轴输入改为同pair raw眼/耳关系，不改
head-up/pitch及原可见/跨度/一致/80°门限或3Hz/2rad/s响应。调用者须紧随planner
build，以原plan对象调用current_face_source；所得FaceAxisSource冻结原双ID/ns/
尺寸、均值clock与原side-forward sign。下一build（含失败）撤销旧plan关联。
rig核对snapshot与当前pair，缺失/错配/未知sign清cue并走原躯干参考，不借旧held。

后者仅在合格cue短缺时保持body-relative target最多100ms（包含边界），期限从
max原source ns计算；重复帧/更晚display调用不续期，新cue立即优先。到期沿原响应
回躯干，reset/neutral/rejected/>250ms源间隔清除；不延长body/source ready。模式
held_face_lateral为display inference：只head槽与head segment/独占nose子点降置信，
tracked转partial，共享neck与其他joint/segment/M/D/原source/score保持。snapshot在
同CPU owner中现建现用，现geometry IPC只继续传最终矩阵/推断槽/完整评分，不新增
图像、网络、DPU或DMA所有者。该横轴仍是未标定preview，不是人体三维真值。

PREVIEW-FINITE-ARM-REACH0.1（0203）：严格bool `finite_arm_reach`，配置键
`preview_finite_arm_reach`，缺省false。仅手臂外可达域改为固定diag(4,4,1)度量的
最近球内点；球内原样，球外32次有界KKT二分。原内半径/肘平面/参考恢复/手掌
仍执行。度量轴是preview世界XY/Z，不保证身体局部前后符号，也不是真实标定。
source/observed/评分、非臂矩阵、原有效位/推断类别和D不变，无新硬件ABI。

PREVIEW-CURRENT-LEG-RECOVERY0.1（0205）：严格bool `leg_recovery`，配置键
`preview_leg_recovery`，缺省false。匹配当前pair的FaceAxisSource也为腿提供冻结的
forward sign（即使头部开关关闭，CPU owner也须在本次build后构造）。只对不完整
render腿chain，以当前三点front>=.30、profile>=.50、有限图内、双源年龄0..100ms
原子建立独立driver；退化拒绝，原source/observed/评分不改，输出腿始终推断。
腿参考/恢复和body恢复的两方向历史在最终display坐标内，以原6Hz/8rad/s跟随，
经原D逆变换重建固定长腿，不改D。neutral真实显示的参考腿只作inferred种子；held
不推进，reset/>250ms源间断/rejected清除，重复clock不推进。非腿矩阵/原最终valid
保持。它不新增地面、接触约束、真实3D观测或评分样本；进程IPC仍只传现有结果。

AVATAR-WIDTH-MORPH0.1（0204）：离线显式六参数head/neck/shoulder/waist/arm/leg_width，
完整13骨共同绑定，数值范围[.7,1.3]；只变rest顶点，沿用原Q1.14舍入/溢出契约，
拒绝新增量化退化面。全1原样返回，骨/绑定/索引/权重/拓扑/颜色/材质均不变。
不从个人profile猜缺失参数，不把宽度字段当骨长，运行时不拟合或扫描人体。

PREVIEW-DISPATCHER-SKINNING0.1（0207）：严格bool preview_skinning_pipeline，缺省false；
仅现有HDMI/inference process/geometry process组合可开，与geometry thread互斥。
CPU child/RPC不变，父进程geometry dispatcher单owner执行program+完整PPS4，再发布
原单READY项。新prepared同时持源/图像/矩阵/D/完整score及独立immutable bytes的
PackedSkinningOutputBatch。take后下一帧可用设备，但不能覆写旧输出或GL buffers；
所有EGL/GPU确认仍在主线程。保持8192分批、500ms超期、错误不重试及cancel/join
先于资源释放。skin按实际execute计，ready等待与renderer overlay另计，不当PL算子时延。

PRESENTATION-TRACE0.1（0207）：仅benchmark_presentations is True时分配，默认4096。
benchmark_presentation_capacity须builtin int 64..65536；600秒候选显式32768，默认
实时不分配。原presentation_samples列表/clock/source语义不变，新增presentation_trace
capacity/appended/retained/dropped/truncated；截断或retained不等确认总数不作完整长测。

NATIVE-RIG-MATH0.1（0209，默认Python）：preview_rig_math_library为显式非空路径字符串
或None，加载ABI1及同CPython主次版本的CPU扩展。rig的math_builder只替换该实例的
matmul/canonical，不改模块函数或其他owner；geometry process在child加载，IPC不变。
仅exact ndarray/native float64/3或3×3/有限绝对值≤1e6接受，其他值调用原Python
oracle。三个乘积以math.fsum保持原顺序，无FMA；规范网格保持2^-24、ties-to-even和
正零，输出独立。未改源/评分/历史/矩阵/D/硬件ABI；rig_math_backend分别报告python
或native_cpu_exact_small_math。组件数值通过不代表整链收益，缺库/ABI错误明确失败。

MODEL-READY-OVERLAP0.1（0210，默认False）：严格bool preview_model_ready_overlap，仅
显式inference-process/prefetch/demand/paired MoveNet/decoded-arena组合允许True。
只放开模型开始前的READY等待，decoder仍单READY背压，原单模型/三槽/至多一个READY
不变。发布替换旧READY并计superseded，READING绝不覆写；像素/姿态/源整组及原
错误/取消/关闭保持。inference_pipeline.model_ready_overlap单列实际选项。
配对路径可另存decode_ready_ns（两解码完成最大值）及model_begin_ns（原paired模型
调用开始，含后续预处理，非纯DPU开始），穿过原metadata及有界presentation trace。
二者是同帧monotonic整数诊断，不改source、GPU确认或已有计数语义。

SHARED-SKINNING-BACKEND0.1（0211）：SKINNING_RESOURCE_SHARED默认1，显式0只选择
既有并行核，不改变PPS4 op5包、Q格式或共享尾包锁。原shared_pose_skinning_dma为
串行，新shared_pose_skinning_parallel_dma才设0；包装--parallel-skinning要求
--skinning与真实HWH参数0。HWH缺字段只可兼容旧串行；清单如实记录False。
板端verify/audit/selector仅接纳严格JSON bool的两种后端，其他PPS4/boot绑定保持；
元数据通过不等于候选板端合格。定向/完整仿真、联合实现、导出/启动及整链
板测分别成立；不由仿真II或独立资源宣称现役速度提升。

NATIVE-UPPER64-SCAN0.1（0212，默认False）：严格bool `preview_native_upper64_scan`，
仅显式shared DMA/native validator的preview实例可开。既有ABI1库新增可选symbol
`posepriv_skinning_upper64_zero_v1(const unsigned char*,size_t)`；默认不解析新symbol，
显式开启在打开DMA前要求存在。每次完整扫描16B stride的upper64，空合法、截尾拒绝，
不缓存；完整transport/adapter/driver的bytes6–15校验继续执行。原类
PackedSkinningOutputBatch以keyword-only InitVar注入checker，仅exact bytes走native，
其他buffer/subclass保持原Python语义；存储字段、repr/equality、对象identity、序列和
源/画质/AXI/ownership均不变。checker只属于该backend，不全局替换。CPU语义测试不
代表ARM或整链收益；新库构建/ARM黄金/同源实际A/B由独立阶段验证，现役默认不改。


FULL33-HEAD4-S16-0.1：冷配置groups1..144，四PE各八signed16项／组，共32乘法器。
W为PE-major/group-minor的128bit词；四路bias48/shift5可乱序加载但必须齐备。
连续特征组II1；first清累加、last形成四个rawS48，bias显式49位，加正半值后算术
右移，最后S16饱和。raw192/post64/tag16共用valid/ready，反压稳定；配置等整个
流水空。只清valid/control不清权重RAM。195输出49chunk，最后3有效+1dummy；
16bit tag不是物理源ID/ns。独立PPA及I/O时序范围见full33_head4_20261030.md。

FULL33-INT8-RESIDUAL-0.1：两个128bit operand与pixel tag原子入队，四通道复用II4。
signed8扩S16后按独立Qa/Qb/Qout做Add+Relu6，7bit输出limit不大于127；只实现
Relu6分区，不是通用nonRelu S8截断。输出完整16byte/pixel tag/job tag共同保持。
冷配置等所有记录排空，外部任务计数并等DDR最终B；原PTQ三组Q/limit不作为新
训练图的默认。没有帧长端口，不能用datapath empty替代任务完成。


FULL33-HEAD4-AXI-0.1：一个冷命令锁存A/W/post/dst四64bit设备DMA地址、source_s8、
source_q7和job tag。A16固定Q13，S8按Q6<<7或Q7<<6精确扩展；A16忽略q7位。
A仅加载一次144词cache、49chunk重放；W为原HWI顺序，不是CHW重排的整图常量。
195输出写一次25×128bit、最后keep003f，必须分配400字节跨度，390字节payload后
十字节保持。等待最终B及core/cache排空才success。busy cancel抑制成功，排空已发
总线后再局部flush；IDLE-only短cancel与新cmd并存时不取消新cmd。外部reset须与AXI
slave/interconnect共享。并行冷命令口尚无AXI-Lite映射；物理分配/缓存一致性归caller。


FULL33-HEAD4-AXI-0.2：继承0.1输入／恢复规则，新增必须由caller明确驱动的冷
xyz_only_i，随命令锁存。0保留195／49chunk／25词／390payload／400span／keep003f；
1为XYZ117／30chunk／15词／234payload／240span／keep03ff。117尾chunk1有效＋3dummy，
须与前chunk28四系数半包合并，六个尾guard字节保持。原confidence来自原195 DPU分支，
仅替换39×3 XYZ；不把117数据冒称完整195或改变原logit／world／评分算法语义。

PREVIEW-AVATAR-CAMERA-0.1（0219）：冷参数avatar_yaw_degrees，现场配置
preview_avatar_yaw_degrees，默认30°；投影与法线使用同一旋转，原−8°俯角／视口／
缩放／Q14／D／来源／评分保持。显式60°候选完成同源87帧实际PPS4／Mali。

CAMERA-NEUTRAL-HEAD-REFERENCE-0.1（0220）：显式WorldCameraHeadPitchReference及
fit_world_camera_head_pitch_reference，仅由≥3独立、同几何、已审平视中立图拟合；
以source-camera +Y水平为展示参考，不是物理重力／标定。只右乘固定局部逆pitch，再走
原头滤波。默认WorldHeadPitchReference仍为身体相对参考；新5ARM747条非head槽／D／
raw／评分严格一致、87帧PPS4／Mali完成，全部动作资格另列。

WORLD-ARM-DISPLAY-INFERENCE-0.1（0221）：默认关闭；已验证immutable世界点输入，
保留原source／normalized／confidence，只改显示副本的肘腕XYZ。当前身体局部可靠方向
优先；供体≤50ms，短时历史≤150ms，默认50ms阻尼。固定骨长、provenance区分当前／
供体／推断／不可用；调用者保证展示不早于任何供体。原raw及评分不回写。

WORLD-OUTPUT-BOUNDARIES-0.1（0222）：filtered_observation复用已验证source及normalized
tuple，新world只读并独立拥有XYZ；置信字段对象必须同源。WorldAvatarPreview的显式
inferred_metadata_builder接收(joints,segments)，返回confidence=0的(dict,tuple)，只作用
最终展示输出，原rig／滤波历史保持。NativeWorldMetadata用目标CPython冷绑定exact
frozen slot布局，热端不重复数值验证。默认原Python边界，构建须显式compiler／output。

WORLD-ARM-RECOVERY-0.1（0224）：RecoveryArmDisplayDriver默认关闭；只在短时预测后
重新取得current方向时融合，首current增益>0且默认≤0.35，50ms时间常数、最长150ms
源时间。复用原身体基底／当前肩／固定骨长；可靠模型历史不写入融合值。状态明确为
recovery_body_local；raw、评分、源对象、原置信与normalized tuple不变。正常可靠
动作没有恢复滤波。公共预测器的内部方向hook不改变默认预测策略。

WORLD-INITIAL-PLAN-0.1（0225）：WorldAvatarPreview.initial_plan_builder默认为None，
保留_initial_plan参考路径；NativeWorldPlan为目标CPython／NumPy的显式单调用边界。
在固定rig／framing／requests设置后绑定；几何设置变化时重建，普通reset保持。
支持现役0／33点及13骨拓扑，原source、浮点运算顺序、类型、Q14／D及rig／head／body
历史保持；外部输入验证不变。无需FPGA设备、驱动或新位流。

JPEG-PREVIEW-0.1（0227）：ThreadLocalJpegDecoder显式指定与目标libjpeg8匹配的库。
原生ABI1输入为本次不可变JPEG字节、scale、rotation；每线程独占持久handle，返回
当前帧独立拥有的连续BGR8数组。使用原准确IDCT/upsampling，EXIF/CMYK明确回原
OpenCV语义，坏JPEG报错；不跨帧复用输出。CapturedFrame的物理view、ID、整数ns、
尺寸及rotation保持。completion-first只在已有JPEG futures期间取当前已完成神经结果，
原单READY/两live pairs/有界credit、因果ROI和发布身份不变。

FULL33-RUNTIME-0.1（0228）：公开源码从board_runtime/full33和既有src/C/板控脚本装包；
权重/模型生成header、ELF、.ko、Python发行目录和授权mesh均为有来源许可的显式资产。
Windows只SSH控制。start在准备资产/命令后才交接明确previous-session-file，按现有
PID/start_ticks/boot/argv/UID及pidfd协作停止；rollback复用匹配活P4或同步两处session。
默认当前候选使用精确JPEG+completion-first，Recovery/yaw60另行显式选择。保留P4，
不安装固件或改变boot默认。源码/资产齐全、功能板测与完整Goal资格分别报告。

SHARED-SKINNING-EXT-0.1（2026-10-31）：独立32B ioctl扩展，原0x40205031范围/行为不变。
0x40205032只op5、1..16384顶点，512+16N输入/16N输出，独立256KiB coherent+cached RX；
0x40205033冷上传16N固定vertex（rx/timeout/reserved为0），0x40205034仅输入512B矩阵头，
按该exclusive fd已存N发送完整原op5包。每fd一个mesh；原命令实际覆盖TX前使其失效，
close/fault/remove清状态，无自动重传。新路径保留原RX/输出检查和同mutex/RX-first序列。
Full33仅preview_static_mesh=true显式选择，默认false，需已加载匹配新模块；不自动换模块、
不改PL/DT。见完整接口和实际结果（原工程参考：`static_mesh_dma_20261031.md`）。

JPEG-PREVIEW-NEON-0.1（2026-10-31）：JPEG ABI1不变；AArch64仅90/270°的完整8×8
三通道块使用精确byte transpose，奇数/尾块、180°及非AArch64保留原scalar。
原ISLOW/fancy/scale/错误/EXIF/CMYK、thread-local及owned output不变，无新依赖。
构建入口仅`--neon-rotation`设置`POSEPRIV_JPEG_NEON_ROTATE=1`，默认仍编译scalar。
4935 ARM旋转reference及8图×3旋转像素严格通过；CPU组件有收益，首轮library-only
45秒平均/源龄没有改善，因此不改当前库/default。见JPEG NEON结果（原工程参考：`jpeg_neon_rotation_20261031.md`）。

RIG-AXIS-BATCH-0.1（2026-10-31）：NativeRigTail的显式axis_batch默认False；
库须用build_native_rig_tail.py --axis-batch编译，并在同CPython冷边界核对能力和原
limb/native builder。尾核ABI1及13骨结果保持；共享limb_axis_math.inc供原入口与批处理
共同使用，保留首次／重获原分支、float64／sum／fsum／Q14／历史提交顺序。只减少
8四肢＋2手掌轴的Python／NumPy往返；不改raw／评分／过滤／模型／驱动／PL。
ARM组件8.824→7.630ms，整链尚待同配置验证，默认不变，见轴合批结果（原工程参考：`rig_axis_batch_20261031.md`）。
Full33冷配置`preview_rig_axis_batch=true`显式启用同一`preview_rig_tail_library`，
该库须以`--axis-batch`构建；未配置时保留原尾核和回调路径。


TWO-VIEW-DISPLAY-CAMERA-0.1（2026-10-31）：Full33 `preview_two_view_camera_profile`
默认null，显式schema `posepriv.two_view_display_camera_profile.v1`和enabled才安装。
profile冷绑定physical front/side的原视场、尺寸、旋转／未镜像、K/R及primary view，
坐标为rotated_full_frame与primary_camera_xyz_y_down，raw minimum_q固定0.5。
当前同源双视角只修显示world四臂，再进原唯一world稳定器；原source、normalized、
评分和其他骨保持，零长原始臂保留给原rig恢复。未知配置／源几何不匹配保持原输入。
同owner的prepare仍由原单owner顺序调用。3719冻结行接口全等、HOST/ARM各4项合成
测试通过；实时现场标定／600秒性能未由这些测试证明。见two-view说明（原工程参考：`two_view_display_geometry_20261031.md`）。
