// SPDX-License-Identifier: GPL-2.0-only
// Fixed AXI4 128-bit writer. AW and registered W are independent while active.
module tensor_axi128_write(
 input logic clk_i,rst_i,
 input logic req_valid_i,output logic req_ready_o,
 input logic[63:0]req_addr_i,input logic[31:0]req_words_i,input logic[15:0]req_tag_i,
 input logic[15:0]req_last_keep_i,
 input logic cancel_i,output logic busy_o,done_o,success_o,error_o,cancelled_o,
 output logic[15:0]status_tag_o,
 input logic[127:0]s_data_i,input logic[15:0]s_tag_i,
 input logic s_valid_i,output logic s_ready_o,input logic s_last_i,
 output logic[63:0]m_axi_awaddr_o,output logic[7:0]m_axi_awlen_o,
 output logic[2:0]m_axi_awsize_o,m_axi_awprot_o,
 output logic[1:0]m_axi_awburst_o,output logic[3:0]m_axi_awcache_o,m_axi_awqos_o,
 output logic m_axi_awid_o,m_axi_awlock_o,m_axi_awvalid_o,input logic m_axi_awready_i,
 output logic[127:0]m_axi_wdata_o,output logic[15:0]m_axi_wstrb_o,
 output logic m_axi_wlast_o,m_axi_wvalid_o,input logic m_axi_wready_i,
 input logic[1:0]m_axi_bresp_i,input logic m_axi_bid_i,m_axi_bvalid_i,output logic m_axi_bready_o
);
 typedef enum logic[1:0]{IDLE,SEND,RESPONSE,FINISH}state_t;
 state_t state_q;
 logic[63:0]addr_q;
 logic[31:0]remaining_q;
 logic[8:0]burst_q,issue_left_q;
 logic[15:0]last_keep_q;
 logic aw_pending_q,w_done_q,stop,source_bad,aw_fire,w_fire,bad_response,fill;
 logic[64:0]req_end;
 function automatic logic[8:0]plan(input logic[63:0]addr,input logic[31:0]words);
  logic[8:0]page,n;
  page=9'd256-{1'b0,addr[11:4]};n=words>32'd256 ? 9'd256 : words[8:0];
  return n>page?page:n;
 endfunction
 assign req_end={1'b0,req_addr_i}+{29'd0,req_words_i,4'd0}-65'd1;
 assign req_ready_o=state_q==IDLE&&!rst_i;
 assign busy_o=state_q!=IDLE;
 assign m_axi_awaddr_o=addr_q;assign m_axi_awlen_o=8'(burst_q-9'd1);
 assign m_axi_awsize_o=3'd4;assign m_axi_awburst_o=2'b01;
 assign m_axi_awcache_o=4'b0011;assign m_axi_awprot_o=3'b000;
 assign m_axi_awid_o=0;assign m_axi_awlock_o=0;assign m_axi_awqos_o=0;
 assign m_axi_awvalid_o=state_q==SEND&&aw_pending_q&&!rst_i;
 assign m_axi_bready_o=state_q==RESPONSE&&!rst_i;
 assign stop=error_o||cancelled_o||cancel_i;
 assign s_ready_o=state_q==SEND&&issue_left_q!=0&&(!m_axi_wvalid_o||m_axi_wready_i)&&!stop&&!rst_i;
 assign fill=state_q==SEND&&issue_left_q!=0&&(!m_axi_wvalid_o||m_axi_wready_i)&&(stop||s_valid_i);
 assign source_bad=s_tag_i!=status_tag_o||s_last_i!=(remaining_q==1);
 assign aw_fire=m_axi_awvalid_o&&m_axi_awready_i;
 assign w_fire=m_axi_wvalid_o&&m_axi_wready_i;
 assign bad_response=m_axi_bresp_i!=0||m_axi_bid_i!=0;
 always_ff@(posedge clk_i)begin
  if(rst_i)begin
   state_q<=IDLE;aw_pending_q<=0;w_done_q<=0;m_axi_wvalid_o<=0;
   done_o<=0;success_o<=0;error_o<=0;cancelled_o<=0;
  end else begin
   done_o<=0;success_o<=0;
   if(state_q!=IDLE&&cancel_i)cancelled_o<=1;
   if(w_fire)m_axi_wvalid_o<=0;
   case(state_q)
    IDLE:if(req_valid_i)begin
     status_tag_o<=req_tag_i;error_o<=0;cancelled_o<=0;
     if(req_addr_i[3:0]!=0||req_words_i==0||req_end[64]||req_last_keep_i==0)begin error_o<=1;done_o<=1;end
     else begin
      addr_q<=req_addr_i;remaining_q<=req_words_i;last_keep_q<=req_last_keep_i;
      burst_q<=plan(req_addr_i,req_words_i);issue_left_q<=plan(req_addr_i,req_words_i);
      aw_pending_q<=1;w_done_q<=0;state_q<=SEND;
     end
    end
    SEND:begin
     if(aw_fire)aw_pending_q<=0;
     if(fill)begin
      m_axi_wvalid_o<=1;m_axi_wdata_o<=s_data_i;m_axi_wlast_o<=issue_left_q==1;
      m_axi_wstrb_o<=(stop||source_bad)?16'd0:(remaining_q==1?last_keep_q:16'hffff);
      issue_left_q<=issue_left_q-9'd1;remaining_q<=remaining_q-32'd1;
      if(!stop&&source_bad)error_o<=1;
     end
     if(w_fire&&m_axi_wlast_o)w_done_q<=1;
     if((!aw_pending_q||aw_fire)&&(w_done_q||(w_fire&&m_axi_wlast_o)))state_q<=RESPONSE;
    end
    RESPONSE:if(m_axi_bvalid_i)begin
     if(bad_response)error_o<=1;
     if(stop||bad_response||remaining_q==0)state_q<=FINISH;
     else begin
      addr_q<=addr_q+(64'(burst_q)<<4);
      burst_q<=plan(addr_q+(64'(burst_q)<<4),remaining_q);
      issue_left_q<=plan(addr_q+(64'(burst_q)<<4),remaining_q);
      aw_pending_q<=1;w_done_q<=0;state_q<=SEND;
     end
    end
    FINISH:begin done_o<=1;success_o<=!stop;state_q<=IDLE;end
    default:state_q<=IDLE;
   endcase
  end
 end
endmodule
