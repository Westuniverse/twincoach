// SPDX-License-Identifier: GPL-2.0-only
// A16CHWc4 CI288 Add4/2 andPool4to2, onecache/oneALU/onewriter.
module tail4_nonconv_axi(
 input logic clk_i,rst_i,cmd_valid_i,output logic cmd_ready_o,
 input logic[63:0]a_addr_i,b_addr_i,dst_addr_i,
 input logic[1:0]mode_i,input logic dim4_i,
 input logic[3:0]qa_i,qb_i,qout_i,input logic relu_i,input logic[14:0]relu_limit_i,
 input logic[15:0]job_tag_i,input logic cancel_i,
 output logic busy_o,done_o,success_o,error_o,cancelled_o,output logic[15:0]status_tag_o,
 output logic[63:0]m_axi_araddr_o,output logic[7:0]m_axi_arlen_o,
 output logic[2:0]m_axi_arsize_o,m_axi_arprot_o,output logic[1:0]m_axi_arburst_o,
 output logic[3:0]m_axi_arcache_o,m_axi_arqos_o,output logic m_axi_arid_o,m_axi_arlock_o,m_axi_arvalid_o,input logic m_axi_arready_i,
 input logic[127:0]m_axi_rdata_i,input logic[1:0]m_axi_rresp_i,input logic m_axi_rid_i,m_axi_rlast_i,m_axi_rvalid_i,output logic m_axi_rready_o,
 output logic[63:0]m_axi_awaddr_o,output logic[7:0]m_axi_awlen_o,
 output logic[2:0]m_axi_awsize_o,m_axi_awprot_o,output logic[1:0]m_axi_awburst_o,
 output logic[3:0]m_axi_awcache_o,m_axi_awqos_o,output logic m_axi_awid_o,m_axi_awlock_o,m_axi_awvalid_o,input logic m_axi_awready_i,
 output logic[127:0]m_axi_wdata_o,output logic[15:0]m_axi_wstrb_o,output logic m_axi_wlast_o,m_axi_wvalid_o,input logic m_axi_wready_i,
 input logic[1:0]m_axi_bresp_i,input logic m_axi_bid_i,m_axi_bvalid_i,output logic m_axi_bready_o
);

 typedef enum logic[4:0]{IDLE,A_REQ,A_WAIT,ALU_CFG,WR_REQ,B_REQ,
  ADD_B0,ADD_B1,ADD_PACK,POOL_READ,POOL_CAPTURE,ALU_SEND,ALU_WAIT,
  OUT_LOW,OUT_HIGH,WAIT_FINAL,ABORT,FLUSH,FINISH}state_t;
 state_t state_q;
 logic mode_pool_q,dim4_q,relu_q;logic[3:0]qa_q,qb_q,qout_q;logic[14:0]limit_q;
 logic[63:0]a_addr_q,b_addr_q,dst_addr_q;
 logic[31:0]input_words_q,output_words_q,write_index_q;
 logic[8:0]record_q,records_q;logic[2:0]pool_word_q;
 logic[9:0]load_index_q,cache_addr;
 logic[127:0]a_low_q,b_low_q,b_high_q,cache_data_q;
 logic[1023:0]taps_q;logic[255:0]result_q;
 logic writer_done_seen_q;
 logic rd_req_valid,rd_req_ready,rd_busy,rd_done,rd_success,rd_error,rd_cancelled;
 logic[127:0]rd_data;logic rd_valid,rd_ready;
 logic wr_req_valid,wr_req_ready,wr_busy,wr_done,wr_success,wr_error,wr_cancelled,wr_sready;
 logic alu_cfg_ready,alu_error,alu_sready,alu_valid,alu_ready;
 logic[255:0]alu_values;logic[15:0]alu_tag;
 wire local_rst=rst_i||(state_q==FLUSH);
 wire abort_now=(state_q==ABORT)||((state_q!=IDLE)&&cancel_i)||alu_error||
                (rd_done&&!rd_success)||(wr_done&&!wr_success);
 wire bad_header=(mode_i>2'd1)||((mode_i==2'd1)&&!dim4_i);
 wire reader_accept=rd_valid&&rd_ready;
 wire writer_valid=(state_q==OUT_LOW||state_q==OUT_HIGH)&&!abort_now;
 wire writer_accept=writer_valid&&wr_sready;
 wire[127:0]writer_data=(state_q==OUT_LOW)?result_q[127:0]:result_q[255:128];
 wire[1:0]pool_pixel={pool_word_q[2],pool_word_q[0]};
 wire cache_write=(state_q==A_WAIT)&&reader_accept;
 wire cache_read=(!abort_now)&&(((state_q==ADD_B0||state_q==ADD_B1)&&reader_accept)||(state_q==POOL_READ));
 (* ram_style="block" *)logic[127:0]cache[0:575];
 assign cmd_ready_o=(state_q==IDLE)&&alu_cfg_ready&&!rst_i;
 assign busy_o=state_q!=IDLE;
 assign rd_req_valid=(state_q==A_REQ||state_q==B_REQ)&&!abort_now;
 assign rd_ready=(state_q==ABORT)||(state_q==A_WAIT)||(state_q==ADD_B0)||(state_q==ADD_B1);
 assign wr_req_valid=(state_q==WR_REQ)&&!abort_now;
 assign alu_ready=(state_q==ALU_WAIT)&&!abort_now;
 always_comb begin
  cache_addr={record_q,1'b0};
  if(state_q==ADD_B1)cache_addr={record_q,1'b0}+10'd1;
  if(state_q==POOL_READ)cache_addr={record_q[6:0],pool_word_q};
 end
 // Standard singlewrite/synchronousread SDP template; no data reset.
 always_ff @(posedge clk_i)begin
  if(cache_write)cache[load_index_q]<=rd_data;
  if(cache_read)cache_data_q<=cache[cache_addr];
 end
 tensor_alu16_folded_s16 #(.LANES(4)) alu(.clk_i(clk_i),.rst_i(local_rst),
  .cfg_valid_i(state_q==ALU_CFG&&!abort_now),.cfg_ready_o(alu_cfg_ready),.cfg_op_i(mode_pool_q?2'd2:2'd0),
  .cfg_qa_i(qa_q),.cfg_qb_i(qb_q),.cfg_qout_i(qout_q),.cfg_relu6_i(relu_q),.cfg_limit_i(limit_q),.cfg_error_o(alu_error),
  .s_valid_i(state_q==ALU_SEND&&!abort_now),.s_ready_o(alu_sready),.taps_i(taps_q),.phase_i(2'd0),.tag_i({7'd0,record_q}),
  .m_valid_o(alu_valid),.m_ready_i(alu_ready),.values_o(alu_values),.tag_o(alu_tag));
 tensor_axi128_read reader(.clk_i(clk_i),.rst_i(rst_i),
  .req_valid_i(rd_req_valid),.req_ready_o(rd_req_ready),.req_addr_i(state_q==A_REQ?a_addr_q:b_addr_q),.req_words_i(input_words_q),.req_tag_i(status_tag_o),
  .cancel_i(abort_now),.busy_o(rd_busy),.done_o(rd_done),.success_o(rd_success),.error_o(rd_error),.cancelled_o(rd_cancelled),.status_tag_o(),
  .m_data_o(rd_data),.m_keep_o(),.m_tag_o(),.m_valid_o(rd_valid),.m_ready_i(rd_ready),.m_last_o(),
  .m_axi_araddr_o(m_axi_araddr_o),.m_axi_arlen_o(m_axi_arlen_o),.m_axi_arsize_o(m_axi_arsize_o),.m_axi_arprot_o(m_axi_arprot_o),.m_axi_arburst_o(m_axi_arburst_o),
  .m_axi_arcache_o(m_axi_arcache_o),.m_axi_arqos_o(m_axi_arqos_o),.m_axi_arid_o(m_axi_arid_o),.m_axi_arlock_o(m_axi_arlock_o),.m_axi_arvalid_o(m_axi_arvalid_o),.m_axi_arready_i(m_axi_arready_i),
  .m_axi_rdata_i(m_axi_rdata_i),.m_axi_rresp_i(m_axi_rresp_i),.m_axi_rid_i(m_axi_rid_i),.m_axi_rlast_i(m_axi_rlast_i),.m_axi_rvalid_i(m_axi_rvalid_i),.m_axi_rready_o(m_axi_rready_o));
 tensor_axi128_write writer(.clk_i(clk_i),.rst_i(rst_i),
  .req_valid_i(wr_req_valid),.req_ready_o(wr_req_ready),.req_addr_i(dst_addr_q),.req_words_i(output_words_q),.req_tag_i(status_tag_o),.req_last_keep_i(16'hffff),
  .cancel_i(abort_now),.busy_o(wr_busy),.done_o(wr_done),.success_o(wr_success),.error_o(wr_error),.cancelled_o(wr_cancelled),.status_tag_o(),
  .s_data_i(writer_data),.s_tag_i(status_tag_o),.s_valid_i(writer_valid),.s_ready_o(wr_sready),.s_last_i(write_index_q==output_words_q-32'd1),
  .m_axi_awaddr_o(m_axi_awaddr_o),.m_axi_awlen_o(m_axi_awlen_o),.m_axi_awsize_o(m_axi_awsize_o),.m_axi_awprot_o(m_axi_awprot_o),.m_axi_awburst_o(m_axi_awburst_o),
  .m_axi_awcache_o(m_axi_awcache_o),.m_axi_awqos_o(m_axi_awqos_o),.m_axi_awid_o(m_axi_awid_o),.m_axi_awlock_o(m_axi_awlock_o),.m_axi_awvalid_o(m_axi_awvalid_o),.m_axi_awready_i(m_axi_awready_i),
  .m_axi_wdata_o(m_axi_wdata_o),.m_axi_wstrb_o(m_axi_wstrb_o),.m_axi_wlast_o(m_axi_wlast_o),.m_axi_wvalid_o(m_axi_wvalid_o),.m_axi_wready_i(m_axi_wready_i),
  .m_axi_bresp_i(m_axi_bresp_i),.m_axi_bid_i(m_axi_bid_i),.m_axi_bvalid_i(m_axi_bvalid_i),.m_axi_bready_o(m_axi_bready_o));
 always_ff @(posedge clk_i)begin
  if(rst_i)begin
   state_q<=IDLE;done_o<=0;success_o<=0;error_o<=0;cancelled_o<=0;
   writer_done_seen_q<=0;load_index_q<=0;record_q<=0;write_index_q<=0;
  end else begin
   done_o<=0;success_o<=0;
   if(state_q!=IDLE&&cancel_i)cancelled_o<=1;
   if(wr_done&&wr_success)writer_done_seen_q<=1;
   if(cache_write)load_index_q<=load_index_q+10'd1;
   if(writer_accept)write_index_q<=write_index_q+32'd1;
   case(state_q)
    IDLE:if(cmd_valid_i&&cmd_ready_o)begin
     error_o<=0;cancelled_o<=0;status_tag_o<=job_tag_i;
     if(bad_header)begin error_o<=1;done_o<=1;end
     else begin
      a_addr_q<=a_addr_i;b_addr_q<=b_addr_i;dst_addr_q<=dst_addr_i;
      mode_pool_q<=mode_i==1;dim4_q<=dim4_i;qa_q<=qa_i;qb_q<=qb_i;qout_q<=qout_i;relu_q<=relu_i;limit_q<=relu_limit_i;
      input_words_q<=dim4_i?32'd576:32'd144;
      output_words_q<=mode_i==1?32'd144:(dim4_i?32'd576:32'd144);
      records_q<=mode_i==1?9'd72:(dim4_i?9'd288:9'd72);
      load_index_q<=0;record_q<=0;write_index_q<=0;writer_done_seen_q<=0;state_q<=A_REQ;
     end
    end
    A_REQ:if(rd_req_valid&&rd_req_ready)state_q<=A_WAIT;
    A_WAIT:if(rd_done&&rd_success)state_q<=ALU_CFG;
    ALU_CFG:if(!abort_now&&alu_cfg_ready)state_q<=WR_REQ;
    WR_REQ:if(wr_req_valid&&wr_req_ready)begin
     if(mode_pool_q)begin pool_word_q<=0;state_q<=POOL_READ;end else state_q<=B_REQ;
    end
    B_REQ:if(rd_req_valid&&rd_req_ready)state_q<=ADD_B0;
    ADD_B0:if(reader_accept)begin b_low_q<=rd_data;state_q<=ADD_B1;end
    ADD_B1:if(reader_accept)begin b_high_q<=rd_data;a_low_q<=cache_data_q;state_q<=ADD_PACK;end
    ADD_PACK:begin taps_q<={512'd0,b_high_q,b_low_q,cache_data_q,a_low_q};state_q<=ALU_SEND;end
    POOL_READ:if(!abort_now)state_q<=POOL_CAPTURE;
    POOL_CAPTURE:begin
     if(!pool_word_q[1])begin
      taps_q[int'(pool_pixel)*64+:64]<=cache_data_q[63:0];
      taps_q[256+int'(pool_pixel)*64+:64]<=cache_data_q[127:64];
     end else begin
      taps_q[512+int'(pool_pixel)*64+:64]<=cache_data_q[63:0];
      taps_q[768+int'(pool_pixel)*64+:64]<=cache_data_q[127:64];
     end
     if(pool_word_q==7)state_q<=ALU_SEND;
     else begin pool_word_q<=pool_word_q+3'd1;state_q<=POOL_READ;end
    end
    ALU_SEND:if(!abort_now&&alu_sready)state_q<=ALU_WAIT;
    ALU_WAIT:if(alu_valid&&alu_ready)begin result_q<=alu_values;state_q<=OUT_LOW;end
    OUT_LOW:if(writer_accept)state_q<=OUT_HIGH;
    OUT_HIGH:if(writer_accept)begin
     if(record_q==records_q-9'd1)state_q<=WAIT_FINAL;
     else begin
      record_q<=record_q+9'd1;
      if(mode_pool_q)begin pool_word_q<=0;state_q<=POOL_READ;end else state_q<=ADD_B0;
     end
    end
    WAIT_FINAL:if((writer_done_seen_q||(wr_done&&wr_success))&&!rd_busy&&alu_cfg_ready)state_q<=FINISH;
    ABORT:if(!rd_busy&&!wr_busy)state_q<=FLUSH;
    FLUSH:state_q<=FINISH;
    FINISH:begin done_o<=1;success_o<=!error_o&&!cancelled_o&&!cancel_i;state_q<=IDLE;end
    default:state_q<=IDLE;
   endcase
   if(state_q!=IDLE&&state_q!=ABORT&&state_q!=FLUSH&&state_q!=FINISH&&
      (cancel_i||alu_error||(rd_done&&!rd_success)||(wr_done&&!wr_success)))begin
    if(alu_error||(rd_done&&rd_error)||(wr_done&&wr_error))error_o<=1;
    if(cancel_i||(rd_done&&rd_cancelled)||(wr_done&&wr_cancelled))cancelled_o<=1;
    state_q<=ABORT;
   end
  end
 end
endmodule
