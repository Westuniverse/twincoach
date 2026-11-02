// AXI-Lite config + AXI-Stream shared pose datapath, all in axis_aclk domain.
// Default PPS2; INCLUDE_RASTER=1 adds PPS3, op4, capability 0x00c and texture_valid[2].
// 0x00: ID; 0x04: status; 0x08: operator (reset=7).
// Only full-word legal operator writes to 0x08 while idle succeed. Others SLVERR.
module shared_pose_accel_axis_ip #(
    parameter bit INCLUDE_RASTER = 1'b0,
    parameter bit INCLUDE_SKINNING = 1'b0,
    parameter bit SKINNING_RESOURCE_SHARED = 1'b1
) (
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 axis_aclk CLK" *)
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME axis_aclk, ASSOCIATED_BUSIF s_axi:s_axis:m_axis, ASSOCIATED_RESET axis_aresetn" *)
    input logic axis_aclk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 axis_aresetn RST" *)
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME axis_aresetn, POLARITY ACTIVE_LOW" *)
    input logic axis_aresetn,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi AWADDR" *)
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME s_axi, PROTOCOL AXI4LITE, DATA_WIDTH 32, ADDR_WIDTH 12, READ_WRITE_MODE READ_WRITE" *)
    input logic [11:0] s_axi_awaddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi AWPROT" *)
    input logic [2:0] s_axi_awprot,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi AWVALID" *)
    input logic s_axi_awvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi AWREADY" *)
    output logic s_axi_awready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi WDATA" *)
    input logic [31:0] s_axi_wdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi WSTRB" *)
    input logic [3:0] s_axi_wstrb,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi WVALID" *)
    input logic s_axi_wvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi WREADY" *)
    output logic s_axi_wready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi BRESP" *)
    output logic [1:0] s_axi_bresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi BVALID" *)
    output logic s_axi_bvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi BREADY" *)
    input logic s_axi_bready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi ARADDR" *)
    input logic [11:0] s_axi_araddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi ARPROT" *)
    input logic [2:0] s_axi_arprot,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi ARVALID" *)
    input logic s_axi_arvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi ARREADY" *)
    output logic s_axi_arready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi RDATA" *)
    output logic [31:0] s_axi_rdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi RRESP" *)
    output logic [1:0] s_axi_rresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi RVALID" *)
    output logic s_axi_rvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi RREADY" *)
    input logic s_axi_rready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 s_axis TVALID" *)
    input logic s_axis_tvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 s_axis TREADY" *)
    output logic s_axis_tready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 s_axis TDATA" *)
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME s_axis, TDATA_NUM_BYTES 16, HAS_TLAST 1, HAS_TKEEP 0" *)
    input logic [127:0] s_axis_tdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 s_axis TLAST" *)
    input logic s_axis_tlast,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 m_axis TVALID" *)
    output logic m_axis_tvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 m_axis TREADY" *)
    input logic m_axis_tready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 m_axis TDATA" *)
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME m_axis, TDATA_NUM_BYTES 16, HAS_TLAST 1, HAS_TKEEP 1" *)
    output logic [127:0] m_axis_tdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 m_axis TKEEP" *)
    output logic [15:0] m_axis_tkeep,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 m_axis TLAST" *)
    output logic m_axis_tlast
);
    logic aw_pending, w_pending, bvalid_q, rvalid_q;
    logic [11:0] awaddr_q;
    logic [31:0] wdata_q, rdata_q;
    logic [3:0] wstrb_q;
    logic [1:0] bresp_q, rresp_q;
    logic [2:0] operator_q;
    logic configured_q, core_busy, cfg_ready, cfg_error, texture_valid;
    logic write_commit, legal_write, cfg_valid;

    assign s_axi_awready = axis_aresetn && !aw_pending && !bvalid_q;
    assign s_axi_wready = axis_aresetn && !w_pending && !bvalid_q;
    assign s_axi_arready = axis_aresetn && !rvalid_q;
    assign s_axi_bvalid = axis_aresetn && bvalid_q;
    assign s_axi_bresp = bresp_q;
    assign s_axi_rvalid = axis_aresetn && rvalid_q;
    assign s_axi_rresp = rresp_q;
    assign s_axi_rdata = rdata_q;
    assign write_commit = axis_aresetn && aw_pending && w_pending && !bvalid_q;
    assign legal_write = awaddr_q == 12'h008 && wstrb_q == 4'hf &&
        ((wdata_q <= 32'd3) || (INCLUDE_RASTER && wdata_q == 32'd4) ||
         (INCLUDE_SKINNING && wdata_q == 32'd5));
    // At this edge configuration wins over new stream input. An already busy
    // packet causes SLVERR immediately, never a queued post-packet switch.
    assign cfg_valid = write_commit && legal_write && !core_busy;

    always_ff @(posedge axis_aclk) begin
        if (!axis_aresetn) begin
            aw_pending <= 1'b0;
            w_pending <= 1'b0;
            awaddr_q <= '0;
            wdata_q <= '0;
            wstrb_q <= '0;
            bvalid_q <= 1'b0;
            bresp_q <= '0;
            rvalid_q <= 1'b0;
            rdata_q <= '0;
            rresp_q <= '0;
            operator_q <= 3'd7;
            configured_q <= 1'b0;
        end else begin
            if (s_axi_awvalid && s_axi_awready) begin
                aw_pending <= 1'b1;
                awaddr_q <= s_axi_awaddr;
            end
            if (s_axi_wvalid && s_axi_wready) begin
                w_pending <= 1'b1;
                wdata_q <= s_axi_wdata;
                wstrb_q <= s_axi_wstrb;
            end
            if (s_axi_bvalid && s_axi_bready) bvalid_q <= 1'b0;
            if (write_commit) begin
                aw_pending <= 1'b0;
                w_pending <= 1'b0;
                bvalid_q <= 1'b1;
                bresp_q <= cfg_valid && cfg_ready ? 2'b00 : 2'b10;
                if (cfg_valid && cfg_ready) begin
                    operator_q <= wdata_q[2:0];
                    configured_q <= 1'b1;
                end
            end
            if (s_axi_rvalid && s_axi_rready) rvalid_q <= 1'b0;
            if (s_axi_arvalid && s_axi_arready) begin
                rvalid_q <= 1'b1;
                rresp_q <= 2'b00;
                case (s_axi_araddr)
                    12'h000: rdata_q <= INCLUDE_SKINNING ? 32'h50505334 :
                        (INCLUDE_RASTER ? 32'h50505333 : 32'h50505332);
                    12'h004: rdata_q <= {29'd0, texture_valid, configured_q, core_busy};
                    12'h008: rdata_q <= {29'd0, operator_q};
                    12'h00c: if (INCLUDE_RASTER || INCLUDE_SKINNING)
                              rdata_q <= 32'hf | (INCLUDE_RASTER ? 32'h10 : 0) |
                                  (INCLUDE_SKINNING ? 32'h20 : 0);
                              else begin rdata_q <= '0; rresp_q <= 2'b10; end
                    default: begin rdata_q <= '0; rresp_q <= 2'b10; end
                endcase
            end
        end
    end

    shared_pose_accel_axis #(.INCLUDE_RASTER(INCLUDE_RASTER), .INCLUDE_SKINNING(INCLUDE_SKINNING),
        .SKINNING_RESOURCE_SHARED(SKINNING_RESOURCE_SHARED)) datapath (
        .clk_i(axis_aclk), .rst_i(!axis_aresetn),
        .cfg_valid_i(cfg_valid), .cfg_ready_o(cfg_ready),
        .cfg_operator_i(wdata_q[2:0]), .cfg_error_o(cfg_error), .busy_o(core_busy), .texture_valid_o(texture_valid),
        .s_axis_tvalid_i(s_axis_tvalid), .s_axis_tready_o(s_axis_tready),
        .s_axis_tdata_i(s_axis_tdata), .s_axis_tlast_i(s_axis_tlast),
        .m_axis_tvalid_o(m_axis_tvalid), .m_axis_tready_i(m_axis_tready),
        .m_axis_tdata_o(m_axis_tdata), .m_axis_tkeep_o(m_axis_tkeep),
        .m_axis_tlast_o(m_axis_tlast)
    );
endmodule
