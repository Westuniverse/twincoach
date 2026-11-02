// SPDX-License-Identifier: GPL-2.0-only
// One command: cached CHWc4 A16 -> 4PE chunks -> one contiguous CHWc4 AXI writer.
module tail4_conv_axi(
 input logic clk_i,rst_i,cmd_valid_i,output logic cmd_ready_o,
 input logic[63:0]a_addr_i,w_addr_i,p_addr_i,dst_addr_i,
 input logic[2:0]mode_i,input logic dim4_i,stride2_i,input logic[14:0]relu_limit_i,
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
 typedef enum logic[4:0]{IDLE,FEED_CFG,A_REQ,A_WAIT,WR_REQ,CORE_CFG,W_REQ,W_WAIT,P_REQ,P_WAIT,START_A,RUN,FINAL_WAIT,ABORT,FLUSH,FINISH}state_t;
 state_t state_q;
 logic[2:0]mode_q;logic dim4_q,stride2_q;logic[14:0]relu_limit_q;
 logic[6:0]chunk_q,last_chunk_q;logic[7:0]groups_q;
 logic[4:0]pixels_q,core_count_q;
 logic[31:0]output_words_q;logic[15:0]output_keep_q;
 logic[63:0]a_addr_q,w_addr_q,p_addr_q,dst_addr_q;
 logic core_cfg_ready,core_error,core_w_ready,core_p_ready,core_s_ready,core_valid,core_ready;
 logic[191:0]core_raw;logic[63:0]core_values;logic[15:0]core_tag;
 logic feed_cfg_ready,feed_load_ready,feed_loaded,act_run_ready,act_valid,act_last,act_done,act_ready,feed_error;
 logic[127:0]act_data;logic[15:0]act_tag;
 logic act_done_seen_q,writer_done_seen_q;
 logic half_valid_q,word_valid_q,word_last_q;
 logic[63:0]half_q;logic[127:0]word_q;
 logic rd_req_valid,rd_req_ready,rd_busy,rd_done,rd_success,rd_error,rd_cancelled;
 logic[63:0]rd_addr;logic[31:0]rd_words;logic[127:0]rd_data;logic rd_valid,rd_ready;
 logic wr_req_valid,wr_req_ready,wr_busy,wr_done,wr_success,wr_error,wr_cancelled,wr_sready;
 wire local_rst=rst_i||(state_q==FLUSH);
 wire abort_now=(state_q==ABORT)||((state_q!=IDLE)&&cancel_i)||core_error||feed_error||
                (rd_done&&!rd_success)||(wr_done&&!wr_success);
 wire last_chunk=(chunk_q==last_chunk_q);
 wire last_vector=last_chunk&&(core_count_q==pixels_q-5'd1);
 wire core_accept=core_valid&&core_ready;
 wire writer_accept=word_valid_q&&wr_sready;
 wire bad_header=(mode_i>3'd4)||((mode_i>=3'd2)&&dim4_i);
 wire[4:0]command_pixels=mode_i>=3'd2 ?5'd1:
   ((mode_i==3'd1)&&stride2_i?(dim4_i?5'd4:5'd1):(dim4_i?5'd16:5'd4));
 assign cmd_ready_o=(state_q==IDLE)&&core_cfg_ready&&feed_cfg_ready&&!rst_i;
 assign busy_o=state_q!=IDLE;
 assign rd_req_valid=(state_q==A_REQ||state_q==W_REQ||state_q==P_REQ)&&!abort_now;
 assign rd_addr=state_q==A_REQ?a_addr_q:(state_q==W_REQ?w_addr_q:p_addr_q);
 assign rd_words=state_q==A_REQ?(dim4_q?32'd576:32'd144):(state_q==W_REQ?({24'd0,groups_q}<<2):32'd4);
 assign rd_ready=state_q==ABORT?1'b1:(state_q==A_WAIT?feed_load_ready:
                 (state_q==W_WAIT?core_w_ready:(state_q==P_WAIT?core_p_ready:1'b0)));
 assign wr_req_valid=(state_q==WR_REQ)&&!abort_now;
 assign core_ready=(state_q==RUN)&&(!word_valid_q||wr_sready);
 assign act_ready=(state_q==RUN)&&core_s_ready;
 tail4_feature_feeder activations(.clk_i(clk_i),.rst_i(local_rst),
  .cfg_valid_i(state_q==FEED_CFG&&!abort_now),.cfg_ready_o(feed_cfg_ready),.cfg_dim2_i(!dim4_q),
  .load_valid_i(state_q==A_WAIT&&rd_valid),.load_ready_o(feed_load_ready),.load_data_i(rd_data),.loaded_o(feed_loaded),
  .run_valid_i(state_q==START_A&&!abort_now),.run_ready_o(act_run_ready),
  .run_mode_i(mode_q>=3'd2 ?2'd2:mode_q[1:0]),.run_stride2_i(stride2_q),
  .run_channel_quartet_i(chunk_q),.run_tag_base_i(16'd0),.run_error_o(feed_error),
  .m_valid_o(act_valid),.m_ready_i(act_ready),.features_o(act_data),.tag_o(act_tag),.last_o(act_last),.done_o(act_done));
 tail4_compute head(.clk_i(clk_i),.rst_i(local_rst),
  .cfg_valid_i(state_q==CORE_CFG&&!abort_now),.cfg_ready_o(core_cfg_ready),.cfg_groups_i(groups_q),
  .relu_i(mode_q==3'd1),.limit_s16_i(relu_limit_q),.cfg_error_o(core_error),
  .w_valid_i(state_q==W_WAIT&&rd_valid),.w_ready_o(core_w_ready),.w_data_i(rd_data),
  .p_valid_i(state_q==P_WAIT&&rd_valid),.p_ready_o(core_p_ready),.p_pe_i(rd_data[54:53]),
  .p_bias_i($signed(rd_data[47:0])),.p_shift_i(rd_data[52:48]),
  .s_valid_i(state_q==RUN&&act_valid),.s_ready_o(core_s_ready),.features_i(act_data),.tag_i(act_tag),
  .m_valid_o(core_valid),.m_ready_i(core_ready),.raw_o(core_raw),.values_o(core_values),.tag_o(core_tag));
 tensor_axi128_read reader(.clk_i(clk_i),.rst_i(rst_i),
  .req_valid_i(rd_req_valid),.req_ready_o(rd_req_ready),.req_addr_i(rd_addr),.req_words_i(rd_words),.req_tag_i(status_tag_o),
  .cancel_i(abort_now),.busy_o(rd_busy),.done_o(rd_done),.success_o(rd_success),.error_o(rd_error),.cancelled_o(rd_cancelled),.status_tag_o(),
  .m_data_o(rd_data),.m_keep_o(),.m_tag_o(),.m_valid_o(rd_valid),.m_ready_i(rd_ready),.m_last_o(),
  .m_axi_araddr_o(m_axi_araddr_o),.m_axi_arlen_o(m_axi_arlen_o),.m_axi_arsize_o(m_axi_arsize_o),.m_axi_arprot_o(m_axi_arprot_o),.m_axi_arburst_o(m_axi_arburst_o),
  .m_axi_arcache_o(m_axi_arcache_o),.m_axi_arqos_o(m_axi_arqos_o),.m_axi_arid_o(m_axi_arid_o),.m_axi_arlock_o(m_axi_arlock_o),.m_axi_arvalid_o(m_axi_arvalid_o),.m_axi_arready_i(m_axi_arready_i),
  .m_axi_rdata_i(m_axi_rdata_i),.m_axi_rresp_i(m_axi_rresp_i),.m_axi_rid_i(m_axi_rid_i),.m_axi_rlast_i(m_axi_rlast_i),.m_axi_rvalid_i(m_axi_rvalid_i),.m_axi_rready_o(m_axi_rready_o));
 tensor_axi128_write writer(.clk_i(clk_i),.rst_i(rst_i),
  .req_valid_i(wr_req_valid),.req_ready_o(wr_req_ready),.req_addr_i(dst_addr_q),.req_words_i(output_words_q),.req_tag_i(status_tag_o),.req_last_keep_i(output_keep_q),
  .cancel_i(abort_now),.busy_o(wr_busy),.done_o(wr_done),.success_o(wr_success),.error_o(wr_error),.cancelled_o(wr_cancelled),.status_tag_o(),
  .s_data_i(word_q),.s_tag_i(status_tag_o),.s_valid_i(word_valid_q),.s_ready_o(wr_sready),.s_last_i(word_last_q),
  .m_axi_awaddr_o(m_axi_awaddr_o),.m_axi_awlen_o(m_axi_awlen_o),.m_axi_awsize_o(m_axi_awsize_o),.m_axi_awprot_o(m_axi_awprot_o),.m_axi_awburst_o(m_axi_awburst_o),
  .m_axi_awcache_o(m_axi_awcache_o),.m_axi_awqos_o(m_axi_awqos_o),.m_axi_awid_o(m_axi_awid_o),.m_axi_awlock_o(m_axi_awlock_o),.m_axi_awvalid_o(m_axi_awvalid_o),.m_axi_awready_i(m_axi_awready_i),
  .m_axi_wdata_o(m_axi_wdata_o),.m_axi_wstrb_o(m_axi_wstrb_o),.m_axi_wlast_o(m_axi_wlast_o),.m_axi_wvalid_o(m_axi_wvalid_o),.m_axi_wready_i(m_axi_wready_i),
  .m_axi_bresp_i(m_axi_bresp_i),.m_axi_bid_i(m_axi_bid_i),.m_axi_bvalid_i(m_axi_bvalid_i),.m_axi_bready_o(m_axi_bready_o));
 always_ff @(posedge clk_i)begin
  if(rst_i)begin
   state_q<=IDLE;done_o<=0;success_o<=0;error_o<=0;cancelled_o<=0;
   half_valid_q<=0;word_valid_q<=0;act_done_seen_q<=0;writer_done_seen_q<=0;core_count_q<=0;
  end else begin
   done_o<=0;success_o<=0;
   if(state_q!=IDLE&&cancel_i)cancelled_o<=1;
   if(act_done)act_done_seen_q<=1;
   if(wr_done&&wr_success)writer_done_seen_q<=1;
   if(writer_accept)word_valid_q<=0;
   if(core_accept)begin
    core_count_q<=core_count_q+5'd1;
    if(last_vector&&!half_valid_q)begin
     word_q<={64'd0,core_values};word_last_q<=1;word_valid_q<=1;half_valid_q<=0;
    end else if(!half_valid_q)begin half_q<=core_values;half_valid_q<=1;end
    else begin word_q<={core_values,half_q};word_last_q<=last_vector;word_valid_q<=1;half_valid_q<=0;end
   end
   case(state_q)
    IDLE:if(cmd_valid_i&&cmd_ready_o)begin
     error_o<=0;cancelled_o<=0;status_tag_o<=job_tag_i;
     if(bad_header)begin error_o<=1;done_o<=1;end
     else begin
      a_addr_q<=a_addr_i;w_addr_q<=w_addr_i;p_addr_q<=p_addr_i;dst_addr_q<=dst_addr_i;
      mode_q<=mode_i;dim4_q<=dim4_i;stride2_q<=stride2_i;relu_limit_q<=relu_limit_i;
      chunk_q<=0;groups_q<=mode_i==0?8'd36:(mode_i==1?8'd5:8'd144);
      last_chunk_q<=mode_i<=1?7'd71:(mode_i==2?7'd48:(mode_i==3?7'd29:7'd0));
      pixels_q<=command_pixels;
      output_words_q<=mode_i<=1?({27'd0,command_pixels}*32'd36):(mode_i==2?32'd25:(mode_i==3?32'd15:32'd1));
      output_keep_q<=mode_i<=1?16'hffff:(mode_i==2?16'h003f:(mode_i==3?16'h03ff:16'h0003));
      half_valid_q<=0;word_valid_q<=0;act_done_seen_q<=0;core_count_q<=0;writer_done_seen_q<=0;
      state_q<=FEED_CFG;
     end
    end
    FEED_CFG:if(!abort_now&&feed_cfg_ready)state_q<=A_REQ;
    A_REQ:if(rd_req_valid&&rd_req_ready)state_q<=A_WAIT;
    A_WAIT:if(rd_done&&rd_success)state_q<=WR_REQ;
    WR_REQ:if(wr_req_valid&&wr_req_ready)state_q<=CORE_CFG;
    CORE_CFG:if(!abort_now&&core_cfg_ready)begin act_done_seen_q<=0;core_count_q<=0;state_q<=W_REQ;end
    W_REQ:if(rd_req_valid&&rd_req_ready)state_q<=W_WAIT;
    W_WAIT:if(rd_done&&rd_success)state_q<=P_REQ;
    P_REQ:if(rd_req_valid&&rd_req_ready)state_q<=P_WAIT;
    P_WAIT:if(rd_done&&rd_success)state_q<=START_A;
    START_A:if(!abort_now&&act_run_ready)state_q<=RUN;
    RUN:if(core_count_q==pixels_q&&core_cfg_ready&&(act_done_seen_q||act_done))begin
     if(last_chunk)state_q<=FINAL_WAIT;
     else begin chunk_q<=chunk_q+7'd1;w_addr_q<=w_addr_q+({56'd0,groups_q}<<6);p_addr_q<=p_addr_q+64'd64;state_q<=CORE_CFG;end
    end
    FINAL_WAIT:if((writer_done_seen_q||(wr_done&&wr_success))&&core_cfg_ready&&act_run_ready)state_q<=FINISH;
    ABORT:if(!rd_busy&&!wr_busy)state_q<=FLUSH;
    FLUSH:begin word_valid_q<=0;half_valid_q<=0;state_q<=FINISH;end
    FINISH:begin done_o<=1;success_o<=!error_o&&!cancelled_o&&!cancel_i;state_q<=IDLE;end
    default:state_q<=IDLE;
   endcase
   if(state_q!=IDLE&&state_q!=ABORT&&state_q!=FLUSH&&state_q!=FINISH&&
      (cancel_i||core_error||feed_error||(rd_done&&!rd_success)||(wr_done&&!wr_success)))begin
    if(core_error||feed_error||(rd_done&&rd_error)||(wr_done&&wr_error))error_o<=1;
    if(cancel_i||(rd_done&&rd_cancelled)||(wr_done&&wr_cancelled))cancelled_o<=1;
    state_q<=ABORT;
   end
  end
 end
endmodule
