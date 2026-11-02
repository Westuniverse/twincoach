module joint_metrics_axis_ip (
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 axis_aclk CLK" *)
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME axis_aclk, ASSOCIATED_BUSIF s_axis:m_axis, ASSOCIATED_RESET axis_aresetn" *)
    input  logic          axis_aclk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 axis_aresetn RST" *)
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME axis_aresetn, POLARITY ACTIVE_LOW" *)
    input  logic          axis_aresetn,

    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 s_axis TVALID" *)
    input  logic          s_axis_tvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 s_axis TREADY" *)
    output logic          s_axis_tready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 s_axis TDATA" *)
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME s_axis, TDATA_NUM_BYTES 16, HAS_TLAST 1, HAS_TKEEP 0" *)
    input  logic [127:0]  s_axis_tdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 s_axis TLAST" *)
    input  logic          s_axis_tlast,

    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 m_axis TVALID" *)
    output logic          m_axis_tvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 m_axis TREADY" *)
    input  logic          m_axis_tready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 m_axis TDATA" *)
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME m_axis, TDATA_NUM_BYTES 16, HAS_TLAST 1, HAS_TKEEP 1" *)
    output logic [127:0]  m_axis_tdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 m_axis TKEEP" *)
    output logic [15:0]   m_axis_tkeep,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 m_axis TLAST" *)
    output logic          m_axis_tlast
);

    logic core_reset;

    assign core_reset = ~axis_aresetn;
    assign m_axis_tkeep = 16'hffff;

    joint_metrics_axis metrics_axis (
        .clk_i(axis_aclk),
        .rst_i(core_reset),
        .s_axis_tvalid_i(s_axis_tvalid),
        .s_axis_tready_o(s_axis_tready),
        .s_axis_tdata_i(s_axis_tdata),
        .s_axis_tlast_i(s_axis_tlast),
        .m_axis_tvalid_o(m_axis_tvalid),
        .m_axis_tready_i(m_axis_tready),
        .m_axis_tdata_o(m_axis_tdata),
        .m_axis_tlast_o(m_axis_tlast)
    );

endmodule
