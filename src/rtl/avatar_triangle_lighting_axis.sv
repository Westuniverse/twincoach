// Two full AXIS beats per face; TLAST is a whole face-batch boundary.
module avatar_triangle_lighting_axis (
    input  logic         clk_i,
    input  logic         rst_i,
    input  logic [127:0]  s_axis_tdata_i,
    input  logic [15:0]   s_axis_tkeep_i,
    input  logic         s_axis_tvalid_i,
    output logic         s_axis_tready_o,
    input  logic         s_axis_tlast_i,
    output logic [127:0]  m_axis_tdata_o,
    output logic [15:0]   m_axis_tkeep_o,
    output logic         m_axis_tvalid_o,
    input  logic         m_axis_tready_i,
    output logic         m_axis_tlast_o,
    output logic         protocol_error_o
);
    typedef enum logic [2:0] {FIRST, SECOND, START_CORE, WAIT_CORE, OUTPUT_RESULT} state_t;
    state_t state_q;
    logic [255:0] packet_q;
    logic first_error_q, core_ready, core_valid, core_degenerate;
    logic [15:0] core_light;
    logic [23:0] core_color;
    logic [51:0] core_length;

    assign s_axis_tready_o = !rst_i && ((state_q == FIRST) || (state_q == SECOND));
    assign m_axis_tvalid_o = !rst_i && (state_q == OUTPUT_RESULT);
    assign m_axis_tkeep_o = 16'hffff;
    avatar_triangle_lighting_core core (
        .clk_i(clk_i), .rst_i(rst_i),
        .in_valid_i(state_q == START_CORE), .in_ready_o(core_ready),
        .points_i(packet_q[215:0]), .color_rgb_i(packet_q[239:216]),
        .out_valid_o(core_valid), .out_ready_i(state_q == WAIT_CORE),
        .light_q15_o(core_light), .color_bgr_o(core_color), .degenerate_o(core_degenerate),
        .normal_length_o(core_length)
    );
    always_ff @(posedge clk_i) begin
        if (rst_i) begin
            state_q <= FIRST;
            protocol_error_o <= 1'b0;
        end else begin
            case (state_q)
                FIRST: if (s_axis_tvalid_i) begin
                    if (s_axis_tlast_i) begin
                        // No complete face_id exists after a one-beat packet.
                        m_axis_tdata_o <= {70'd0, 1'b1, 1'b0, 16'hffff, 40'd0};
                        m_axis_tlast_o <= 1'b1;
                        protocol_error_o <= 1'b1;
                        state_q <= OUTPUT_RESULT;
                    end else begin
                        packet_q[127:0] <= s_axis_tdata_i;
                        first_error_q <= s_axis_tkeep_i != 16'hffff;
                        state_q <= SECOND;
                    end
                end
                SECOND: if (s_axis_tvalid_i) begin
                    packet_q[255:128] <= s_axis_tdata_i;
                    m_axis_tlast_o <= s_axis_tlast_i;
                    if (first_error_q || s_axis_tkeep_i != 16'hffff) begin
                        m_axis_tdata_o <= {70'd0, 1'b1, 1'b0, s_axis_tdata_i[127:112], 40'd0};
                        protocol_error_o <= 1'b1;
                        state_q <= OUTPUT_RESULT;
                    end else state_q <= START_CORE;
                end
                START_CORE: if (core_ready) state_q <= WAIT_CORE;
                WAIT_CORE: if (core_valid) begin
                    m_axis_tdata_o <= {18'd0, core_length, 1'b0, core_degenerate,
                        packet_q[255:240], core_light, core_color};
                    state_q <= OUTPUT_RESULT;
                end
                OUTPUT_RESULT: if (m_axis_tready_i) state_q <= FIRST;
                default: state_q <= FIRST;
            endcase
        end
    end
endmodule
