// SPDX-License-Identifier: GPL-2.0-only
// Exact 5-bit-fraction BGR8 -> rounded RGB8 -> original RGB S8Q7.
// The four neighbors are already BORDER_REPLICATE gathered by the caller.
// Reuses the project's stallable bilinear leaf; adds no multiplier or buffer RAM.
module crop_bilinear_q7_axis (
    input  logic clk_i,
    input  logic rst_i,
    input  logic s_valid_i,
    output logic s_ready_o,
    input  logic [23:0] p00_bgr_i, p10_bgr_i, p01_bgr_i, p11_bgr_i,
    input  logic [4:0] fx_q5_i, fy_q5_i,
    input  logic [31:0] tag_i,
    input  logic eol_i, eof_i,
    output logic m_valid_o,
    input  logic m_ready_i,
    output logic [23:0] rgb_u8_o,
    output logic [23:0] rgb_q7_o,
    output logic [31:0] tag_o,
    output logic eol_o, eof_o,
    output logic error_o
);
    logic [23:0] sampled_bgr;
    logic [3:0] eol_q;
    logic sample_valid, sample_ready, sample_eof, sample_error;
    logic [31:0] sample_tag;

    function automatic logic [7:0] to_q7(input logic [7:0] value);
        logic [8:0] incremented;
        begin
            incremented = {1'b0,value} + 9'd1;
            to_q7 = incremented[8:1] > 8'd127 ? 8'd127 : incremented[8:1];
        end
    endfunction

    avatar_bilinear_rgb8_axis sampler (
        .clk_i, .rst_i,
        .in_valid_i(s_valid_i), .in_ready_o(s_ready_o),
        .p00_rgb_i(p00_bgr_i), .p10_rgb_i(p10_bgr_i),
        .p01_rgb_i(p01_bgr_i), .p11_rgb_i(p11_bgr_i),
        .u_i({fx_q5_i,3'b0}), .v_i({fy_q5_i,3'b0}), .shade_i(9'd256),
        .tag_i, .last_i(eof_i),
        .out_valid_o(sample_valid), .out_ready_i(sample_ready),
        .rgb_o(sampled_bgr), .tag_o(sample_tag), .last_o(sample_eof), .error_o(sample_error)
    );

    // Same common advance as the unchanged four-stage sampler. EOL is meaningful
    // only on m_valid; all metadata remain stable while downstream is stalled.
    always_ff @(posedge clk_i) begin
        if (rst_i) eol_q <= '0;
        else if (s_ready_o) eol_q <= {eol_q[2:0],eol_i};
    end
    // A final elastic register makes Q7 rounding part of the timed internal
    // datapath. It adds no PE and permits one accepted output every clock.
    assign sample_ready = !m_valid_o || m_ready_i;
    always_ff @(posedge clk_i) begin
        if (rst_i) m_valid_o <= 1'b0;
        else if (sample_ready) begin
            m_valid_o <= sample_valid;
            if (sample_valid) begin
                // Least-significant byte is tensor R, matching NHWC memory.
                rgb_u8_o <= {sampled_bgr[7:0],sampled_bgr[15:8],sampled_bgr[23:16]};
                rgb_q7_o <= {to_q7(sampled_bgr[7:0]),to_q7(sampled_bgr[15:8]),
                             to_q7(sampled_bgr[23:16])};
                tag_o <= sample_tag;
                eol_o <= eol_q[3];
                eof_o <= sample_eof;
                error_o <= sample_error;
            end
        end
    end
endmodule
