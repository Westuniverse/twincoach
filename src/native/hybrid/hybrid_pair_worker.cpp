// SPDX-License-Identifier: GPL-2.0-only
// Component-only pair boundary. Reuse the exact qualified Full33 Backend/Slot
// and its prefix/cut copy + Tail35 raw extraction; do not instantiate its server.
#define main frozen_tail4_shm_worker_main
#include "../worker/tail4_shm_worker.cpp"
#undef main
#include <algorithm>
#include <future>
#include <sstream>

namespace {
constexpr char PAIR_MAGIC[]="PPHYPR01";
constexpr size_t LIGHT_INPUT=110592,LIGHT_OUTPUT=198144;
constexpr std::array<size_t,4>LIGHT_SIZES{2304,39168,78336,78336};
constexpr std::array<int,4>LIGHT_QS{1,2,3,1};
using Stamp=std::chrono::steady_clock;
uint64_t elapsed(Stamp::time_point begin) {
 return uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(Stamp::now()-begin).count());
}
void pair_record(Io&io,uint32_t kind,const std::vector<uint8_t>&payload) {
 std::array<uint8_t,16>h{};std::memcpy(h.data(),PAIR_MAGIC,8);store(h.data()+8,kind,4);store(h.data()+12,payload.size(),4);
 io.write(h.data(),h.size());io.write(payload.data(),payload.size());
}
void append_number(std::vector<uint8_t>&out,uint64_t value,size_t bytes) {
 const size_t pos=out.size();out.resize(pos+bytes);store(out.data()+pos,value,bytes);
}
void full_raw(std::vector<uint8_t>&out,const Slot&s) {
 out.insert(out.end(),s.head195.begin(),s.head195.end());
 out.insert(out.end(),s.head117.begin(),s.head117.end());
 out.insert(out.end(),s.head1.begin(),s.head1.end());
 out.insert(out.end(),s.heat.begin(),s.heat.end());
}
class LightningBackend {
 std::array<std::string,4>names_;
#ifndef TAIL4_COMBINED_MOCK
 std::unique_ptr<xir::Graph>graph_;
 std::unique_ptr<vart::Runner>runner_;
 std::vector<std::unique_ptr<vart::TensorBuffer>>ib_,ob_;
 std::vector<vart::TensorBuffer*>ip_,op_;
 std::array<size_t,4>indices_{};
#else
 std::vector<uint8_t>storage_;
#endif
 uint8_t*input_=nullptr;
public:
 explicit LightningBackend(char**argv) {
  for(size_t j=0;j<4;++j)names_[j]=argv[6+j];
#ifndef TAIL4_COMBINED_MOCK
  graph_=xir::Graph::deserialize(argv[5]);std::vector<const xir::Subgraph*>dpu;collect(graph_->get_root_subgraph(),dpu);
  require(dpu.size()==1,"Lightning needs exactly one actual DPU partition");
  runner_=vart::Runner::create_runner(dpu[0],"run");
  auto in=runner_->get_input_tensors(),out=runner_->get_output_tensors();
  require(in.size()==1&&out.size()==4,"Lightning actual input/output arity");
  check_tensor(in[0],"StatefulPartitionedCall/sub_1/aquant",{1,192,192,3},7,LIGHT_INPUT);
  for(size_t j=0;j<4;++j) {
   size_t found=4;for(size_t k=0;k<4;++k)if(out[k]->get_name()==argv[6+j]) {require(found==4,"duplicate Lightning head");found=k;}
   require(found!=4,"exact Lightning head name absent");indices_[j]=found;
   check_tensor(out[found],argv[6+j],{1,48,48,int(LIGHT_SIZES[j]/2304)},LIGHT_QS[j],LIGHT_SIZES[j]);
  }
  ib_=vart::alloc_cpu_flat_tensor_buffers(in);ob_=vart::alloc_cpu_flat_tensor_buffers(out);
  auto input=vart::get_tensor_buffer_data(ib_[0].get(),0);require(input.size==LIGHT_INPUT,"Lightning input allocation");
  input_=static_cast<uint8_t*>(input.data);
  for(auto&b:ib_)ip_.push_back(b.get());
  for(auto&b:ob_)op_.push_back(b.get());
#else
  (void)argv;storage_.resize(LIGHT_INPUT);input_=storage_.data();
#endif
 }
 std::string metadata()const {
  std::ostringstream out;out<<"{\"schema\":\"posepriv.cnn_worker.v1\",\"backend\":\"vart\",\"dtype\":\"int8\",\"layout\":\"NHWC\",\"input\":{\"name\":\"StatefulPartitionedCall/sub_1/aquant\",\"shape\":[1,192,192,3],\"fix_point\":7,\"bytes\":110592},\"heads\":[";
  const std::array<const char*,4>roles{"center","heatmap","offset","regression"};
  for(size_t j=0;j<4;++j) {if(j)out<<',';out<<"{\"role\":\""<<roles[j]<<"\",\"name\":\""<<names_[j]<<"\",\"shape\":[1,48,48,"<<LIGHT_SIZES[j]/2304<<"],\"fix_point\":"<<LIGHT_QS[j]<<",\"bytes\":"<<LIGHT_SIZES[j]<<'}';}
  out<<"]}";return out.str();
 }
 std::vector<uint8_t>run(const uint8_t*p) {
  std::memcpy(input_,p,LIGHT_INPUT);std::vector<uint8_t>result;result.reserve(LIGHT_OUTPUT);
#ifndef TAIL4_COMBINED_MOCK
  auto job=runner_->execute_async(ip_,op_);require(job.second==0&&runner_->wait(job.first,5000)==0,"Lightning DPU run failed");
  for(size_t j=0;j<4;++j) {
   auto b=vart::get_tensor_buffer_data(ob_[indices_[j]].get(),0);require(b.size==LIGHT_SIZES[j],"Lightning raw head size");
   auto*data=static_cast<uint8_t*>(b.data);result.insert(result.end(),data,data+b.size);
  }
#else
  for(size_t j=0;j<4;++j)for(size_t k=0;k<LIGHT_SIZES[j];++k)result.push_back(uint8_t(input_[(k*7+j*13)%LIGHT_INPUT]+j));
#endif
  return result;
 }
};
}

int main(int argc,char**argv) {
 try {
  std::signal(SIGPIPE,SIG_IGN);std::atomic<bool>failed{false};Io io(failed);
  require(argc==10,"usage: hybrid_pair_worker FULL.xmodel TAIL_DEVICE W P LIGHT.xmodel CENTER HEAT OFFSET REGRESSION");
  Backend full(argv);LightningBackend light(argv);
#ifndef TAIL4_COMBINED_MOCK
  const std::string backend="vart_full33_lightning_one_process";
#else
  const std::string backend="host_mock_no_NN";
#endif
  std::string meta="{\"schema\":\"posepriv.hybrid_pair_worker.v1\",\"backend\":\""+backend+
   "\",\"runner_creations\":2,\"tail_fd_opens\":1,\"native_slots\":2,\"model_kind\":{\"1\":\"side_full33_front_full33\",\"2\":\"side_full33_front_lightning\"},\"full_input_bytes\":196608,\"full_output_bytes\":160370,\"lightning_input_bytes\":110592,\"lightning_output_bytes\":198144,\"lightning_roles\":[\"center\",\"heatmap\",\"offset\",\"regression\"],\"lightning_Q\":[1,2,3,1]}";
  meta.pop_back();meta+=",\"lightning_metadata\":"+light.metadata()+",\"front_only_kind\":3}";
  pair_record(io,3,std::vector<uint8_t>(meta.begin(),meta.end()));
  uint64_t completed=0,full_calls=0,light_calls=0,tail_calls=0;
  for(;;) {
   std::array<uint8_t,16>header{};if(!io.read(header.data(),16,true))break;
   require(std::memcmp(header.data(),PAIR_MAGIC,8)==0&&number(header.data()+8,4)==1,"hybrid pair request header");
   const size_t length=size_t(number(header.data()+12,4));
   require(length==72+INPUT+INPUT||length==72+INPUT+LIGHT_INPUT||length==72+LIGHT_INPUT,"hybrid request byte bound");
   std::vector<uint8_t>request(length);io.read(request.data(),request.size());
   const uint32_t kind=uint32_t(number(request.data(),4));
   const size_t side_input=kind==3?0:INPUT;
   require((kind==1||kind==2||kind==3)&&length==72+side_input+(kind==1?INPUT:LIGHT_INPUT),"explicit pair model/length mismatch");
   const size_t front_input=kind==1?INPUT:LIGHT_INPUT;
   require(number(request.data()+8+16,4)==2&&number(request.data()+40+16,4)==1,"original side/front physical IDs required");
   require(number(request.data()+8+28,4)==side_input&&number(request.data()+40+28,4)==front_input,"input tensor model byte count");
   Slot side,front;std::copy_n(request.data()+8,32,side.identity.begin());std::copy_n(request.data()+40,32,front.identity.begin());
   const auto begin=Stamp::now();auto started=Stamp::now();uint64_t side_prefix=0;
   // Same prefix/Tail overlap as the qualified two-slot pipeline. A future's
   // destructor waits on exceptions, so an in-flight Tail is not abandoned.
   std::future<uint64_t>side_tail;
   if(kind!=3) {std::memcpy(full.input(),request.data()+72,INPUT);started=Stamp::now();full.prefix(side);side_prefix=elapsed(started);++full_calls;
    side_tail=std::async(std::launch::async,[&]{auto t=Stamp::now();full.tail(side);return elapsed(t);});}
   uint64_t front_prefix=0,front_tail=0;std::vector<uint8_t>light_raw;
   if(kind==1) {
    std::memcpy(full.input(),request.data()+72+side_input,INPUT);started=Stamp::now();full.prefix(front);front_prefix=elapsed(started);++full_calls;
   } else {started=Stamp::now();light_raw=light.run(request.data()+72+side_input);front_prefix=elapsed(started);++light_calls;}
   uint64_t side_tail_ns=0;if(side_tail.valid()) {side_tail_ns=side_tail.get();++tail_calls;}
   if(kind==1) {started=Stamp::now();full.tail(front);front_tail=elapsed(started);++tail_calls;}
   const auto pair_ns=elapsed(begin);
   std::vector<uint8_t>response(request.begin(),request.begin()+72);
   store(response.data()+8+28,kind==3?0:OUTPUT,4);store(response.data()+40+28,kind==1?OUTPUT:LIGHT_OUTPUT,4);
   for(uint64_t v:{side_prefix,side_tail_ns,front_prefix,front_tail,pair_ns})append_number(response,v,8);
   if(kind!=3)full_raw(response,side);
   if(kind==1)full_raw(response,front);else response.insert(response.end(),light_raw.begin(),light_raw.end());
   pair_record(io,2,response);++completed;
  }
  std::vector<uint8_t>footer;for(uint64_t v:{completed,full_calls,light_calls,tail_calls})append_number(footer,v,8);
  pair_record(io,4,footer);
  std::cerr<<"hybrid_pair completed="<<completed<<" full33="<<full_calls<<" lightning="<<light_calls<<" actual_tail35="<<tail_calls<<'\n';
  return 0;
 } catch(const std::exception&e) {std::cerr<<"hybrid_pair_worker: "<<e.what()<<'\n';return 3;}
}
