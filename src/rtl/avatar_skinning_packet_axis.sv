// AXI4-Stream packet wrapper for one complete avatar skinning batch.
//
// Packet format is intentionally fixed-width and DMA friendly:
//   - first 32 beats: 16 matrix records, two 128-bit beats per record;
//     beat 0 carries coefficients 0..7, beat 1 carries coefficients 8..11
//     in bits [63:0] and leaves [127:64] reserved;
//   - remaining beats: the 128-bit vertex_skinning_axis request layout;
//   - TLAST is meaningful only on the final vertex beat.
//
// The wrapper keeps the matrix programming path in the same DMA stream as the
// vertex batch. It therefore needs no extra AXI-Lite register bank or second
// DMA channel, while the inner axis core still owns arithmetic backpressure.
module avatar_skinning_packet_axis #(
    parameter bit RESOURCE_SHARED = 1'b0
) (
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

    typedef enum logic [1:0] {
        MATRIX_FIRST = 2'b00,
        MATRIX_SECOND = 2'b01,
        VERTEX_DATA = 2'b10
    } parser_state_t;

    parser_state_t parser_state_q;
    logic [3:0] matrix_index_q;
    logic [127:0] matrix_first_half_q;
    logic [191:0] matrix_wr_data;
    logic [3:0] matrix_wr_index;
    logic matrix_wr_valid;
    logic matrix_wr_ready;
    logic core_reset;
    logic core_in_valid;
    logic core_in_ready;
    logic core_out_valid;
    logic core_out_ready;
    logic [127:0] core_in_data;
    logic core_in_last;
    logic [63:0] core_out_data;
    logic core_out_last;
    logic input_fire;

    assign core_reset = ~axis_aresetn;
    assign core_in_valid = s_axis_tvalid && (parser_state_q == VERTEX_DATA);
    assign core_in_data = s_axis_tdata;
    assign core_in_last = s_axis_tlast;
    assign core_out_ready = m_axis_tready;
    assign s_axis_tready = axis_aresetn && ((parser_state_q == VERTEX_DATA)
        ? core_in_ready
        : ((parser_state_q == MATRIX_SECOND) ? matrix_wr_ready : 1'b1));
    assign input_fire = s_axis_tvalid && s_axis_tready;

    // The serial backend can still be reading a prior frame's matrix bank.
    // Its ready stalls the second header half until that vertex has retired.
    assign matrix_wr_valid = axis_aresetn && s_axis_tvalid && (parser_state_q == MATRIX_SECOND);
    assign matrix_wr_index = matrix_index_q;
    assign matrix_wr_data = {s_axis_tdata[63:0], matrix_first_half_q};

    assign m_axis_tvalid = core_out_valid;
    assign m_axis_tdata = {64'b0, core_out_data};
    assign m_axis_tkeep = 16'hffff;
    assign m_axis_tlast = core_out_last;

    always_ff @(posedge axis_aclk) begin
        if (!axis_aresetn) begin
            parser_state_q <= MATRIX_FIRST;
            matrix_index_q <= '0;
            matrix_first_half_q <= '0;
        end else if (input_fire) begin
            case (parser_state_q)
                MATRIX_FIRST: begin
                    matrix_first_half_q <= s_axis_tdata;
                    parser_state_q <= MATRIX_SECOND;
                end
                MATRIX_SECOND: begin
                    if (matrix_index_q == 4'd15) begin
                        parser_state_q <= VERTEX_DATA;
                    end else begin
                        matrix_index_q <= matrix_index_q + 1'b1;
                        parser_state_q <= MATRIX_FIRST;
                    end
                end
                default: begin
                    // TLAST closes one complete frame. The following beat is
                    // the first half of matrix 0 for the next frame; no reset
                    // is required between real-time DMA packets.
                    if (s_axis_tlast) begin
                        parser_state_q <= MATRIX_FIRST;
                        matrix_index_q <= '0;
                    end
                end
            endcase
        end
    end

    generate if (RESOURCE_SHARED) begin : shared_backend
    vertex_skinning_serial_axis core (
        .clk_i(axis_aclk),
        .rst_i(core_reset),
        .matrix_wr_valid_i(matrix_wr_valid),
        .matrix_wr_ready_o(matrix_wr_ready),
        .matrix_wr_index_i(matrix_wr_index),
        .matrix_wr_data_i(matrix_wr_data),
        .in_tvalid_i(core_in_valid),
        .in_tready_o(core_in_ready),
        .in_tdata_i(core_in_data),
        .in_tlast_i(core_in_last),
        .out_tvalid_o(core_out_valid),
        .out_tready_i(core_out_ready),
        .out_tdata_o(core_out_data),
        .out_tlast_o(core_out_last)
    );
    end else begin : parallel_backend
    vertex_skinning_axis core (
        .clk_i(axis_aclk),
        .rst_i(core_reset),
        .matrix_wr_valid_i(matrix_wr_valid),
        .matrix_wr_ready_o(matrix_wr_ready),
        .matrix_wr_index_i(matrix_wr_index),
        .matrix_wr_data_i(matrix_wr_data),
        .in_tvalid_i(core_in_valid),
        .in_tready_o(core_in_ready),
        .in_tdata_i(core_in_data),
        .in_tlast_i(core_in_last),
        .out_tvalid_o(core_out_valid),
        .out_tready_i(core_out_ready),
        .out_tdata_o(core_out_data),
        .out_tlast_o(core_out_last)
    );
    end endgenerate

endmodule
