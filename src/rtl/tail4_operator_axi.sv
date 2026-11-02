// SPDX-License-Identifier: GPL-2.0-only
// One cold AXI-Lite command owns Conv or Add/Pool until final AXI-B and leaf done.
module tail4_operator_axi(
 input logic clk_i,rst_i,
 input logic[11:0]s_axi_awaddr_i,input logic[2:0]s_axi_awprot_i,input logic s_axi_awvalid_i,output logic s_axi_awready_o,
 input logic[31:0]s_axi_wdata_i,input logic[3:0]s_axi_wstrb_i,input logic s_axi_wvalid_i,output logic s_axi_wready_o,
 output logic[1:0]s_axi_bresp_o,output logic s_axi_bvalid_o,input logic s_axi_bready_i,
 input logic[11:0]s_axi_araddr_i,input logic[2:0]s_axi_arprot_i,input logic s_axi_arvalid_i,output logic s_axi_arready_o,
 output logic[31:0]s_axi_rdata_o,output logic[1:0]s_axi_rresp_o,output logic s_axi_rvalid_o,input logic s_axi_rready_i,
 output logic irq_o,
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
 typedef enum logic[1:0]{IDLE,LAUNCH,RUN}state_t;
 state_t state_q;logic owner_q,cancel_q;
 logic[31:0]cfg_q,qparam_q,limit_q,tag_q;
 logic[63:0]a_q,wb_q,p_q,dst_q;
 logic[31:0]cmd_cfg_q,cmd_qparam_q;logic[14:0]cmd_limit_q;logic[15:0]cmd_tag_q;
 logic[63:0]cmd_a_q,cmd_wb_q,cmd_p_q,cmd_dst_q;
 logic sticky_done_q,sticky_success_q,sticky_error_q,sticky_cancelled_q;logic[15:0]last_tag_q;
 logic aw_pending_q,w_pending_q;logic[11:0]awaddr_q;logic[31:0]wdata_q;logic[3:0]wstrb_q;
 logic[31:0]read_data;logic read_bad,write_bad;
 logic[1:0]eng_ready,eng_busy,eng_done,eng_success,eng_error,eng_cancelled;
 logic[15:0]eng_tag[0:1];
 wire busy=state_q!=IDLE;
 wire write_commit=aw_pending_q&&w_pending_q&&!s_axi_bvalid_o;
 function automatic logic[31:0]merge_bytes(input logic[31:0]before_value,new_value,input logic[3:0]strb);
  logic[31:0]result;result=before_value;
  for(integer i=0;i<4;i++)if(strb[i])result[i*8+:8]=new_value[i*8+:8];
  return result;
 endfunction
 assign irq_o=sticky_done_q;
 assign s_axi_awready_o=!rst_i&&!aw_pending_q&&!s_axi_bvalid_o;
 assign s_axi_wready_o=!rst_i&&!w_pending_q&&!s_axi_bvalid_o;
 assign s_axi_arready_o=!rst_i&&!s_axi_rvalid_o;
 always_comb begin
  read_bad=0;read_data=0;
  case(s_axi_araddr_i)
   12'h000:read_data=32'h54344f50;
   12'h004:read_data=32'h00010000;
   12'h008:read_data=0;
   12'h00c:read_data={26'd0,sticky_done_q,sticky_cancelled_q,sticky_error_q,sticky_success_q,sticky_done_q,busy};
   12'h010:read_data={16'd0,last_tag_q};
   12'h014:read_data=cfg_q;12'h018:read_data=qparam_q;12'h01c:read_data=limit_q;12'h020:read_data=tag_q;
   12'h030:read_data=a_q[31:0];12'h034:read_data=a_q[63:32];
   12'h038:read_data=wb_q[31:0];12'h03c:read_data=wb_q[63:32];
   12'h040:read_data=p_q[31:0];12'h044:read_data=p_q[63:32];
   12'h048:read_data=dst_q[31:0];12'h04c:read_data=dst_q[63:32];
   default:read_bad=1;
  endcase
 end
 always_comb begin
  write_bad=0;
  case(awaddr_q)
   12'h008:if(wstrb_q[0]&&wdata_q[0]&&(busy||cfg_q[1:0]>2))write_bad=1;
   12'h014,12'h018,12'h01c,12'h020,12'h030,12'h034,12'h038,12'h03c,12'h040,12'h044,12'h048,12'h04c:;
   default:write_bad=1;
  endcase
 end
 always_ff @(posedge clk_i)begin
  if(rst_i)begin
   state_q<=IDLE;cancel_q<=0;owner_q<=0;aw_pending_q<=0;w_pending_q<=0;s_axi_bvalid_o<=0;s_axi_bresp_o<=0;
   s_axi_rvalid_o<=0;s_axi_rresp_o<=0;s_axi_rdata_o<=0;
   cfg_q<=0;qparam_q<=0;limit_q<=0;tag_q<=0;a_q<=0;wb_q<=0;p_q<=0;dst_q<=0;
   sticky_done_q<=0;sticky_success_q<=0;sticky_error_q<=0;sticky_cancelled_q<=0;last_tag_q<=0;
  end else begin
   if(s_axi_bvalid_o&&s_axi_bready_i)s_axi_bvalid_o<=0;
   if(s_axi_rvalid_o&&s_axi_rready_i)s_axi_rvalid_o<=0;
   if(s_axi_awvalid_i&&s_axi_awready_o)begin aw_pending_q<=1;awaddr_q<=s_axi_awaddr_i;end
   if(s_axi_wvalid_i&&s_axi_wready_o)begin w_pending_q<=1;wdata_q<=s_axi_wdata_i;wstrb_q<=s_axi_wstrb_i;end
   if(s_axi_arvalid_i&&s_axi_arready_o)begin s_axi_rvalid_o<=1;s_axi_rdata_o<=read_data;s_axi_rresp_o<=read_bad?2'd2:2'd0;end
   if(write_commit)begin
    aw_pending_q<=0;w_pending_q<=0;s_axi_bvalid_o<=1;s_axi_bresp_o<=write_bad?2'd2:2'd0;
    if(!write_bad)case(awaddr_q)
     12'h008:if(wstrb_q[0])begin
      if(wdata_q[2])begin sticky_done_q<=0;sticky_success_q<=0;sticky_error_q<=0;sticky_cancelled_q<=0;end
      if(wdata_q[1]&&busy)cancel_q<=1;
      if(wdata_q[0])begin
       cmd_a_q<=a_q;cmd_wb_q<=wb_q;cmd_p_q<=p_q;cmd_dst_q<=dst_q;
       cmd_cfg_q<=cfg_q;cmd_qparam_q<=qparam_q;cmd_limit_q<=limit_q[14:0];cmd_tag_q<=tag_q[15:0];
       owner_q<=cfg_q[1:0]!=0;cancel_q<=0;state_q<=LAUNCH;
       sticky_done_q<=0;sticky_success_q<=0;sticky_error_q<=0;sticky_cancelled_q<=0;
      end
     end
     12'h014:cfg_q<=merge_bytes(cfg_q,wdata_q,wstrb_q);
     12'h018:qparam_q<=merge_bytes(qparam_q,wdata_q,wstrb_q);
     12'h01c:limit_q<=merge_bytes(limit_q,wdata_q,wstrb_q);
     12'h020:tag_q<=merge_bytes(tag_q,wdata_q,wstrb_q);
     12'h030:a_q[31:0]<=merge_bytes(a_q[31:0],wdata_q,wstrb_q);
     12'h034:a_q[63:32]<=merge_bytes(a_q[63:32],wdata_q,wstrb_q);
     12'h038:wb_q[31:0]<=merge_bytes(wb_q[31:0],wdata_q,wstrb_q);
     12'h03c:wb_q[63:32]<=merge_bytes(wb_q[63:32],wdata_q,wstrb_q);
     12'h040:p_q[31:0]<=merge_bytes(p_q[31:0],wdata_q,wstrb_q);
     12'h044:p_q[63:32]<=merge_bytes(p_q[63:32],wdata_q,wstrb_q);
     12'h048:dst_q[31:0]<=merge_bytes(dst_q[31:0],wdata_q,wstrb_q);
     12'h04c:dst_q[63:32]<=merge_bytes(dst_q[63:32],wdata_q,wstrb_q);
     default:;
    endcase
   end
   case(state_q)
    LAUNCH:if(cancel_q)begin sticky_done_q<=1;sticky_cancelled_q<=1;last_tag_q<=cmd_tag_q;state_q<=IDLE;cancel_q<=0;end
           else if(eng_ready[owner_q])state_q<=RUN;
    RUN:if(eng_done[owner_q])begin
     sticky_done_q<=1;sticky_success_q<=eng_success[owner_q];sticky_error_q<=eng_error[owner_q];
     sticky_cancelled_q<=eng_cancelled[owner_q];last_tag_q<=eng_tag[owner_q];state_q<=IDLE;cancel_q<=0;
    end
    default:;
   endcase
  end
 end
 logic[63:0]bus_araddr[0:1];
 assign m_axi_araddr_o=busy?bus_araddr[owner_q]:'0;
 logic[7:0]bus_arlen[0:1];
 assign m_axi_arlen_o=busy?bus_arlen[owner_q]:'0;
 logic[2:0]bus_arsize[0:1];
 assign m_axi_arsize_o=busy?bus_arsize[owner_q]:'0;
 logic[2:0]bus_arprot[0:1];
 assign m_axi_arprot_o=busy?bus_arprot[owner_q]:'0;
 logic[1:0]bus_arburst[0:1];
 assign m_axi_arburst_o=busy?bus_arburst[owner_q]:'0;
 logic[3:0]bus_arcache[0:1];
 assign m_axi_arcache_o=busy?bus_arcache[owner_q]:'0;
 logic[3:0]bus_arqos[0:1];
 assign m_axi_arqos_o=busy?bus_arqos[owner_q]:'0;
 logic bus_arid[0:1];
 assign m_axi_arid_o=busy?bus_arid[owner_q]:'0;
 logic bus_arlock[0:1];
 assign m_axi_arlock_o=busy?bus_arlock[owner_q]:'0;
 logic bus_arvalid[0:1];
 assign m_axi_arvalid_o=busy?bus_arvalid[owner_q]:'0;
 logic bus_rready[0:1];
 assign m_axi_rready_o=busy?bus_rready[owner_q]:'0;
 logic[63:0]bus_awaddr[0:1];
 assign m_axi_awaddr_o=busy?bus_awaddr[owner_q]:'0;
 logic[7:0]bus_awlen[0:1];
 assign m_axi_awlen_o=busy?bus_awlen[owner_q]:'0;
 logic[2:0]bus_awsize[0:1];
 assign m_axi_awsize_o=busy?bus_awsize[owner_q]:'0;
 logic[2:0]bus_awprot[0:1];
 assign m_axi_awprot_o=busy?bus_awprot[owner_q]:'0;
 logic[1:0]bus_awburst[0:1];
 assign m_axi_awburst_o=busy?bus_awburst[owner_q]:'0;
 logic[3:0]bus_awcache[0:1];
 assign m_axi_awcache_o=busy?bus_awcache[owner_q]:'0;
 logic[3:0]bus_awqos[0:1];
 assign m_axi_awqos_o=busy?bus_awqos[owner_q]:'0;
 logic bus_awid[0:1];
 assign m_axi_awid_o=busy?bus_awid[owner_q]:'0;
 logic bus_awlock[0:1];
 assign m_axi_awlock_o=busy?bus_awlock[owner_q]:'0;
 logic bus_awvalid[0:1];
 assign m_axi_awvalid_o=busy?bus_awvalid[owner_q]:'0;
 logic[127:0]bus_wdata[0:1];
 assign m_axi_wdata_o=busy?bus_wdata[owner_q]:'0;
 logic[15:0]bus_wstrb[0:1];
 assign m_axi_wstrb_o=busy?bus_wstrb[owner_q]:'0;
 logic bus_wlast[0:1];
 assign m_axi_wlast_o=busy?bus_wlast[owner_q]:'0;
 logic bus_wvalid[0:1];
 assign m_axi_wvalid_o=busy?bus_wvalid[owner_q]:'0;
 logic bus_bready[0:1];
 assign m_axi_bready_o=busy?bus_bready[owner_q]:'0;
 tail4_conv_axi conv(
  .clk_i(clk_i),
  .rst_i(rst_i),
  .cmd_valid_i(state_q==LAUNCH&&owner_q==1'd0&&!cancel_q),
  .cmd_ready_o(eng_ready[0]),
  .a_addr_i(cmd_a_q),
  .dst_addr_i(cmd_dst_q),
  .job_tag_i(cmd_tag_q),
  .cancel_i(cancel_q&&owner_q==1'd0&&busy),
  .w_addr_i(cmd_wb_q),
  .p_addr_i(cmd_p_q),
  .mode_i(cmd_cfg_q[6:4]),
  .dim4_i(cmd_cfg_q[8]),
  .stride2_i(cmd_cfg_q[9]),
  .relu_limit_i(cmd_limit_q),
  .busy_o(eng_busy[0]),
  .done_o(eng_done[0]),
  .success_o(eng_success[0]),
  .error_o(eng_error[0]),
  .cancelled_o(eng_cancelled[0]),
  .status_tag_o(eng_tag[0]),
  .m_axi_araddr_o(bus_araddr[0]),
  .m_axi_arlen_o(bus_arlen[0]),
  .m_axi_arsize_o(bus_arsize[0]),
  .m_axi_arprot_o(bus_arprot[0]),
  .m_axi_arburst_o(bus_arburst[0]),
  .m_axi_arcache_o(bus_arcache[0]),
  .m_axi_arqos_o(bus_arqos[0]),
  .m_axi_arid_o(bus_arid[0]),
  .m_axi_arlock_o(bus_arlock[0]),
  .m_axi_arvalid_o(bus_arvalid[0]),
  .m_axi_rready_o(bus_rready[0]),
  .m_axi_awaddr_o(bus_awaddr[0]),
  .m_axi_awlen_o(bus_awlen[0]),
  .m_axi_awsize_o(bus_awsize[0]),
  .m_axi_awprot_o(bus_awprot[0]),
  .m_axi_awburst_o(bus_awburst[0]),
  .m_axi_awcache_o(bus_awcache[0]),
  .m_axi_awqos_o(bus_awqos[0]),
  .m_axi_awid_o(bus_awid[0]),
  .m_axi_awlock_o(bus_awlock[0]),
  .m_axi_awvalid_o(bus_awvalid[0]),
  .m_axi_wdata_o(bus_wdata[0]),
  .m_axi_wstrb_o(bus_wstrb[0]),
  .m_axi_wlast_o(bus_wlast[0]),
  .m_axi_wvalid_o(bus_wvalid[0]),
  .m_axi_bready_o(bus_bready[0]),
  .m_axi_arready_i((busy&&owner_q==1'd0&&m_axi_arready_i)),
  .m_axi_rdata_i(m_axi_rdata_i),
  .m_axi_rresp_i(m_axi_rresp_i),
  .m_axi_rid_i(m_axi_rid_i),
  .m_axi_rlast_i(m_axi_rlast_i),
  .m_axi_rvalid_i((busy&&owner_q==1'd0&&m_axi_rvalid_i)),
  .m_axi_awready_i((busy&&owner_q==1'd0&&m_axi_awready_i)),
  .m_axi_wready_i((busy&&owner_q==1'd0&&m_axi_wready_i)),
  .m_axi_bresp_i(m_axi_bresp_i),
  .m_axi_bid_i(m_axi_bid_i),
  .m_axi_bvalid_i((busy&&owner_q==1'd0&&m_axi_bvalid_i))
 );
 tail4_nonconv_axi nonconv(
  .clk_i(clk_i),
  .rst_i(rst_i),
  .cmd_valid_i(state_q==LAUNCH&&owner_q==1'd1&&!cancel_q),
  .cmd_ready_o(eng_ready[1]),
  .a_addr_i(cmd_a_q),
  .dst_addr_i(cmd_dst_q),
  .job_tag_i(cmd_tag_q),
  .cancel_i(cancel_q&&owner_q==1'd1&&busy),
  .b_addr_i(cmd_wb_q),
  .mode_i(cmd_cfg_q[1:0]==2'd2 ?2'd1:2'd0),
  .dim4_i(cmd_cfg_q[8]),
  .qa_i(cmd_qparam_q[3:0]),
  .qb_i(cmd_qparam_q[7:4]),
  .qout_i(cmd_qparam_q[11:8]),
  .relu_i(cmd_qparam_q[16]),
  .relu_limit_i(cmd_limit_q),
  .busy_o(eng_busy[1]),
  .done_o(eng_done[1]),
  .success_o(eng_success[1]),
  .error_o(eng_error[1]),
  .cancelled_o(eng_cancelled[1]),
  .status_tag_o(eng_tag[1]),
  .m_axi_araddr_o(bus_araddr[1]),
  .m_axi_arlen_o(bus_arlen[1]),
  .m_axi_arsize_o(bus_arsize[1]),
  .m_axi_arprot_o(bus_arprot[1]),
  .m_axi_arburst_o(bus_arburst[1]),
  .m_axi_arcache_o(bus_arcache[1]),
  .m_axi_arqos_o(bus_arqos[1]),
  .m_axi_arid_o(bus_arid[1]),
  .m_axi_arlock_o(bus_arlock[1]),
  .m_axi_arvalid_o(bus_arvalid[1]),
  .m_axi_rready_o(bus_rready[1]),
  .m_axi_awaddr_o(bus_awaddr[1]),
  .m_axi_awlen_o(bus_awlen[1]),
  .m_axi_awsize_o(bus_awsize[1]),
  .m_axi_awprot_o(bus_awprot[1]),
  .m_axi_awburst_o(bus_awburst[1]),
  .m_axi_awcache_o(bus_awcache[1]),
  .m_axi_awqos_o(bus_awqos[1]),
  .m_axi_awid_o(bus_awid[1]),
  .m_axi_awlock_o(bus_awlock[1]),
  .m_axi_awvalid_o(bus_awvalid[1]),
  .m_axi_wdata_o(bus_wdata[1]),
  .m_axi_wstrb_o(bus_wstrb[1]),
  .m_axi_wlast_o(bus_wlast[1]),
  .m_axi_wvalid_o(bus_wvalid[1]),
  .m_axi_bready_o(bus_bready[1]),
  .m_axi_arready_i((busy&&owner_q==1'd1&&m_axi_arready_i)),
  .m_axi_rdata_i(m_axi_rdata_i),
  .m_axi_rresp_i(m_axi_rresp_i),
  .m_axi_rid_i(m_axi_rid_i),
  .m_axi_rlast_i(m_axi_rlast_i),
  .m_axi_rvalid_i((busy&&owner_q==1'd1&&m_axi_rvalid_i)),
  .m_axi_awready_i((busy&&owner_q==1'd1&&m_axi_awready_i)),
  .m_axi_wready_i((busy&&owner_q==1'd1&&m_axi_wready_i)),
  .m_axi_bresp_i(m_axi_bresp_i),
  .m_axi_bid_i(m_axi_bid_i),
  .m_axi_bvalid_i((busy&&owner_q==1'd1&&m_axi_bvalid_i))
 );
endmodule
