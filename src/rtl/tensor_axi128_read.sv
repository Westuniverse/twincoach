// SPDX-License-Identifier: GPL-2.0-only
// Fixed AXI4 128-bit INCR reader; one outstanding burst, no payload RAM reset.
module tensor_axi128_read(
 input logic clk_i,rst_i,
 input logic req_valid_i,output logic req_ready_o,
 input logic[63:0]req_addr_i,input logic[31:0]req_words_i,input logic[15:0]req_tag_i,
 input logic cancel_i,output logic busy_o,done_o,success_o,error_o,cancelled_o,
 output logic[15:0]status_tag_o,
 output logic[127:0]m_data_o,output logic[15:0]m_keep_o,m_tag_o,
 output logic m_valid_o,input logic m_ready_i,output logic m_last_o,
 output logic[63:0]m_axi_araddr_o,output logic[7:0]m_axi_arlen_o,
 output logic[2:0]m_axi_arsize_o,m_axi_arprot_o,
 output logic[1:0]m_axi_arburst_o,output logic[3:0]m_axi_arcache_o,m_axi_arqos_o,
 output logic m_axi_arid_o,m_axi_arlock_o,m_axi_arvalid_o,input logic m_axi_arready_i,
 input logic[127:0]m_axi_rdata_i,input logic[1:0]m_axi_rresp_i,
 input logic m_axi_rid_i,m_axi_rlast_i,m_axi_rvalid_i,output logic m_axi_rready_o
);
 typedef enum logic[1:0]{IDLE,ADDRESS,DATA,FINISH}state_t;
 state_t state_q;
 logic[63:0]addr_q;
 logic[31:0]remaining_q;
 logic[8:0]burst_q,read_left_q;
 logic[64:0]req_end;
 logic bad_beat,stop,read_fire;
 function automatic logic[8:0]plan(input logic[63:0]addr,input logic[31:0]words);
  logic[8:0]page,n;
  page=9'd256-{1'b0,addr[11:4]};n=words>32'd256 ? 9'd256 : words[8:0];
  return n>page?page:n;
 endfunction
 assign req_end={1'b0,req_addr_i}+{29'd0,req_words_i,4'd0}-65'd1;
 assign req_ready_o=state_q==IDLE&&!rst_i;
 assign busy_o=state_q!=IDLE;
 assign m_keep_o=16'hffff;assign m_tag_o=status_tag_o;
 assign m_axi_araddr_o=addr_q;assign m_axi_arlen_o=8'(burst_q-9'd1);
 assign m_axi_arsize_o=3'd4;assign m_axi_arburst_o=2'b01;
 assign m_axi_arcache_o=4'b0011;assign m_axi_arprot_o=3'b000;
 assign m_axi_arid_o=0;assign m_axi_arlock_o=0;assign m_axi_arqos_o=0;
 assign m_axi_arvalid_o=state_q==ADDRESS&&!rst_i;
 assign stop=error_o||cancelled_o||cancel_i;
 assign m_axi_rready_o=state_q==DATA&&!rst_i&&(!m_valid_o||m_ready_i||stop);
 assign read_fire=m_axi_rvalid_i&&m_axi_rready_o;
 assign bad_beat=m_axi_rresp_i!=0||m_axi_rid_i!=0||m_axi_rlast_i!=(read_left_q==1);
 always_ff@(posedge clk_i)begin
  if(rst_i)begin
   state_q<=IDLE;m_valid_o<=0;done_o<=0;success_o<=0;error_o<=0;cancelled_o<=0;
  end else begin
   done_o<=0;success_o<=0;
   if(m_valid_o&&m_ready_i)m_valid_o<=0;
   if(state_q!=IDLE&&cancel_i)cancelled_o<=1;
   case(state_q)
    IDLE:if(req_valid_i)begin
     status_tag_o<=req_tag_i;error_o<=0;cancelled_o<=0;
     if(req_addr_i[3:0]!=0||req_words_i==0||req_end[64])begin error_o<=1;done_o<=1;end
     else begin addr_q<=req_addr_i;remaining_q<=req_words_i;burst_q<=plan(req_addr_i,req_words_i);state_q<=ADDRESS;end
    end
    ADDRESS:if(m_axi_arready_i)begin read_left_q<=burst_q;state_q<=DATA;end
    DATA:if(read_fire)begin
     read_left_q<=read_left_q-9'd1;remaining_q<=remaining_q-32'd1;
     if(bad_beat)error_o<=1;
     if(!stop&&!bad_beat)begin m_data_o<=m_axi_rdata_i;m_last_o<=remaining_q==1;m_valid_o<=1;end
     if(read_left_q==1)begin
      if(stop||bad_beat||remaining_q==1)state_q<=FINISH;
      else begin
       addr_q<=addr_q+(64'(burst_q)<<4);
       burst_q<=plan(addr_q+(64'(burst_q)<<4),remaining_q-32'd1);state_q<=ADDRESS;
      end
     end
    end
    FINISH:if(!m_valid_o||m_ready_i)begin
     done_o<=1;success_o<=!stop;state_q<=IDLE;
    end
    default:state_q<=IDLE;
   endcase
  end
 end
endmodule
