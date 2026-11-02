// SPDX-License-Identifier: GPL-2.0-only
// Private ROI1 BD module: exact sampler plus control and externally selected RX.
module roi_dma_sampler_ip(
 (* X_INTERFACE_INFO="xilinx.com:signal:clock:1.0 aclk CLK" *)
 (* X_INTERFACE_PARAMETER="XIL_INTERFACENAME aclk, ASSOCIATED_BUSIF s_axi:M_AXI:s_axis_legacy:m_axis, ASSOCIATED_RESET aresetn" *)
 input logic aclk,
 (* X_INTERFACE_INFO="xilinx.com:signal:reset:1.0 aresetn RST" *)
 (* X_INTERFACE_PARAMETER="XIL_INTERFACENAME aresetn, POLARITY ACTIVE_LOW" *)
 input logic aresetn,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 s_axi AWADDR" *)
 (* X_INTERFACE_PARAMETER="XIL_INTERFACENAME s_axi, PROTOCOL AXI4LITE, DATA_WIDTH 32, ADDR_WIDTH 12, READ_WRITE_MODE READ_WRITE" *)
 input logic[11:0] s_axi_awaddr,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 s_axi AWVALID" *)
 input logic s_axi_awvalid,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 s_axi AWREADY" *)
 output logic s_axi_awready,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 s_axi AWPROT" *)
 input logic[2:0] s_axi_awprot,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 s_axi WDATA" *)
 input logic[31:0] s_axi_wdata,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 s_axi WSTRB" *)
 input logic[3:0] s_axi_wstrb,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 s_axi WVALID" *)
 input logic s_axi_wvalid,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 s_axi WREADY" *)
 output logic s_axi_wready,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 s_axi BRESP" *)
 output logic[1:0] s_axi_bresp,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 s_axi BVALID" *)
 output logic s_axi_bvalid,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 s_axi BREADY" *)
 input logic s_axi_bready,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 s_axi ARADDR" *)
 input logic[11:0] s_axi_araddr,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 s_axi ARPROT" *)
 input logic[2:0] s_axi_arprot,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 s_axi ARVALID" *)
 input logic s_axi_arvalid,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 s_axi ARREADY" *)
 output logic s_axi_arready,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 s_axi RDATA" *)
 output logic[31:0] s_axi_rdata,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 s_axi RRESP" *)
 output logic[1:0] s_axi_rresp,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 s_axi RVALID" *)
 output logic s_axi_rvalid,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 s_axi RREADY" *)
 input logic s_axi_rready,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 M_AXI ARADDR" *)
 (* X_INTERFACE_PARAMETER="XIL_INTERFACENAME M_AXI, PROTOCOL AXI4, ADDR_WIDTH 64, DATA_WIDTH 128, ID_WIDTH 1, READ_WRITE_MODE READ_ONLY, MAX_BURST_LENGTH 256, NUM_READ_OUTSTANDING 1" *)
 output logic[63:0] m_axi_araddr,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 M_AXI ARLEN" *)
 output logic[7:0] m_axi_arlen,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 M_AXI ARSIZE" *)
 output logic[2:0] m_axi_arsize,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 M_AXI ARBURST" *)
 output logic[1:0] m_axi_arburst,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 M_AXI ARPROT" *)
 output logic[2:0] m_axi_arprot,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 M_AXI ARCACHE" *)
 output logic[3:0] m_axi_arcache,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 M_AXI ARQOS" *)
 output logic[3:0] m_axi_arqos,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 M_AXI ARID" *)
 output logic m_axi_arid,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 M_AXI ARLOCK" *)
 output logic m_axi_arlock,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 M_AXI ARVALID" *)
 output logic m_axi_arvalid,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 M_AXI ARREADY" *)
 input logic m_axi_arready,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 M_AXI RDATA" *)
 input logic[127:0] m_axi_rdata,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 M_AXI RRESP" *)
 input logic[1:0] m_axi_rresp,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 M_AXI RID" *)
 input logic m_axi_rid,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 M_AXI RLAST" *)
 input logic m_axi_rlast,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 M_AXI RVALID" *)
 input logic m_axi_rvalid,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 M_AXI RREADY" *)
 output logic m_axi_rready,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 s_axis_legacy TDATA" *)
 (* X_INTERFACE_PARAMETER="XIL_INTERFACENAME s_axis_legacy, TDATA_NUM_BYTES 16, HAS_TKEEP 1, HAS_TLAST 1" *)
 input logic[127:0] s_axis_legacy_tdata,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 s_axis_legacy TKEEP" *)
 input logic[15:0] s_axis_legacy_tkeep,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 s_axis_legacy TVALID" *)
 input logic s_axis_legacy_tvalid,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 s_axis_legacy TLAST" *)
 input logic s_axis_legacy_tlast,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 s_axis_legacy TREADY" *)
 output logic s_axis_legacy_tready,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 m_axis TDATA" *)
 (* X_INTERFACE_PARAMETER="XIL_INTERFACENAME m_axis, TDATA_NUM_BYTES 16, HAS_TKEEP 1, HAS_TLAST 1" *)
 output logic[127:0] m_axis_tdata,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 m_axis TKEEP" *)
 output logic[15:0] m_axis_tkeep,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 m_axis TVALID" *)
 output logic m_axis_tvalid,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 m_axis TLAST" *)
 output logic m_axis_tlast,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 m_axis TREADY" *)
 input logic m_axis_tready
);
 logic cmd_valid,cmd_ready,cancel,busy,done,success,error,cancelled,read_quiesced;
 logic[63:0]source_addr,map_addr,ticket,completed_ticket,roi_user;
 logic[11:0]width,height;logic[31:0]stride;logic[3:0]error_code;
 logic[127:0]roi_data;logic[15:0]roi_keep;logic roi_valid,roi_last,roi_ready;
 roi_control_rx_axis128 control(
  .clk_i(aclk),
  .rst_i(!aresetn),
  .s_axi_awaddr(s_axi_awaddr),
  .s_axi_awvalid(s_axi_awvalid),
  .s_axi_awready(s_axi_awready),
  .s_axi_wdata(s_axi_wdata),
  .s_axi_wstrb(s_axi_wstrb),
  .s_axi_wvalid(s_axi_wvalid),
  .s_axi_wready(s_axi_wready),
  .s_axi_bresp(s_axi_bresp),
  .s_axi_bvalid(s_axi_bvalid),
  .s_axi_bready(s_axi_bready),
  .s_axi_araddr(s_axi_araddr),
  .s_axi_arvalid(s_axi_arvalid),
  .s_axi_arready(s_axi_arready),
  .s_axi_rdata(s_axi_rdata),
  .s_axi_rresp(s_axi_rresp),
  .s_axi_rvalid(s_axi_rvalid),
  .s_axi_rready(s_axi_rready),
  .core_cmd_valid_o(cmd_valid),
  .core_cmd_ready_i(cmd_ready),
  .source_addr_o(source_addr),
  .map_addr_o(map_addr),
  .ticket_o(ticket),
  .source_width_o(width),
  .source_height_o(height),
  .source_stride_o(stride),
  .core_cancel_o(cancel),
  .core_busy_i(busy),
  .core_done_i(done),
  .core_success_i(success),
  .core_error_i(error),
  .core_cancelled_i(cancelled),
  .core_read_quiesced_i(read_quiesced),
  .core_error_code_i(error_code),
  .roi_tdata_i(roi_data),
  .legacy_tdata_i(s_axis_legacy_tdata),
  .roi_tkeep_i(roi_keep),
  .legacy_tkeep_i(s_axis_legacy_tkeep),
  .roi_tvalid_i(roi_valid),
  .legacy_tvalid_i(s_axis_legacy_tvalid),
  .roi_tlast_i(roi_last),
  .legacy_tlast_i(s_axis_legacy_tlast),
  .roi_tready_o(roi_ready),
  .legacy_tready_o(s_axis_legacy_tready),
  .m_axis_tdata_o(m_axis_tdata),
  .m_axis_tkeep_o(m_axis_tkeep),
  .m_axis_tvalid_o(m_axis_tvalid),
  .m_axis_tlast_o(m_axis_tlast),
  .m_axis_tready_i(m_axis_tready)
 );
 roi_dma_sampler_axis128 sampler(
  .clk_i(aclk),
  .rst_i(!aresetn),
  .cmd_valid_i(cmd_valid),
  .cmd_ready_o(cmd_ready),
  .source_addr_i(source_addr),
  .map_addr_i(map_addr),
  .ticket_i(ticket),
  .source_width_i(width),
  .source_height_i(height),
  .source_stride_i(stride),
  .cancel_i(cancel),
  .busy_o(busy),
  .done_o(done),
  .success_o(success),
  .error_o(error),
  .cancelled_o(cancelled),
  .read_quiesced_o(read_quiesced),
  .ticket_o(completed_ticket),
  .error_code_o(error_code),
  .m_axi_araddr_o(m_axi_araddr),
  .m_axi_arlen_o(m_axi_arlen),
  .m_axi_arsize_o(m_axi_arsize),
  .m_axi_arburst_o(m_axi_arburst),
  .m_axi_arprot_o(m_axi_arprot),
  .m_axi_arcache_o(m_axi_arcache),
  .m_axi_arqos_o(m_axi_arqos),
  .m_axi_arid_o(m_axi_arid),
  .m_axi_arlock_o(m_axi_arlock),
  .m_axi_arvalid_o(m_axi_arvalid),
  .m_axi_arready_i(m_axi_arready),
  .m_axi_rdata_i(m_axi_rdata),
  .m_axi_rresp_i(m_axi_rresp),
  .m_axi_rid_i(m_axi_rid),
  .m_axi_rlast_i(m_axi_rlast),
  .m_axi_rvalid_i(m_axi_rvalid),
  .m_axi_rready_o(m_axi_rready),
  .m_axis_tdata_o(roi_data),
  .m_axis_tkeep_o(roi_keep),
  .m_axis_tvalid_o(roi_valid),
  .m_axis_tlast_o(roi_last),
  .m_axis_tuser_o(roi_user),
  .m_axis_tready_i(roi_ready)
 );
endmodule
