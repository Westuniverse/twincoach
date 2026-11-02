// Private memfd transport candidate; generated from the unchanged combined worker.
// SPDX-License-Identifier: GPL-2.0-only
// One prefix Runner + one tail fd, two owned slots and ordered duplex output.
#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <csignal>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <iostream>
#include <memory>
#include <mutex>
#include <poll.h>
#include <stdexcept>
#include <string>
#include <sys/eventfd.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <thread>
#include <unistd.h>
#include <vector>
#ifndef TAIL4_COMBINED_MOCK
#include <vart/runner.hpp>
#include <vart/runner_helper.hpp>
#include <xir/graph/graph.hpp>
#include <xir/graph/subgraph.hpp>
extern "C" {
#include "tail4_runtime.h"
}
#endif
namespace {
constexpr size_t INPUT=196608,OUTPUT=160370;
constexpr char MAGIC[]="PPT4SM01";
constexpr size_t RING_SLOTS=4,SLOT_STRIDE=360448,RING_BYTES=RING_SLOTS*SLOT_STRIDE;
constexpr char PARTITION[]="subgraph_MP_003_CONV_2D/Conv2D";
constexpr char INPUT_NAME[]="mp_pose_input/aquant";
constexpr std::array<const char*,3>NAMES{"MP_245_CONV_2D/BiasAdd/aquant","MP_246_MAX_POOL_2D/MaxPool2d/aquant","MP_233_CONV_2D/BiasAdd/aquant"};
constexpr std::array<size_t,3>SIZES{4608,4608,159744};
constexpr std::array<int,3>QS{4,4,2};
void require(bool ok,const char*msg) {if(!ok)throw std::runtime_error(msg);}
uint64_t number(const uint8_t*p,size_t n) {uint64_t v=0;for(size_t i=0;i<n;++i)v|=uint64_t(p[i])<<(i*8);return v;}
void store(uint8_t*p,uint64_t v,size_t n) {for(size_t i=0;i<n;++i)p[i]=uint8_t(v>>(i*8));}
enum class State {FREE,PREFIX,CUT_READY,TAIL,RESULT_READY,OUTPUT};
struct Slot {
 State state=State::FREE;
 uint64_t sequence=0;
 std::array<uint8_t,44>identity;
 std::array<uint8_t,4608>bias,pool;
 std::array<uint8_t,159744>heat;
 std::array<uint8_t,390>head195;
 std::array<uint8_t,234>head117;
 std::array<uint8_t,2>head1;
};
class Ring {
 uint8_t*data_=nullptr;int fd_=-1;
public:
 explicit Ring(const char*text) {
  char*end=nullptr;const long value=std::strtol(text,&end,10);
  require(end&&*end==0&&value>=0&&value<=2147483647L,"invalid inherited memfd");fd_=int(value);
  struct stat s{};require(::fstat(fd_,&s)==0&&s.st_size==static_cast<off_t>(RING_BYTES),"memfd tensor ring size mismatch");
  void*map=::mmap(nullptr,RING_BYTES,PROT_READ|PROT_WRITE,MAP_SHARED,fd_,0);
  require(map!=MAP_FAILED,"cannot mmap inherited tensor ring");data_=static_cast<uint8_t*>(map);
 }
 ~Ring() {if(data_)::munmap(data_,RING_BYTES);if(fd_>=0)::close(fd_);}
 void input(unsigned slot,uint8_t*destination)const {
  std::atomic_thread_fence(std::memory_order_acquire);
  std::memcpy(destination,data_+slot*SLOT_STRIDE,INPUT);
 }
 void output(unsigned slot,const Slot&s) {
  uint8_t*p=data_+slot*SLOT_STRIDE+INPUT;
  std::memcpy(p,s.head195.data(),390);p+=390;
  std::memcpy(p,s.head117.data(),234);p+=234;
  std::memcpy(p,s.head1.data(),2);p+=2;
  std::memcpy(p,s.heat.data(),159744);
  std::atomic_thread_fence(std::memory_order_release);
 }
};
class Io {
 std::atomic<bool>&failed_;
 int output_=-1,wake_=-1;
 void ready(int fd,short events) {
  for(;;) {
   if(failed_.load())throw std::runtime_error("pipeline aborted");
   pollfd p[2]={{fd,events,0},{wake_,POLLIN,0}};
   int ret=::poll(p,2,-1);if(ret<0&&errno==EINTR)continue;
   require(ret>=0,"pipe poll failed");
   if(p[1].revents&POLLIN)throw std::runtime_error("pipeline aborted");
   if(p[0].revents&(events|POLLHUP|POLLERR))return;
   require(!(p[0].revents&POLLNVAL),"invalid pipe descriptor");
  }
 }
public:
 explicit Io(std::atomic<bool>&failed):failed_(failed) {
  output_=::dup(STDOUT_FILENO);require(output_>=0,"cannot duplicate binary stdout");
  require(::dup2(STDERR_FILENO,STDOUT_FILENO)>=0,"cannot redirect library stdout");
  wake_=::eventfd(0,EFD_NONBLOCK|EFD_CLOEXEC);require(wake_>=0,"cannot create abort eventfd");
  for(int fd:{STDIN_FILENO,output_}) {int f=::fcntl(fd,F_GETFL);require(f>=0&&::fcntl(fd,F_SETFL,f|O_NONBLOCK)==0,"cannot configure nonblocking pipes");}
 }
 ~Io() {if(output_>=0)::close(output_);if(wake_>=0)::close(wake_);}
 void wake() noexcept {uint64_t one=1;const auto ignored=::write(wake_,&one,sizeof(one));(void)ignored;}
 bool read(uint8_t*p,size_t n,bool boundary=false) {
  size_t got=0;
  while(got<n) {
   if(failed_.load())throw std::runtime_error("pipeline aborted");
   auto k=::read(STDIN_FILENO,p+got,n-got);
   if(k>0) {got+=size_t(k);continue;}
   if(k==0) {if(boundary&&got==0)return false;throw std::runtime_error("truncated request");}
   if(errno==EINTR)continue;
   if(errno==EAGAIN||errno==EWOULDBLOCK) {ready(STDIN_FILENO,POLLIN);continue;}
   throw std::runtime_error("input pipe read failed");
  }
  return true;
 }
 void write(const uint8_t*p,size_t n) {
  while(n) {
   if(failed_.load())throw std::runtime_error("pipeline aborted");
   auto k=::write(output_,p,n);
   if(k>0) {p+=k;n-=size_t(k);continue;}
   if(k<0&&errno==EINTR)continue;
   if(k<0&&(errno==EAGAIN||errno==EWOULDBLOCK)) {ready(output_,POLLOUT);continue;}
   throw std::runtime_error("binary output pipe failed");
  }
 }
 void header(uint32_t kind,uint32_t bytes) {
  std::array<uint8_t,16>h{};std::memcpy(h.data(),MAGIC,8);store(h.data()+8,kind,4);store(h.data()+12,bytes,4);write(h.data(),h.size());
 }
};
#ifndef TAIL4_COMBINED_MOCK
void collect(const xir::Subgraph*sg,std::vector<const xir::Subgraph*>&out) {
 if(sg->has_attr("device")&&sg->get_attr<std::string>("device")=="DPU") {out.push_back(sg);return;}
 for(auto*child:sg->get_children())collect(child,out);
}
void check_tensor(const xir::Tensor*t,const char*name,const std::vector<int>&shape,int q,size_t bytes) {
 auto dt=t->get_data_type();require(t->get_name()==name,"tensor name mismatch");
 require(dt.type==xir::DataType::XINT&&dt.bit_width==8,"tensor must be signed int8");
 require(t->get_shape()==shape&&t->get_data_size()==static_cast<int32_t>(bytes),"tensor shape/bytes mismatch");
 require(t->has_attr("fix_point")&&t->get_attr<int32_t>("fix_point")==q,"tensor Q mismatch");
}
void check_environment() {
 const char*firmware=std::getenv("XLNX_VART_FIRMWARE"),*disable=std::getenv("XLNX_DISABLE_LOAD_XCLBIN"),*library=std::getenv("LD_LIBRARY_PATH");
 require(firmware&&*firmware,"set XLNX_VART_FIRMWARE explicitly before Runner creation");
 const std::string path(firmware),suffix="/posepriv-dpu-isolation.xclbin";
 require(path.size()>=suffix.size()&&path.compare(path.size()-suffix.size(),suffix.size(),suffix)==0,"firmware must be the current posepriv-dpu-isolation.xclbin");
 require(::access(firmware,R_OK)==0,"firmware is not readable");
 require(disable&&std::string(disable)=="0","XLNX_DISABLE_LOAD_XCLBIN must be 0");
 require(library&&std::string(":"+std::string(library)+":").find(":/opt/vitis-ai-compat/lib:")!=std::string::npos,"LD_LIBRARY_PATH must include current /opt/vitis-ai-compat/lib");
}
#endif
class Backend {
#ifndef TAIL4_COMBINED_MOCK
 std::unique_ptr<xir::Graph>graph_;
 std::unique_ptr<vart::Runner>runner_;
 std::vector<std::unique_ptr<vart::TensorBuffer>>ib_,ob_;
 std::vector<vart::TensorBuffer*>ip_,op_;
 std::array<const uint8_t*,3>cuts_{};
 tail4_runtime tail_{};
 bool tail_open_=false;
#else
 std::vector<uint8_t>input_storage_;
 std::array<std::vector<uint8_t>,3>cuts_;
 std::atomic<bool>prefix_active_{false},tail_active_{false};
 std::atomic<unsigned>overlap_{0};
 unsigned prefix_us_=10000,tail_us_=30000;
 uint64_t fail_prefix_=0,fail_tail_=0;
 static uint64_t env(const char*name,uint64_t fallback) {const char*v=std::getenv(name);return v?std::strtoull(v,nullptr,10):fallback;}
#endif
 uint8_t*input_=nullptr;
public:
 Backend(char**argv) {
#ifndef TAIL4_COMBINED_MOCK
  check_environment();graph_=xir::Graph::deserialize(argv[1]);std::vector<const xir::Subgraph*>dpu;collect(graph_->get_root_subgraph(),dpu);
  const xir::Subgraph*selected=nullptr;
  for(auto*sg:dpu)if(sg->get_name()==PARTITION) {require(!selected,"duplicate prefix partition");selected=sg;}
  require(selected,"exact prefix partition missing");runner_=vart::Runner::create_runner(selected,"run");
  auto it=runner_->get_input_tensors(),ot=runner_->get_output_tensors();require(it.size()==1&&ot.size()==3,"prefix input/cut arity");
  check_tensor(it[0],INPUT_NAME,{1,256,256,3},7,INPUT);std::array<size_t,3>indices{};
  for(size_t j=0;j<3;++j) {size_t found=3;for(size_t k=0;k<3;++k)if(ot[k]->get_name()==NAMES[j]) {require(found==3,"duplicate cut name");found=k;}
   require(found!=3,"named prefix cut missing");indices[j]=found;check_tensor(ot[found],NAMES[j],j==2?std::vector<int>{1,64,64,39}:std::vector<int>{1,4,4,288},QS[j],SIZES[j]);}
  ib_=vart::alloc_cpu_flat_tensor_buffers(it);ob_=vart::alloc_cpu_flat_tensor_buffers(ot);
  auto input=vart::get_tensor_buffer_data(ib_[0].get(),0);require(input.size==INPUT,"Runner input storage");input_=static_cast<uint8_t*>(input.data);
  for(auto&b:ib_)ip_.push_back(b.get());
  for(auto&b:ob_)op_.push_back(b.get());
  for(size_t j=0;j<3;++j) {auto d=vart::get_tensor_buffer_data(ob_[indices[j]].get(),0);require(d.size==SIZES[j],"Runner cut storage");cuts_[j]=static_cast<const uint8_t*>(d.data);}
  int ret=tail4_runtime_open(&tail_,argv[2],argv[3],argv[4]);if(ret)throw std::runtime_error("tail4 runtime initialize errno="+std::to_string(-ret));tail_open_=true;
#else
  (void)argv;input_storage_.resize(INPUT);input_=input_storage_.data();
  for(size_t j=0;j<3;++j)cuts_[j].resize(SIZES[j]);
  prefix_us_=unsigned(env("TAIL4_MOCK_PREFIX_US",10000));tail_us_=unsigned(env("TAIL4_MOCK_TAIL_US",30000));
  fail_prefix_=env("TAIL4_MOCK_FAIL_PREFIX",0);fail_tail_=env("TAIL4_MOCK_FAIL_TAIL",0);
#endif
 }
 ~Backend() {
#ifndef TAIL4_COMBINED_MOCK
  if(tail_open_)tail4_runtime_close(&tail_);
#endif
 }
 uint8_t*input() {return input_;}
 void prefix(Slot&s) {
#ifndef TAIL4_COMBINED_MOCK
  const auto job=runner_->execute_async(ip_,op_);require(job.second==0&&runner_->wait(job.first,5000)==0,"DPU prefix execution failed");
  std::memcpy(s.bias.data(),cuts_[0],4608);std::memcpy(s.pool.data(),cuts_[1],4608);std::memcpy(s.heat.data(),cuts_[2],159744);
#else
  prefix_active_=true;if(tail_active_)++overlap_;
  std::this_thread::sleep_for(std::chrono::microseconds(prefix_us_));
  if(tail_active_)++overlap_;
  if(fail_prefix_&&s.sequence+1==fail_prefix_) {prefix_active_=false;throw std::runtime_error("mock prefix failure");}
  for(size_t j=0;j<3;++j)for(size_t k=0;k<SIZES[j];++k)cuts_[j][k]=uint8_t(input_[(k*13+j*17)%INPUT]+j+s.identity[16]);
  std::memcpy(s.bias.data(),cuts_[0].data(),4608);std::memcpy(s.pool.data(),cuts_[1].data(),4608);std::memcpy(s.heat.data(),cuts_[2].data(),159744);prefix_active_=false;
#endif
 }
 void tail(Slot&s) {
#ifndef TAIL4_COMBINED_MOCK
  int ret=tail4_runtime_execute(&tail_,reinterpret_cast<const int8_t*>(s.bias.data()),reinterpret_cast<const int8_t*>(s.pool.data()),uint16_t(number(s.identity.data(),8)));
  if(ret)throw std::runtime_error("tail35 errno="+std::to_string(-ret)+" status="+std::to_string(tail_.last_submission.status)+" outcome="+std::to_string(tail_.last_submission.outcome));
  // Copy raw S16 directly; never recover logits from float/sigmoid values.
  std::memcpy(s.head195.data(),tail_.arena+tail_.head195,390);std::memcpy(s.head117.data(),tail_.arena+tail_.head117,234);std::memcpy(s.head1.data(),tail_.arena+tail_.head1,2);
#else
  tail_active_=true;if(prefix_active_)++overlap_;
  std::this_thread::sleep_for(std::chrono::microseconds(tail_us_));if(prefix_active_)++overlap_;
  if(fail_tail_&&s.sequence+1==fail_tail_) {tail_active_=false;throw std::runtime_error("mock tail failure");}
  for(size_t k=0;k<195;++k)store(s.head195.data()+k*2,uint16_t(int(int8_t(s.bias[k%4608]))*257+int(int8_t(s.pool[(k*7)%4608]))*31+int(k)),2);
  for(size_t k=0;k<117;++k)store(s.head117.data()+k*2,uint16_t(int(int8_t(s.pool[(k*11)%4608]))*193-int(int8_t(s.bias[(k*5)%4608]))*17-int(k)),2);
  store(s.head1.data(),uint16_t(int(int8_t(s.bias[0]))*4+int(int8_t(s.pool[1]))),2);tail_active_=false;
#endif
 }
 unsigned mock_overlap()const {
#ifdef TAIL4_COMBINED_MOCK
  return overlap_.load();
#else
  return 0;
#endif
 }
};
std::string metadata() {
#ifndef TAIL4_COMBINED_MOCK
 const char*backend="vart_tail4_linux_memfd";const unsigned runners=1,tailfd=1;
#else
 const char*backend="backend_mock_memfd";const unsigned runners=0,tailfd=0;
#endif
 return "{\"schema\":\"posepriv.tail4_shm_worker.v1\",\"backend\":\""+std::string(backend)+"\",\"scope\":\"first60DPU+tail35PL\",\"runner_creations\":"+std::to_string(runners)+",\"tail_fd_opens\":"+std::to_string(tailfd)+",\"slots\":2,\"ring\":{\"slots\":4,\"stride\":360448,\"input_offset\":0,\"output_offset\":196608,\"bytes\":1441792,\"ticket_bytes\":44},\"input\":{\"name\":\"mp_pose_input/aquant\",\"dtype\":\"int8\",\"shape\":[1,256,256,3],\"fix_point\":7,\"bytes\":196608},\"outputs\":[{\"role\":\"landmarks195\",\"dtype\":\"int16\",\"shape\":[1,195],\"fix_point\":5,\"bytes\":390},{\"role\":\"world117\",\"dtype\":\"int16\",\"shape\":[1,117],\"fix_point\":14,\"bytes\":234},{\"role\":\"presence_logit1\",\"dtype\":\"int16\",\"shape\":[1,1],\"fix_point\":8,\"bytes\":2},{\"role\":\"heat233\",\"dtype\":\"int8\",\"shape\":[1,64,64,39],\"fix_point\":2,\"bytes\":159744}]}";
}
class Pipeline {
 Backend&backend_;
 std::atomic<bool>&failed_;
 Io&io_;
 Ring&ring_;
 std::array<bool,4>ring_active_{};
 std::array<Slot,2>slots_;
 std::mutex mutex_;
 std::condition_variable cv_;
 std::thread tail_thread_,output_thread_;
 bool input_done_=false,tail_done_=false;
 uint64_t produced_=0,processed_=0,completed_=0;
 struct RunTiming {
  uint64_t sources=0,total_ns=0,max_ns=0;
  void add(std::chrono::steady_clock::time_point begin) {
   const auto ns=uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-begin).count());
   ++sources;total_ns+=ns;if(ns>max_ns)max_ns=ns;
  }
 }prefix_timing_,tail_timing_;
 unsigned owned_=0,max_owned_=0;
 std::string error_;
 void fail(const std::string&message) {
  bool first=!failed_.exchange(true);
  if(first) {std::lock_guard<std::mutex>l(mutex_);error_=message;}
  io_.wake();cv_.notify_all();
 }
 void tail_loop() {
  uint64_t sequence=0;
  for(;;) {
   Slot*s;
   {std::unique_lock<std::mutex>l(mutex_);cv_.wait(l,[&]{return failed_||slots_[sequence%2].state==State::CUT_READY||(input_done_&&sequence==produced_);});
    if(failed_)return;
    if(input_done_&&sequence==produced_) {tail_done_=true;cv_.notify_all();return;}
    s=&slots_[sequence%2];s->state=State::TAIL;}
   const auto tail_begin=std::chrono::steady_clock::now();
   backend_.tail(*s);tail_timing_.add(tail_begin);
   {std::lock_guard<std::mutex>l(mutex_);if(failed_)return;s->state=State::RESULT_READY;++processed_;++sequence;cv_.notify_all();}
  }
 }
 void output_loop() {
  uint64_t sequence=0;
  for(;;) {
   Slot*s;
   {std::unique_lock<std::mutex>l(mutex_);cv_.wait(l,[&]{return failed_||slots_[sequence%2].state==State::RESULT_READY||(tail_done_&&sequence==processed_);});
    if(failed_)return;
    if(tail_done_&&sequence==processed_)break;
    s=&slots_[sequence%2];s->state=State::OUTPUT;}
   const unsigned ring_slot=unsigned(number(s->identity.data()+32,4));
   ring_.output(ring_slot,*s);
   {std::lock_guard<std::mutex>l(mutex_);ring_active_[ring_slot]=false;}
   store(s->identity.data()+28,OUTPUT,4);io_.header(2,44);io_.write(s->identity.data(),44);
   {std::lock_guard<std::mutex>l(mutex_);s->state=State::FREE;--owned_;++completed_;++sequence;cv_.notify_all();}
  }
  std::array<uint8_t,8>count{};store(count.data(),completed_,8);io_.header(4,8);io_.write(count.data(),8);
 }
public:
 Pipeline(Backend&backend,std::atomic<bool>&failed,Io&io,Ring&ring):backend_(backend),failed_(failed),io_(io),ring_(ring) {}
 ~Pipeline() {if(tail_thread_.joinable()||output_thread_.joinable()) {fail("pipeline teardown");if(tail_thread_.joinable())tail_thread_.join();if(output_thread_.joinable())output_thread_.join();}}
 int run() {
  try {
   tail_thread_=std::thread([&]{try{tail_loop();}catch(const std::exception&e){fail(e.what());}});
   output_thread_=std::thread([&]{try{output_loop();}catch(const std::exception&e){fail(e.what());}});
   for(;;) {
    Slot*s;
    {std::unique_lock<std::mutex>l(mutex_);cv_.wait(l,[&]{return failed_||slots_[produced_%2].state==State::FREE;});if(failed_)break;
     s=&slots_[produced_%2];s->sequence=produced_;s->state=State::PREFIX;++owned_;if(owned_>max_owned_)max_owned_=owned_;}
    std::array<uint8_t,16>h{};
    if(!io_.read(h.data(),16,true)) {std::lock_guard<std::mutex>l(mutex_);s->state=State::FREE;--owned_;input_done_=true;cv_.notify_all();break;}
    require(std::memcmp(h.data(),MAGIC,8)==0,"bad magic/version");require(number(h.data()+8,4)==1&&number(h.data()+12,4)==44,"request kind/length mismatch");
    io_.read(s->identity.data(),44);require(number(s->identity.data()+28,4)==INPUT,"input byte count mismatch");
    const unsigned ring_slot=unsigned(number(s->identity.data()+32,4));
    require(ring_slot<RING_SLOTS&&number(s->identity.data()+36,8)>0,"invalid shared tensor ring ticket");
    {std::lock_guard<std::mutex>l(mutex_);require(!ring_active_[ring_slot],"shared tensor ring slot still owned");ring_active_[ring_slot]=true;}
    ring_.input(ring_slot,backend_.input());
    const auto prefix_begin=std::chrono::steady_clock::now();
    backend_.prefix(*s);prefix_timing_.add(prefix_begin);
    {std::lock_guard<std::mutex>l(mutex_);if(failed_)break;s->state=State::CUT_READY;++produced_;cv_.notify_all();}
   }
  } catch(const std::exception&e) {fail(e.what());}
  if(tail_thread_.joinable())tail_thread_.join();
  if(output_thread_.joinable())output_thread_.join();
  std::cerr<<"tail4_combined frames="<<completed_<<" prefix_calls="<<produced_<<" tail_calls="<<processed_<<" max_owned_slots="<<max_owned_<<" mock_overlap="<<backend_.mock_overlap();
  if(!failed_)std::cerr<<" prefix_run_sources="<<prefix_timing_.sources<<" prefix_run_total_ns="<<prefix_timing_.total_ns<<" prefix_run_max_ns="<<prefix_timing_.max_ns
    <<" tail_run_sources="<<tail_timing_.sources<<" tail_run_total_ns="<<tail_timing_.total_ns<<" tail_run_max_ns="<<tail_timing_.max_ns;
  std::cerr<<'\n';
  if(failed_) {std::cerr<<"tail4_combined_worker: "<<error_<<'\n';return 3;}return 0;
 }
};
}
int main(int argc,char**argv) {
 try {
  std::signal(SIGPIPE,SIG_IGN);std::atomic<bool>failed{false};Io io(failed);
  require(argc==6,"usage: tail4_shm_worker MODEL.xmodel DEVICE W.bin P.bin MEMFD_FD");Ring ring(argv[5]);Backend backend(argv);
  const auto m=metadata();io.header(3,uint32_t(m.size()));io.write(reinterpret_cast<const uint8_t*>(m.data()),m.size());
  Pipeline pipeline(backend,failed,io,ring);return pipeline.run();
 } catch(const std::exception&e) {std::cerr<<"tail4_combined_worker: "<<e.what()<<'\n';return 3;}
}
