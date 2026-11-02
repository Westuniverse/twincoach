module joint_metrics_core #(
    parameter int unsigned COORD_W = 16
) (
    input  logic                              clk_i,
    input  logic                              rst_i,

    input  logic                              in_valid_i,
    output logic                              in_ready_o,
    input  logic signed [COORD_W-1:0]         point_a_x_i,
    input  logic signed [COORD_W-1:0]         point_a_y_i,
    input  logic signed [COORD_W-1:0]         point_b_x_i,
    input  logic signed [COORD_W-1:0]         point_b_y_i,
    input  logic signed [COORD_W-1:0]         point_c_x_i,
    input  logic signed [COORD_W-1:0]         point_c_y_i,

    output logic                              out_valid_o,
    input  logic                              out_ready_i,
    output logic signed [2*(COORD_W+1):0]     dot_product_o,
    output logic        [2*(COORD_W+1):0]     magnitude_sq_ab_o,
    output logic        [2*(COORD_W+1):0]     magnitude_sq_cb_o
);

    localparam int unsigned VECTOR_W = COORD_W + 1;

    logic signed [VECTOR_W-1:0] vector_ab_x;
    logic signed [VECTOR_W-1:0] vector_ab_y;
    logic signed [VECTOR_W-1:0] vector_cb_x;
    logic signed [VECTOR_W-1:0] vector_cb_y;

    assign vector_ab_x =
        {point_a_x_i[COORD_W-1], point_a_x_i} -
        {point_b_x_i[COORD_W-1], point_b_x_i};
    assign vector_ab_y =
        {point_a_y_i[COORD_W-1], point_a_y_i} -
        {point_b_y_i[COORD_W-1], point_b_y_i};
    assign vector_cb_x =
        {point_c_x_i[COORD_W-1], point_c_x_i} -
        {point_b_x_i[COORD_W-1], point_b_x_i};
    assign vector_cb_y =
        {point_c_y_i[COORD_W-1], point_c_y_i} -
        {point_b_y_i[COORD_W-1], point_b_y_i};

    vector_metrics_core #(
        .COORD_W(VECTOR_W)
    ) vector_metrics (
        .clk_i,
        .rst_i,
        .in_valid_i,
        .in_ready_o,
        .vec_a_x_i(vector_ab_x),
        .vec_a_y_i(vector_ab_y),
        .vec_b_x_i(vector_cb_x),
        .vec_b_y_i(vector_cb_y),
        .out_valid_o,
        .out_ready_i,
        .dot_product_o,
        .magnitude_sq_a_o(magnitude_sq_ab_o),
        .magnitude_sq_b_o(magnitude_sq_cb_o)
    );

endmodule
