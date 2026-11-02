module temporal_motion_core #(
    parameter int unsigned COORD_W = 16
) (
    input  logic                         clk_i,
    input  logic                         rst_i,

    input  logic                         in_valid_i,
    output logic                         in_ready_o,
    input  logic signed [COORD_W-1:0]    point_t0_x_i,
    input  logic signed [COORD_W-1:0]    point_t0_y_i,
    input  logic signed [COORD_W-1:0]    point_t1_x_i,
    input  logic signed [COORD_W-1:0]    point_t1_y_i,
    input  logic signed [COORD_W-1:0]    point_t2_x_i,
    input  logic signed [COORD_W-1:0]    point_t2_y_i,

    output logic                         out_valid_o,
    input  logic                         out_ready_i,
    output logic        [2*(COORD_W+1):0] velocity_t1_sq_o,
    output logic        [2*(COORD_W+1):0] velocity_t2_sq_o,
    output logic        [2*(COORD_W+1):0] acceleration_sq_o
);

    localparam int unsigned VELOCITY_W = COORD_W + 1;
    localparam int unsigned METRIC_W = 2 * VELOCITY_W + 1;

    logic signed [VELOCITY_W-1:0] velocity_t1_x;
    logic signed [VELOCITY_W-1:0] velocity_t1_y;
    logic signed [VELOCITY_W-1:0] velocity_t2_x;
    logic signed [VELOCITY_W-1:0] velocity_t2_y;
    logic signed [METRIC_W-1:0] velocity_dot;

    assign velocity_t1_x =
        {point_t1_x_i[COORD_W-1], point_t1_x_i} -
        {point_t0_x_i[COORD_W-1], point_t0_x_i};
    assign velocity_t1_y =
        {point_t1_y_i[COORD_W-1], point_t1_y_i} -
        {point_t0_y_i[COORD_W-1], point_t0_y_i};
    assign velocity_t2_x =
        {point_t2_x_i[COORD_W-1], point_t2_x_i} -
        {point_t1_x_i[COORD_W-1], point_t1_x_i};
    assign velocity_t2_y =
        {point_t2_y_i[COORD_W-1], point_t2_y_i} -
        {point_t1_y_i[COORD_W-1], point_t1_y_i};

    // |v2-v1|^2 = |v1|^2 + |v2|^2 - 2(v1 dot v2).  Reusing the
    // vector metric products avoids two additional multipliers.
    always_comb begin
        acceleration_sq_o =
            velocity_t1_sq_o + velocity_t2_sq_o -
            $unsigned(velocity_dot <<< 1);
    end

    vector_metrics_core #(
        .COORD_W(VELOCITY_W)
    ) velocity_metrics (
        .clk_i,
        .rst_i,
        .in_valid_i,
        .in_ready_o,
        .vec_a_x_i(velocity_t1_x),
        .vec_a_y_i(velocity_t1_y),
        .vec_b_x_i(velocity_t2_x),
        .vec_b_y_i(velocity_t2_y),
        .out_valid_o,
        .out_ready_i,
        .dot_product_o(velocity_dot),
        .magnitude_sq_a_o(velocity_t1_sq_o),
        .magnitude_sq_b_o(velocity_t2_sq_o)
    );

endmodule
