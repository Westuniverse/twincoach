// SPDX-License-Identifier: GPL-2.0-only
// Module-reference metadata only; underlying frozen operator ABI is unchanged.
module tail4_operator_axi_bd_joint (
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 aclk CLK" *)
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME aclk, ASSOCIATED_BUSIF S_AXI:M_AXI, ASSOCIATED_RESET aresetn" *)
    input wire  aclk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 aresetn RST" *)
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME aresetn, POLARITY ACTIVE_LOW" *)
    input wire  aresetn,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI AWADDR" *)
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME S_AXI, PROTOCOL AXI4LITE, DATA_WIDTH 32, ADDR_WIDTH 12" *)
    input wire [11:0] s_axi_awaddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI AWPROT" *)
    input wire [2:0] s_axi_awprot,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI AWVALID" *)
    input wire  s_axi_awvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI AWREADY" *)
    output wire  s_axi_awready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI WDATA" *)
    input wire [31:0] s_axi_wdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI WSTRB" *)
    input wire [3:0] s_axi_wstrb,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI WVALID" *)
    input wire  s_axi_wvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI WREADY" *)
    output wire  s_axi_wready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI BRESP" *)
    output wire [1:0] s_axi_bresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI BVALID" *)
    output wire  s_axi_bvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI BREADY" *)
    input wire  s_axi_bready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI ARADDR" *)
    input wire [11:0] s_axi_araddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI ARPROT" *)
    input wire [2:0] s_axi_arprot,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI ARVALID" *)
    input wire  s_axi_arvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI ARREADY" *)
    output wire  s_axi_arready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI RDATA" *)
    output wire [31:0] s_axi_rdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI RRESP" *)
    output wire [1:0] s_axi_rresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI RVALID" *)
    output wire  s_axi_rvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI RREADY" *)
    input wire  s_axi_rready,
    (* X_INTERFACE_INFO = "xilinx.com:signal:interrupt:1.0 irq INTERRUPT" *)
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME irq, SENSITIVITY LEVEL_HIGH" *)
    output wire  irq,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI ARADDR" *)
    output wire [63:0] m_axi_araddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI ARLEN" *)
    output wire [7:0] m_axi_arlen,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI ARSIZE" *)
    output wire [2:0] m_axi_arsize,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI ARPROT" *)
    output wire [2:0] m_axi_arprot,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI ARBURST" *)
    output wire [1:0] m_axi_arburst,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI ARCACHE" *)
    output wire [3:0] m_axi_arcache,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI ARQOS" *)
    output wire [3:0] m_axi_arqos,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI ARID" *)
    output wire  m_axi_arid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI ARLOCK" *)
    output wire  m_axi_arlock,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI ARVALID" *)
    output wire  m_axi_arvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI ARREADY" *)
    input wire  m_axi_arready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI RDATA" *)
    input wire [127:0] m_axi_rdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI RRESP" *)
    input wire [1:0] m_axi_rresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI RID" *)
    input wire  m_axi_rid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI RLAST" *)
    input wire  m_axi_rlast,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI RVALID" *)
    input wire  m_axi_rvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI RREADY" *)
    output wire  m_axi_rready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI AWADDR" *)
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME M_AXI, PROTOCOL AXI4, DATA_WIDTH 128, ADDR_WIDTH 64, ID_WIDTH 1, HAS_BURST 1, MAX_BURST_LENGTH 256, NUM_READ_OUTSTANDING 1, NUM_WRITE_OUTSTANDING 1" *)
    output wire [63:0] m_axi_awaddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI AWLEN" *)
    output wire [7:0] m_axi_awlen,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI AWSIZE" *)
    output wire [2:0] m_axi_awsize,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI AWPROT" *)
    output wire [2:0] m_axi_awprot,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI AWBURST" *)
    output wire [1:0] m_axi_awburst,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI AWCACHE" *)
    output wire [3:0] m_axi_awcache,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI AWQOS" *)
    output wire [3:0] m_axi_awqos,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI AWID" *)
    output wire  m_axi_awid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI AWLOCK" *)
    output wire  m_axi_awlock,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI AWVALID" *)
    output wire  m_axi_awvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI AWREADY" *)
    input wire  m_axi_awready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI WDATA" *)
    output wire [127:0] m_axi_wdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI WSTRB" *)
    output wire [15:0] m_axi_wstrb,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI WLAST" *)
    output wire  m_axi_wlast,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI WVALID" *)
    output wire  m_axi_wvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI WREADY" *)
    input wire  m_axi_wready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI BRESP" *)
    input wire [1:0] m_axi_bresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI BID" *)
    input wire  m_axi_bid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI BVALID" *)
    input wire  m_axi_bvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 M_AXI BREADY" *)
    output wire  m_axi_bready
);
    tail4_operator_axi core (
        .clk_i(aclk),
        .rst_i(~aresetn),
        .s_axi_awaddr_i(s_axi_awaddr),
        .s_axi_awprot_i(s_axi_awprot),
        .s_axi_awvalid_i(s_axi_awvalid),
        .s_axi_awready_o(s_axi_awready),
        .s_axi_wdata_i(s_axi_wdata),
        .s_axi_wstrb_i(s_axi_wstrb),
        .s_axi_wvalid_i(s_axi_wvalid),
        .s_axi_wready_o(s_axi_wready),
        .s_axi_bresp_o(s_axi_bresp),
        .s_axi_bvalid_o(s_axi_bvalid),
        .s_axi_bready_i(s_axi_bready),
        .s_axi_araddr_i(s_axi_araddr),
        .s_axi_arprot_i(s_axi_arprot),
        .s_axi_arvalid_i(s_axi_arvalid),
        .s_axi_arready_o(s_axi_arready),
        .s_axi_rdata_o(s_axi_rdata),
        .s_axi_rresp_o(s_axi_rresp),
        .s_axi_rvalid_o(s_axi_rvalid),
        .s_axi_rready_i(s_axi_rready),
        .irq_o(irq),
        .m_axi_araddr_o(m_axi_araddr),
        .m_axi_arlen_o(m_axi_arlen),
        .m_axi_arsize_o(m_axi_arsize),
        .m_axi_arprot_o(m_axi_arprot),
        .m_axi_arburst_o(m_axi_arburst),
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
        .m_axi_awaddr_o(m_axi_awaddr),
        .m_axi_awlen_o(m_axi_awlen),
        .m_axi_awsize_o(m_axi_awsize),
        .m_axi_awprot_o(m_axi_awprot),
        .m_axi_awburst_o(m_axi_awburst),
        .m_axi_awcache_o(m_axi_awcache),
        .m_axi_awqos_o(m_axi_awqos),
        .m_axi_awid_o(m_axi_awid),
        .m_axi_awlock_o(m_axi_awlock),
        .m_axi_awvalid_o(m_axi_awvalid),
        .m_axi_awready_i(m_axi_awready),
        .m_axi_wdata_o(m_axi_wdata),
        .m_axi_wstrb_o(m_axi_wstrb),
        .m_axi_wlast_o(m_axi_wlast),
        .m_axi_wvalid_o(m_axi_wvalid),
        .m_axi_wready_i(m_axi_wready),
        .m_axi_bresp_i(m_axi_bresp),
        .m_axi_bid_i(m_axi_bid),
        .m_axi_bvalid_i(m_axi_bvalid),
        .m_axi_bready_o(m_axi_bready)
    );
endmodule
