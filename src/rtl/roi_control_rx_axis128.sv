// SPDX-License-Identifier: GPL-2.0-only
// ROI1 control + exclusive RX mux. All ports synchronous to clk_i, reset high.
// Reset requires system-level master quiescence. CANCEL never resets AXI master.
module roi_control_rx_axis128 (
 input logic clk_i,rst_i,
 input logic[11:0]s_axi_awaddr,input logic s_axi_awvalid,output logic s_axi_awready,
 input logic[31:0]s_axi_wdata,input logic[3:0]s_axi_wstrb,input logic s_axi_wvalid,output logic s_axi_wready,
 output logic[1:0]s_axi_bresp,output logic s_axi_bvalid,input logic s_axi_bready,
 input logic[11:0]s_axi_araddr,input logic s_axi_arvalid,output logic s_axi_arready,
 output logic[31:0]s_axi_rdata,output logic[1:0]s_axi_rresp,output logic s_axi_rvalid,input logic s_axi_rready,
 output logic core_cmd_valid_o,input logic core_cmd_ready_i,
 output logic[63:0]source_addr_o,map_addr_o,ticket_o,
 output logic[11:0]source_width_o,source_height_o,output logic[31:0]source_stride_o,
 output logic core_cancel_o,
 input logic core_busy_i,core_done_i,core_success_i,core_error_i,core_cancelled_i,core_read_quiesced_i,
 input logic[3:0]core_error_code_i,
 input logic[127:0]roi_tdata_i,input logic[15:0]roi_tkeep_i,input logic roi_tvalid_i,roi_tlast_i,output logic roi_tready_o,
 input logic[127:0]legacy_tdata_i,input logic[15:0]legacy_tkeep_i,input logic legacy_tvalid_i,legacy_tlast_i,output logic legacy_tready_o,
 output logic[127:0]m_axis_tdata_o,output logic[15:0]m_axis_tkeep_o,output logic m_axis_tvalid_o,m_axis_tlast_o,input logic m_axis_tready_i
);
 logic awheld_q,wheld_q,bvalid_q,rvalid_q;
 logic[11:0]awaddr_q;
 logic[31:0]wdata_q,rdata_q;
 logic[3:0]wstrb_q;
 logic[1:0]bresp_q,rresp_q;
 logic route_q,legacy_packet_q,busy_q,pending_q,done_q,success_q,error_q,cancelled_q;
 logic cancel_q,quiesced_q,ended_q,abort_q;
 logic[3:0]error_code_q;
 logic[31:0]bytes_q,dims_q;
 logic commit,write_ok,cmd_fire,roi_fire,last_fire;
 logic idle_config,can_arm,can_release;
 assign s_axi_awready=!awheld_q&&!bvalid_q&&!rst_i;
 assign s_axi_wready=!wheld_q&&!bvalid_q&&!rst_i;
 assign s_axi_bvalid=bvalid_q;assign s_axi_bresp=bresp_q;
 assign s_axi_arready=!rvalid_q&&!rst_i;
 assign s_axi_rvalid=rvalid_q;assign s_axi_rdata=rdata_q;assign s_axi_rresp=rresp_q;
 assign commit=awheld_q&&wheld_q&&!bvalid_q;
 assign idle_config=!busy_q&&!pending_q&&!core_busy_i&&!abort_q;
 assign can_arm=idle_config&&!done_q&&!legacy_packet_q&&!legacy_tvalid_i&&!roi_tvalid_i;
 assign can_release=idle_config&&!roi_tvalid_i;
 assign core_cmd_valid_o=pending_q&&!cancel_q;
 assign cmd_fire=core_cmd_valid_o&&core_cmd_ready_i;
 assign core_cancel_o=busy_q&&cancel_q;
 assign source_width_o=dims_q[11:0];assign source_height_o=dims_q[27:16];
 // Route cannot change while an offered beat or reservation exists.
 assign m_axis_tdata_o=route_q ? (abort_q ? 128'd0:roi_tdata_i):legacy_tdata_i;
 assign m_axis_tkeep_o=route_q ? (abort_q ? 16'hffff:roi_tkeep_i):legacy_tkeep_i;
 assign m_axis_tvalid_o=!rst_i&&(route_q ? (abort_q||roi_tvalid_i):legacy_tvalid_i);
 assign m_axis_tlast_o=route_q ? (abort_q||roi_tlast_i):legacy_tlast_i;
 assign roi_tready_o=route_q&&!abort_q&&m_axis_tready_i&&!rst_i;
 assign legacy_tready_o=!route_q&&m_axis_tready_i&&!rst_i;
 assign roi_fire=route_q&&m_axis_tvalid_o&&m_axis_tready_i;
 assign last_fire=roi_fire&&m_axis_tlast_o;
 always_comb begin
  write_ok=1'b0;
  if(wstrb_q==0)write_ok=1'b1;
  else if(wstrb_q==4'hf)begin
   case(awaddr_q)
    12'h008:case(wdata_q)
     32'd1:write_ok=route_q&&idle_config&&!done_q;
     32'd2:write_ok=route_q&&busy_q;
     32'd4:write_ok=idle_config;
     32'd8:write_ok=can_arm;
     32'd16:write_ok=can_release;
     default:write_ok=1'b0;
    endcase
    12'h020,12'h024,12'h028,12'h02c,12'h034,12'h038,12'h03c:write_ok=idle_config&&!done_q;
    12'h030:write_ok=idle_config&&!done_q&&!(|(wdata_q&32'hf000f000));
    default:write_ok=1'b0;
   endcase
  end
 end
 always_ff@(posedge clk_i)begin
  if(rst_i)begin
   awheld_q<=0;wheld_q<=0;bvalid_q<=0;rvalid_q<=0;bresp_q<=0;rresp_q<=0;
   route_q<=0;legacy_packet_q<=0;busy_q<=0;pending_q<=0;done_q<=0;
   success_q<=0;error_q<=0;cancelled_q<=0;cancel_q<=0;quiesced_q<=0;ended_q<=0;abort_q<=0;
   error_code_q<=0;bytes_q<=0;
   source_addr_o<=0;map_addr_o<=0;ticket_o<=0;dims_q<=0;source_stride_o<=0;
  end else begin
   if(s_axi_awvalid&&s_axi_awready)begin awheld_q<=1;awaddr_q<=s_axi_awaddr;end
   if(s_axi_wvalid&&s_axi_wready)begin wheld_q<=1;wdata_q<=s_axi_wdata;wstrb_q<=s_axi_wstrb;end
   if(bvalid_q&&s_axi_bready)bvalid_q<=0;
   if(rvalid_q&&s_axi_rready)rvalid_q<=0;
   if(s_axi_arvalid&&s_axi_arready)begin
    rvalid_q<=1;rresp_q<=0;
    case(s_axi_araddr)
     12'h000:rdata_q<=32'h524f4931;
     12'h004:rdata_q<=32'h00010000;
     12'h00c:rdata_q<={15'd0,legacy_packet_q, error_code_q,2'd0,(cancel_q&&core_read_quiesced_i),ended_q,quiesced_q,abort_q,route_q,cancelled_q,error_q,success_q,done_q,busy_q};
     12'h010:rdata_q<=ticket_o[31:0];12'h014:rdata_q<=ticket_o[63:32];
     12'h018:rdata_q<=32'd196608;12'h01c:rdata_q<=32'd262144;
     12'h020:rdata_q<=source_addr_o[31:0];12'h024:rdata_q<=source_addr_o[63:32];
     12'h028:rdata_q<=map_addr_o[31:0];12'h02c:rdata_q<=map_addr_o[63:32];
     12'h030:rdata_q<=dims_q;12'h034:rdata_q<=source_stride_o;
     12'h038:rdata_q<=ticket_o[31:0];12'h03c:rdata_q<=ticket_o[63:32];
     12'h040:rdata_q<=bytes_q;
     default:begin rdata_q<=0;rresp_q<=2'b10;end
    endcase
   end
   if(!route_q&&legacy_tvalid_i&&legacy_tready_o)legacy_packet_q<=!legacy_tlast_i;
   if(cmd_fire)pending_q<=0;
   if(roi_fire)bytes_q<=bytes_q+32'd16;
   if(last_fire)begin ended_q<=1;abort_q<=0;end
   if(core_done_i&&busy_q)begin
    quiesced_q<=1;success_q<=core_success_i&&!cancel_q;error_q<=core_error_i;cancelled_q<=core_cancelled_i||cancel_q;error_code_q<=core_error_code_i;
    if(!(ended_q||last_fire))begin
     abort_q<=1;
     if(core_success_i)begin success_q<=0;error_q<=1;error_code_q<=4'd7;end
    end else if(core_success_i&&bytes_q+(roi_fire?32'd16:32'd0)!=32'd196608)begin
     success_q<=0;error_q<=1;error_code_q<=4'd7;
    end
   end
   if(busy_q&&quiesced_q&&(ended_q||last_fire))begin
    busy_q<=0;done_q<=1;abort_q<=0;
    if(cancel_q)begin success_q<=0;cancelled_q<=1;end
   end
   if(commit)begin
    awheld_q<=0;wheld_q<=0;bvalid_q<=1;bresp_q<=write_ok?2'b00:2'b10;
    if(write_ok&&wstrb_q!=0)case(awaddr_q)
     12'h008:case(wdata_q)
      32'd1:begin busy_q<=1;pending_q<=1;done_q<=0;success_q<=0;error_q<=0;cancelled_q<=0;
       cancel_q<=0;quiesced_q<=0;ended_q<=0;abort_q<=0;error_code_q<=0;bytes_q<=0;end
      32'd2:begin
       cancel_q<=1;success_q<=0;cancelled_q<=1;
       // A command not accepted yet owns no AR. Otherwise wait actual core drain.
       if(pending_q&&!cmd_fire)begin pending_q<=0;quiesced_q<=1;cancelled_q<=1;success_q<=0;abort_q<=1;end
      end
      32'd4:begin done_q<=0;success_q<=0;error_q<=0;cancelled_q<=0;cancel_q<=0;quiesced_q<=0;ended_q<=0;error_code_q<=0;end
      32'd8:route_q<=1;
      32'd16:route_q<=0;
      default:begin end
     endcase
     12'h020:source_addr_o[31:0]<=wdata_q;12'h024:source_addr_o[63:32]<=wdata_q;
     12'h028:map_addr_o[31:0]<=wdata_q;12'h02c:map_addr_o[63:32]<=wdata_q;
     12'h030:dims_q<=wdata_q;12'h034:source_stride_o<=wdata_q;
     12'h038:ticket_o[31:0]<=wdata_q;12'h03c:ticket_o[63:32]<=wdata_q;
     default:begin end
    endcase
   end
  end
 end
endmodule
