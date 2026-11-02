// Integer triangle lighting golden v0.1. One shared signed 53x53 multiplier,
// digit-by-digit integer sqrt, and bounded quotient restoring division.
module avatar_triangle_lighting_core (
    input  logic         clk_i,
    input  logic         rst_i,
    input  logic         in_valid_i,
    output logic         in_ready_o,
    input  logic [215:0]  points_i,
    input  logic [23:0]   color_rgb_i,
    output logic         out_valid_o,
    input  logic         out_ready_i,
    output logic [15:0]   light_q15_o,
    output logic [23:0]   color_bgr_o,
    output logic         degenerate_o,
    output logic [51:0]  normal_length_o
);
    typedef enum logic [4:0] {
        IDLE, CROSS_SETUP, SQUARE_SETUP, CHECK_SQUARED, ROOT_STEP,
        DOT_SETUP, DOT_ABS, DIV_STEP, DIV_ROUND, WEIGHT_SETUP,
        COLOR_LIGHT_SETUP, COLOR_SHADE_SETUP, MUL_EXEC, MUL_USE, OUTPUT_RESULT
    } state_t;
    typedef enum logic [2:0] {CROSS, SQUARE, DOT, WEIGHT, COLOR_LIGHT, COLOR_SHADE} mul_kind_t;
    state_t state_q;
    mul_kind_t mul_kind_q;
    logic signed [24:0] edge_a_q [0:2], edge_b_q [0:2];
    logic signed [50:0] normal_q [0:2];
    logic signed [49:0] cross_first_q;
    logic [2:0] step_q;
    logic [1:0] channel_q;
    logic [23:0] rgb_q, color_light_q;
    logic [26:0] shade_q;
    logic signed [52:0] mul_a_q, mul_b_q;
    logic signed [105:0] product_q;
    logic [103:0] squared_q, radicand_q;
    logic [55:0] root_rem_q, root_rem_shift, root_trial;
    logic [51:0] root_q, root_next;
    logic [5:0] root_count_q;
    logic root_take;
    logic signed [68:0] dot_q;
    logic [68:0] div_rem_q, div_shift_q, div_base_q;
    logic [15:0] div_quotient_q, diffuse_q;
    logic [4:0] div_count_q;
    logic div_color_q;
    logic [16:0] div_rounded;
    logic [7:0] channel_value;
    logic [17:0] mixed_light;
    logic signed [25:0] input_depth_sum;

    function automatic logic signed [24:0] coord(input logic [23:0] value);
        coord = $signed({value[23], value});
    endfunction
    function automatic logic signed [52:0] edge_ext(input logic signed [24:0] value);
        edge_ext = {{28{value[24]}}, value};
    endfunction
    function automatic logic signed [52:0] normal_ext(input logic signed [50:0] value);
        normal_ext = {{2{value[50]}}, value};
    endfunction

    assign input_depth_sum =
        $signed({{2{points_i[71]}}, points_i[71:48]}) +
        $signed({{2{points_i[143]}}, points_i[143:120]}) +
        $signed({{2{points_i[215]}}, points_i[215:192]});
    assign in_ready_o = (state_q == IDLE) && !rst_i;
    assign out_valid_o = (state_q == OUTPUT_RESULT) && !rst_i;
    // Reuse the existing sqrt; stale root data on the degenerate bypass is masked.
    assign normal_length_o = degenerate_o ? 52'd0 : root_q;
    assign root_rem_shift = {root_rem_q[53:0], radicand_q[103:102]};
    assign root_trial = {2'b00, root_q, 2'b01};
    assign root_take = root_rem_shift >= root_trial;
    assign root_next = {root_q[50:0], root_take};
    // RTE uses the final remainder, not an approximate reciprocal.
    assign div_rounded = {1'b0, div_quotient_q} +
        17'(({div_rem_q, 1'b0} > {1'b0, div_base_q}) ||
        (({div_rem_q, 1'b0} == {1'b0, div_base_q}) && div_quotient_q[0]));
    assign mixed_light = 18'd9175 + {1'b0, product_q[31:15]} +
        18'((product_q[14:0] > 15'd16384) ||
        ((product_q[14:0] == 15'd16384) && product_q[15]));
    always_comb begin
        case (channel_q)
            2'd0: channel_value = rgb_q[23:16];
            2'd1: channel_value = rgb_q[15:8];
            default: channel_value = rgb_q[7:0];
        endcase
    end

    always_ff @(posedge clk_i) begin
        if (rst_i) begin
            state_q <= IDLE;
        end else begin
            case (state_q)
                IDLE: if (in_valid_i) begin
                    for (int axis = 0; axis < 3; axis++) begin
                        edge_a_q[axis] <= coord(points_i[(axis+3)*24 +: 24]) -
                            coord(points_i[axis*24 +: 24]);
                        edge_b_q[axis] <= coord(points_i[(axis+6)*24 +: 24]) -
                            coord(points_i[axis*24 +: 24]);
                    end
                    if (input_depth_sum < -26'sd12582912)
                        shade_q <= 27'd83886080;
                    else if (input_depth_sum > 26'sd12582912)
                        shade_q <= 27'd58720256;
                    else
                        shade_q <= 27'(27'sd71303168 -
                            $signed({input_depth_sum[25], input_depth_sum}));
                    rgb_q <= color_rgb_i;
                    degenerate_o <= 1'b0;
                    step_q <= 0;
                    state_q <= CROSS_SETUP;
                end
                CROSS_SETUP: begin
                    case (step_q)
                        0: begin mul_a_q <= edge_ext(edge_a_q[1]); mul_b_q <= edge_ext(edge_b_q[2]); end
                        1: begin mul_a_q <= edge_ext(edge_a_q[2]); mul_b_q <= edge_ext(edge_b_q[1]); end
                        2: begin mul_a_q <= edge_ext(edge_a_q[2]); mul_b_q <= edge_ext(edge_b_q[0]); end
                        3: begin mul_a_q <= edge_ext(edge_a_q[0]); mul_b_q <= edge_ext(edge_b_q[2]); end
                        4: begin mul_a_q <= edge_ext(edge_a_q[0]); mul_b_q <= edge_ext(edge_b_q[1]); end
                        default: begin mul_a_q <= edge_ext(edge_a_q[1]); mul_b_q <= edge_ext(edge_b_q[0]); end
                    endcase
                    mul_kind_q <= CROSS;
                    state_q <= MUL_EXEC;
                end
                SQUARE_SETUP: begin
                    mul_a_q <= normal_ext(normal_q[step_q[1:0]]);
                    mul_b_q <= normal_ext(normal_q[step_q[1:0]]);
                    mul_kind_q <= SQUARE;
                    state_q <= MUL_EXEC;
                end
                CHECK_SQUARED: begin
                    if (squared_q <= 104'd309) begin
                        degenerate_o <= 1'b1;
                        light_q15_o <= 16'd11469;
                        channel_q <= 0;
                        state_q <= COLOR_LIGHT_SETUP;
                    end else begin
                        radicand_q <= squared_q;
                        root_rem_q <= 0;
                        root_q <= 0;
                        root_count_q <= 6'd51;
                        state_q <= ROOT_STEP;
                    end
                end
                ROOT_STEP: begin
                    root_rem_q <= root_take ? root_rem_shift - root_trial : root_rem_shift;
                    root_q <= root_next;
                    radicand_q <= {radicand_q[101:0], 2'b00};
                    root_count_q <= root_count_q - 1'b1;
                    if (root_count_q == 0) begin
                        step_q <= 0;
                        dot_q <= 0;
                        state_q <= DOT_SETUP;
                    end
                end
                DOT_SETUP: begin
                    mul_a_q <= normal_ext(normal_q[step_q[1:0]]);
                    case (step_q)
                        0: mul_b_q <= -53'sd11469;
                        1: mul_b_q <= 53'sd18022;
                        default: mul_b_q <= -53'sd24904;
                    endcase
                    mul_kind_q <= DOT;
                    state_q <= MUL_EXEC;
                end
                DOT_ABS: begin
                    div_rem_q <= dot_q[68] ? $unsigned(-dot_q) : $unsigned(dot_q);
                    div_base_q <= {17'd0, root_q};
                    div_shift_q <= {2'd0, root_q, 15'd0};
                    div_quotient_q <= 0;
                    div_count_q <= 15;
                    div_color_q <= 1'b0;
                    state_q <= DIV_STEP;
                end
                DIV_STEP: begin
                    if (div_rem_q >= div_shift_q) begin
                        div_rem_q <= div_rem_q - div_shift_q;
                        div_quotient_q[div_count_q[3:0]] <= 1'b1;
                    end
                    div_shift_q <= {1'b0, div_shift_q[68:1]};
                    div_count_q <= div_count_q - 1'b1;
                    if (div_count_q == 0) state_q <= DIV_ROUND;
                end
                DIV_ROUND: begin
                    if (!div_color_q) begin
                        diffuse_q <= div_rounded[15:0];
                        state_q <= WEIGHT_SETUP;
                    end else begin
                        color_bgr_o[channel_q*8 +: 8] <= div_rounded[7:0];
                        if (channel_q == 2) state_q <= OUTPUT_RESULT;
                        else begin
                            channel_q <= channel_q + 1'b1;
                            state_q <= COLOR_LIGHT_SETUP;
                        end
                    end
                end
                WEIGHT_SETUP: begin
                    mul_a_q <= $signed({37'd0, diffuse_q});
                    mul_b_q <= 53'sd23593;
                    mul_kind_q <= WEIGHT;
                    state_q <= MUL_EXEC;
                end
                COLOR_LIGHT_SETUP: begin
                    mul_a_q <= $signed({45'd0, channel_value});
                    mul_b_q <= $signed({37'd0, light_q15_o});
                    mul_kind_q <= COLOR_LIGHT;
                    state_q <= MUL_EXEC;
                end
                COLOR_SHADE_SETUP: begin
                    mul_a_q <= $signed({29'd0, color_light_q});
                    mul_b_q <= $signed({26'd0, shade_q});
                    mul_kind_q <= COLOR_SHADE;
                    state_q <= MUL_EXEC;
                end
                MUL_EXEC: begin
                    product_q <= mul_a_q * mul_b_q;
                    state_q <= MUL_USE;
                end
                MUL_USE: case (mul_kind_q)
                    CROSS: begin
                        if (!step_q[0]) cross_first_q <= product_q[49:0];
                        else normal_q[step_q[2:1]] <=
                            $signed({cross_first_q[49], cross_first_q}) -
                            $signed({product_q[49], product_q[49:0]});
                        if (step_q == 5) begin
                            step_q <= 0;
                            squared_q <= 0;
                            state_q <= SQUARE_SETUP;
                        end else begin
                            step_q <= step_q + 1'b1;
                            state_q <= CROSS_SETUP;
                        end
                    end
                    SQUARE: begin
                        squared_q <= squared_q + {2'd0, product_q[101:0]};
                        if (step_q == 2) state_q <= CHECK_SQUARED;
                        else begin step_q <= step_q + 1'b1; state_q <= SQUARE_SETUP; end
                    end
                    DOT: begin
                        dot_q <= dot_q + $signed({{2{product_q[66]}}, product_q[66:0]});
                        if (step_q == 2) state_q <= DOT_ABS;
                        else begin step_q <= step_q + 1'b1; state_q <= DOT_SETUP; end
                    end
                    WEIGHT: begin
                        light_q15_o <= mixed_light > 18'd32768 ? 16'd32768 : mixed_light[15:0];
                        channel_q <= 0;
                        state_q <= COLOR_LIGHT_SETUP;
                    end
                    COLOR_LIGHT: begin
                        color_light_q <= product_q[23:0];
                        state_q <= COLOR_SHADE_SETUP;
                    end
                    default: begin // COLOR_SHADE
                        div_rem_q <= product_q[68:0];
                        div_base_q <= 69'd2748779069440; // 20 * 2^22 * 2^15
                        div_shift_q <= 69'd351843720888320; // denominator << 7
                        div_quotient_q <= 0;
                        div_count_q <= 7;
                        div_color_q <= 1'b1;
                        state_q <= DIV_STEP;
                    end
                endcase
                OUTPUT_RESULT: if (out_ready_i) state_q <= IDLE;
                default: state_q <= IDLE;
            endcase
        end
    end
endmodule
