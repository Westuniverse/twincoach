// SPDX-License-Identifier: GPL-2.0-only
// Resource-sharing alternative to vertex_skinning_axis. Same arithmetic/vertex ABI.
module vertex_skinning_serial_axis (
    input logic clk_i,
    input logic rst_i,
    input logic matrix_wr_valid_i,
    output logic matrix_wr_ready_o,
    input logic [3:0] matrix_wr_index_i,
    input logic [191:0] matrix_wr_data_i,
    input logic in_tvalid_i,
    output logic in_tready_o,
    input logic [127:0] in_tdata_i,
    input logic in_tlast_i,
    output logic out_tvalid_o,
    input logic out_tready_i,
    output logic [63:0] out_tdata_o,
    output logic out_tlast_o
);
    localparam int unsigned ACC_W = 37;
    typedef enum logic [2:0] {IDLE, ISSUE, WAIT_RESULT, ACCUMULATE, OUTPUT_DATA} state_t;
    state_t state_q;
    (* ram_style = "distributed" *) logic [191:0] matrix_bank [0:15];
    logic [127:0] vertex_q;
    logic last_q;
    logic [1:0] influence_q;
    logic [3:0] matrix_index;
    logic [191:0] matrix_value;
    logic signed [16:0] weight;
    logic affine_ready, affine_valid;
    logic signed [33:0] affine_x, affine_y, affine_z;
    logic signed [17:0] quantized_x, quantized_y, quantized_z;
    (* use_dsp = "yes" *) logic signed [34:0] product_x, product_y, product_z;
    logic signed [34:0] product_x_q, product_y_q, product_z_q;
    logic signed [ACC_W-1:0] accumulated_x_q, accumulated_y_q, accumulated_z_q;
    logic signed [ACC_W-1:0] next_x, next_y, next_z;
    logic [47:0] result_q;

    assign matrix_wr_ready_o = !rst_i && state_q == IDLE;
    assign in_tready_o = !rst_i && state_q == IDLE && !matrix_wr_valid_i;
    assign out_tvalid_o = !rst_i && state_q == OUTPUT_DATA;
    assign out_tdata_o = {16'b0, result_q};
    assign out_tlast_o = last_q;
    assign matrix_index = vertex_q[48 + influence_q*4 +: 4];
    assign matrix_value = matrix_bank[matrix_index];
    assign weight = $signed({1'b0, vertex_q[64 + influence_q*16 +: 16]});
    assign product_x = quantized_x * weight;
    assign product_y = quantized_y * weight;
    assign product_z = quantized_z * weight;
    // ISSUE contributes zero to the shared accumulator/rounding datapath.
    // ACCUMULATE keeps the original registered product, so no second round
    // network or readable copy of the DSP accumulator is needed for last-zero.
    assign next_x = accumulated_x_q + (state_q == ISSUE ? 37'sd0 : {{2{product_x_q[34]}}, product_x_q});
    assign next_y = accumulated_y_q + (state_q == ISSUE ? 37'sd0 : {{2{product_y_q[34]}}, product_y_q});
    assign next_z = accumulated_z_q + (state_q == ISSUE ? 37'sd0 : {{2{product_z_q[34]}}, product_z_q});

    function automatic logic signed [15:0] round_and_saturate(input logic signed [ACC_W-1:0] value);
        logic [ACC_W:0] magnitude, rounded_magnitude;
        logic signed [ACC_W:0] rounded_value;
        begin
            if (value < 0) magnitude = $unsigned(-$signed({value[ACC_W-1], value}));
            else magnitude = $unsigned({1'b0, value});
            rounded_magnitude = (magnitude + ({{ACC_W{1'b0}},1'b1} << 14)) >> 15;
            if (value < 0) rounded_value = -$signed(rounded_magnitude);
            else rounded_value = $signed(rounded_magnitude);
            if (rounded_value > 32767) round_and_saturate = 16'sd32767;
            else if (rounded_value < -32768) round_and_saturate = -16'sd32768;
            else round_and_saturate = rounded_value[15:0];
        end
    endfunction

    always_ff @(posedge clk_i) begin
        if (matrix_wr_valid_i && matrix_wr_ready_o) matrix_bank[matrix_wr_index_i] <= matrix_wr_data_i;
        if (rst_i) begin
            state_q <= IDLE;
        end else begin
            case (state_q)
                IDLE: if (in_tvalid_i && in_tready_o) begin
                    vertex_q <= in_tdata_i;
                    last_q <= in_tlast_i;
                    influence_q <= 0;
                    accumulated_x_q <= 0;
                    accumulated_y_q <= 0;
                    accumulated_z_q <= 0;
                    state_q <= ISSUE;
                end
                ISSUE: begin
                    if (weight == 0) begin
                        // No affine request/result exists for a zero influence.
                        // Last-zero must finish from the existing accumulator;
                        // product_*_q still belongs to a previous nonzero lane.
                        if (influence_q == 3) begin
                            result_q <= {round_and_saturate(next_z), round_and_saturate(next_y), round_and_saturate(next_x)};
                            state_q <= OUTPUT_DATA;
                        end else influence_q <= influence_q + 1'b1;
                    end else if (affine_ready) state_q <= WAIT_RESULT;
                end
                WAIT_RESULT: if (affine_valid) begin
                    product_x_q <= product_x;
                    product_y_q <= product_y;
                    product_z_q <= product_z;
                    state_q <= ACCUMULATE;
                end
                ACCUMULATE: begin
                    accumulated_x_q <= next_x;
                    accumulated_y_q <= next_y;
                    accumulated_z_q <= next_z;
                    if (influence_q == 3) begin
                        result_q <= {round_and_saturate(next_z), round_and_saturate(next_y), round_and_saturate(next_x)};
                        state_q <= OUTPUT_DATA;
                    end else begin
                        influence_q <= influence_q + 1'b1;
                        state_q <= ISSUE;
                    end
                end
                OUTPUT_DATA: if (out_tready_i) state_q <= IDLE;
                default: state_q <= IDLE;
            endcase
        end
    end

    affine_transform_3d_core affine (
        .clk_i, .rst_i, .in_valid_i(!rst_i && state_q == ISSUE && weight != 0), .in_ready_o(affine_ready),
        .point_x_i($signed(vertex_q[15:0])), .point_y_i($signed(vertex_q[31:16])), .point_z_i($signed(vertex_q[47:32])),
        .matrix_00_i($signed(matrix_value[0*16 +: 16])), .matrix_01_i($signed(matrix_value[1*16 +: 16])),
        .matrix_02_i($signed(matrix_value[2*16 +: 16])), .matrix_03_i($signed(matrix_value[3*16 +: 16])),
        .matrix_10_i($signed(matrix_value[4*16 +: 16])), .matrix_11_i($signed(matrix_value[5*16 +: 16])),
        .matrix_12_i($signed(matrix_value[6*16 +: 16])), .matrix_13_i($signed(matrix_value[7*16 +: 16])),
        .matrix_20_i($signed(matrix_value[8*16 +: 16])), .matrix_21_i($signed(matrix_value[9*16 +: 16])),
        .matrix_22_i($signed(matrix_value[10*16 +: 16])), .matrix_23_i($signed(matrix_value[11*16 +: 16])),
        .out_valid_o(affine_valid), .out_ready_i(!rst_i && state_q == WAIT_RESULT),
        .point_x_o(affine_x), .point_y_o(affine_y), .point_z_o(affine_z)
    );
    affine_quantize_3d quantize (
        .point_x_i(affine_x), .point_y_i(affine_y), .point_z_i(affine_z),
        .point_x_o(quantized_x), .point_y_o(quantized_y), .point_z_o(quantized_z)
    );
endmodule
