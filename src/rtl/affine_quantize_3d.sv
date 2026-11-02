module affine_quantize_3d (
    input  logic signed [33:0] point_x_i,
    input  logic signed [33:0] point_y_i,
    input  logic signed [33:0] point_z_i,
    output logic signed [17:0] point_x_o,
    output logic signed [17:0] point_y_o,
    output logic signed [17:0] point_z_o
);

    localparam int unsigned INPUT_W = 34;
    localparam int unsigned SHIFT_W = 14;
    localparam logic signed [35:0] OUTPUT_MAX = 36'sd131071;
    localparam logic signed [35:0] OUTPUT_MIN = -36'sd131072;

    function automatic logic signed [17:0] round_and_saturate(
        input logic signed [INPUT_W-1:0] value
    );
        logic [INPUT_W:0] magnitude;
        logic [INPUT_W:0] rounded_magnitude;
        logic signed [INPUT_W+1:0] rounded_value;
        begin
            if (value < 0)
                magnitude = $unsigned(-$signed({value[INPUT_W-1], value}));
            else
                magnitude = $unsigned({1'b0, value});

            rounded_magnitude =
                (magnitude + ({{INPUT_W{1'b0}}, 1'b1} << (SHIFT_W - 1))) >> SHIFT_W;
            if (value < 0)
                rounded_value = -$signed({1'b0, rounded_magnitude});
            else
                rounded_value = $signed({1'b0, rounded_magnitude});

            if (rounded_value > OUTPUT_MAX)
                round_and_saturate = 18'sd131071;
            else if (rounded_value < OUTPUT_MIN)
                round_and_saturate = -18'sd131072;
            else
                round_and_saturate = rounded_value[17:0];
        end
    endfunction

    always_comb begin
        point_x_o = round_and_saturate(point_x_i);
        point_y_o = round_and_saturate(point_y_i);
        point_z_o = round_and_saturate(point_z_i);
    end

endmodule
