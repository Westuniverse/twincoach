// Fixed-point rectified stereo triangulation with one shared restoring divider.
//
// Input beat (128 bit, little-endian field numbering):
//   [15:0]   front x, [31:16] front y,
//   [47:32]  side x,  [63:48] side y,
//   [79:64]  baseline, [95:80] focal length,
//   [103:96] joint id, [111:104] front confidence,
//   [119:112] side confidence, [127:120] reserved.
//
// All six coordinate/calibration values are signed Q1.14 raw integers.  For
// rectified normalized coordinates the equations are X=B*x/d, Y=B*y/d and
// Z=F*B/d.  The core shares a 33-cycle restoring divider across those three
// quotients.  This bounds the divider resource and accepts a new point only
// after the current result is consumed; the 17-point stereo batch is still
// comfortably below a 30 FPS frame budget at a 100 MHz PL clock.
//
// Output beat:
//   [31:0]   X, [63:32] Y, [95:64] Z (signed Q1.14 in 32 bits),
//   [103:96] joint id, [111:104] min confidence, [112] valid,
//   [127:113] saturated absolute disparity report.  TLAST is preserved.

module rectified_triangulation_axis #(
    parameter int unsigned MIN_DISPARITY = 64,
    parameter int unsigned MIN_CONFIDENCE = 64
) (
    input  logic          clk_i,
    input  logic          rst_i,

    input  logic          s_axis_tvalid_i,
    output logic          s_axis_tready_o,
    input  logic [127:0]  s_axis_tdata_i,
    input  logic          s_axis_tlast_i,

    output logic          m_axis_tvalid_o,
    input  logic          m_axis_tready_i,
    output logic [127:0]  m_axis_tdata_o,
    output logic          m_axis_tlast_o
);

    localparam int unsigned COORD_W = 16;
    localparam int unsigned PRODUCT_W = 33;
    localparam int unsigned DIVISOR_W = 17;
    localparam int unsigned REMAINDER_W = DIVISOR_W + 1;
    localparam int unsigned DIV_COUNT_W = $clog2(PRODUCT_W + 1);

    typedef enum logic [2:0] {
        IDLE = 3'd0,
        DIVIDE_X = 3'd1,
        DIVIDE_Y = 3'd2,
        DIVIDE_Z = 3'd3,
        OUTPUT = 3'd4
    } state_t;

    state_t state_q;

    logic signed [COORD_W-1:0] front_x_i;
    logic signed [COORD_W-1:0] front_y_i;
    logic signed [COORD_W-1:0] side_x_i;
    logic signed [COORD_W-1:0] side_y_i;
    logic signed [COORD_W-1:0] baseline_i;
    logic signed [COORD_W-1:0] focal_i;
    logic [7:0] front_confidence_i;
    logic [7:0] side_confidence_i;
    logic signed [16:0] front_x_ext;
    logic signed [16:0] side_x_ext;
    logic signed [16:0] disparity_i;
    logic [16:0] disparity_abs_i;
    logic [7:0] confidence_i;
    logic geometry_valid_i;

    logic signed [PRODUCT_W-1:0] product_x_i;
    logic signed [PRODUCT_W-1:0] product_y_i;
    logic signed [PRODUCT_W-1:0] product_z_i;
    logic [PRODUCT_W-1:0] product_x_abs_i;
    logic [PRODUCT_W-1:0] product_y_abs_i;
    logic [PRODUCT_W-1:0] product_z_abs_i;

    logic signed [PRODUCT_W-1:0] product_y_q;
    logic signed [PRODUCT_W-1:0] product_z_q;
    logic [PRODUCT_W-1:0] dividend_q;
    logic [PRODUCT_W-1:0] quotient_q;
    logic [REMAINDER_W-1:0] remainder_q;
    logic [DIVISOR_W-1:0] divisor_q;
    logic [DIV_COUNT_W-1:0] divide_count_q;
    logic negative_x_q;
    logic negative_y_q;
    logic negative_z_q;

    logic signed [31:0] result_x_q;
    logic signed [31:0] result_y_q;
    logic signed [31:0] result_z_q;
    logic [7:0] result_joint_id_q;
    logic [7:0] result_confidence_q;
    logic result_valid_q;
    logic [16:0] result_disparity_q;
    logic result_last_q;

    logic [REMAINDER_W-1:0] shifted_remainder;
    logic [REMAINDER_W-1:0] divisor_extended;
    logic divide_take;
    logic [REMAINDER_W-1:0] next_remainder;
    logic [PRODUCT_W-1:0] next_quotient;
    logic [PRODUCT_W-1:0] next_dividend;

    assign front_x_i = $signed(s_axis_tdata_i[15:0]);
    assign front_y_i = $signed(s_axis_tdata_i[31:16]);
    assign side_x_i = $signed(s_axis_tdata_i[47:32]);
    assign side_y_i = $signed(s_axis_tdata_i[63:48]);
    assign baseline_i = $signed(s_axis_tdata_i[79:64]);
    assign focal_i = $signed(s_axis_tdata_i[95:80]);
    assign front_confidence_i = s_axis_tdata_i[111:104];
    assign side_confidence_i = s_axis_tdata_i[119:112];

    assign front_x_ext = {front_x_i[15], front_x_i};
    assign side_x_ext = {side_x_i[15], side_x_i};
    assign disparity_i = front_x_ext - side_x_ext;
    assign disparity_abs_i = disparity_i[16]
        ? $unsigned(-disparity_i)
        : $unsigned(disparity_i);
    assign confidence_i = (front_confidence_i < side_confidence_i)
        ? front_confidence_i
        : side_confidence_i;
    assign geometry_valid_i =
        (confidence_i >= MIN_CONFIDENCE[7:0]) &&
        (disparity_abs_i >= MIN_DISPARITY[16:0]);

    // Extending operands before multiplication keeps the intended signed
    // product width explicit; Vivado can map the three products to DSPs.
    assign product_x_i = $signed({{17{baseline_i[15]}}, baseline_i}) *
                         $signed({{17{front_x_i[15]}}, front_x_i});
    assign product_y_i = $signed({{17{baseline_i[15]}}, baseline_i}) *
                         $signed({{17{front_y_i[15]}}, front_y_i});
    assign product_z_i = $signed({{17{focal_i[15]}}, focal_i}) *
                         $signed({{17{baseline_i[15]}}, baseline_i});
    assign product_x_abs_i = product_x_i[PRODUCT_W-1]
        ? $unsigned(-product_x_i)
        : $unsigned(product_x_i);
    assign product_y_abs_i = product_y_i[PRODUCT_W-1]
        ? $unsigned(-product_y_i)
        : $unsigned(product_y_i);
    assign product_z_abs_i = product_z_i[PRODUCT_W-1]
        ? $unsigned(-product_z_i)
        : $unsigned(product_z_i);

    // The output register is a one-entry elastic buffer.  When the consumer
    // accepts the current result, accept the next request on the same edge;
    // this removes one bubble between points without adding arithmetic state.
    assign s_axis_tready_o = (state_q == IDLE) ||
                             ((state_q == OUTPUT) && m_axis_tready_i);
    assign m_axis_tvalid_o = (state_q == OUTPUT);
    assign m_axis_tlast_o = result_last_q;

    always_comb begin
        m_axis_tdata_o = '0;
        m_axis_tdata_o[31:0] = result_x_q;
        m_axis_tdata_o[63:32] = result_y_q;
        m_axis_tdata_o[95:64] = result_z_q;
        m_axis_tdata_o[103:96] = result_joint_id_q;
        m_axis_tdata_o[111:104] = result_confidence_q;
        m_axis_tdata_o[112] = result_valid_q;
        m_axis_tdata_o[127:113] =
            (result_disparity_q > 17'd32767)
                ? 15'h7fff
                : result_disparity_q[14:0];
    end

    // One restoring division iteration per clock.  The final quotient bit is
    // explicitly formed as a combinational next value so no off-by-one cycle
    // can drop the most significant result bit.
    assign shifted_remainder = {remainder_q[16:0], dividend_q[PRODUCT_W-1]};
    assign divisor_extended = {{(REMAINDER_W-DIVISOR_W){1'b0}}, divisor_q};
    assign divide_take = shifted_remainder >= divisor_extended;
    assign next_remainder = divide_take
        ? shifted_remainder - divisor_extended
        : shifted_remainder;
    assign next_quotient = {quotient_q[PRODUCT_W-2:0], divide_take};
    assign next_dividend = {dividend_q[PRODUCT_W-2:0], 1'b0};

    function automatic logic [PRODUCT_W-1:0] absolute_product(
        input logic signed [PRODUCT_W-1:0] value
    );
        begin
            absolute_product = value[PRODUCT_W-1]
                ? $unsigned(-value)
                : $unsigned(value);
        end
    endfunction

    function automatic logic signed [31:0] signed_quotient(
        input logic [PRODUCT_W-1:0] magnitude,
        input logic                  negative
    );
        logic [31:0] limited;
        begin
            // Keep the output deterministic if a very small disparity causes
            // a quotient larger than the documented Q1.14 result range.
            if (magnitude[PRODUCT_W-1] || magnitude[31]) begin
                signed_quotient = negative ? 32'sh80000000 : 32'sh7fffffff;
            end else begin
                limited = magnitude[31:0];
                signed_quotient = negative
                    ? -$signed(limited)
                    : $signed(limited);
            end
        end
    endfunction

    always_ff @(posedge clk_i) begin
        if (rst_i) begin
            state_q <= IDLE;
            quotient_q <= '0;
            remainder_q <= '0;
            dividend_q <= '0;
            divisor_q <= '0;
            divide_count_q <= '0;
            result_x_q <= '0;
            result_y_q <= '0;
            result_z_q <= '0;
            result_joint_id_q <= '0;
            result_confidence_q <= '0;
            result_valid_q <= 1'b0;
            result_disparity_q <= '0;
            result_last_q <= 1'b0;
        end else begin
            case (state_q)
                IDLE: begin
                    // Input capture is handled below so OUTPUT can hand off
                    // one result and one new request in the same cycle.
                end
                DIVIDE_X: begin
                    dividend_q <= next_dividend;
                    quotient_q <= next_quotient;
                    remainder_q <= next_remainder;
                    if (divide_count_q == PRODUCT_W - 1) begin
                        result_x_q <= signed_quotient(next_quotient, negative_x_q);
                        dividend_q <= absolute_product(product_y_q);
                        quotient_q <= '0;
                        remainder_q <= '0;
                        divide_count_q <= '0;
                        state_q <= DIVIDE_Y;
                    end else begin
                        divide_count_q <= divide_count_q + 1'b1;
                    end
                end
                DIVIDE_Y: begin
                    dividend_q <= next_dividend;
                    quotient_q <= next_quotient;
                    remainder_q <= next_remainder;
                    if (divide_count_q == PRODUCT_W - 1) begin
                        result_y_q <= signed_quotient(next_quotient, negative_y_q);
                        dividend_q <= absolute_product(product_z_q);
                        quotient_q <= '0;
                        remainder_q <= '0;
                        divide_count_q <= '0;
                        state_q <= DIVIDE_Z;
                    end else begin
                        divide_count_q <= divide_count_q + 1'b1;
                    end
                end
                DIVIDE_Z: begin
                    dividend_q <= next_dividend;
                    quotient_q <= next_quotient;
                    remainder_q <= next_remainder;
                    if (divide_count_q == PRODUCT_W - 1) begin
                        result_z_q <= signed_quotient(next_quotient, negative_z_q);
                        state_q <= OUTPUT;
                    end else begin
                        divide_count_q <= divide_count_q + 1'b1;
                    end
                end
                OUTPUT: begin
                    if (m_axis_tready_i) begin
                        state_q <= IDLE;
                    end
                end
                default: state_q <= IDLE;
            endcase

            // A request is accepted either from IDLE or on an output
            // consume/produce handoff.  Keep this after the state case so it
            // intentionally overrides OUTPUT -> IDLE on a simultaneous beat.
            if (s_axis_tvalid_i && s_axis_tready_o) begin
                result_joint_id_q <= s_axis_tdata_i[103:96];
                result_confidence_q <= confidence_i;
                result_valid_q <= geometry_valid_i;
                result_disparity_q <= disparity_abs_i;
                result_last_q <= s_axis_tlast_i;
                if (!geometry_valid_i) begin
                    result_x_q <= '0;
                    result_y_q <= '0;
                    result_z_q <= '0;
                    state_q <= OUTPUT;
                end else begin
                    product_y_q <= product_y_i;
                    product_z_q <= product_z_i;
                    negative_x_q <= product_x_i[PRODUCT_W-1] ^ disparity_i[16];
                    negative_y_q <= product_y_i[PRODUCT_W-1] ^ disparity_i[16];
                    negative_z_q <= product_z_i[PRODUCT_W-1] ^ disparity_i[16];
                    divisor_q <= disparity_abs_i;
                    dividend_q <= product_x_abs_i;
                    quotient_q <= '0;
                    remainder_q <= '0;
                    divide_count_q <= '0;
                    state_q <= DIVIDE_X;
                end
            end
        end
    end

endmodule
