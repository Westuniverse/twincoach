module vector_metrics_core #(
    parameter int unsigned COORD_W = 16
) (
    input  logic                         clk_i,
    input  logic                         rst_i,

    input  logic                         in_valid_i,
    output logic                         in_ready_o,
    input  logic signed [COORD_W-1:0]    vec_a_x_i,
    input  logic signed [COORD_W-1:0]    vec_a_y_i,
    input  logic signed [COORD_W-1:0]    vec_b_x_i,
    input  logic signed [COORD_W-1:0]    vec_b_y_i,

    output logic                         out_valid_o,
    input  logic                         out_ready_i,
    output logic signed [2*COORD_W:0]    dot_product_o,
    output logic        [2*COORD_W:0]    magnitude_sq_a_o,
    output logic        [2*COORD_W:0]    magnitude_sq_b_o
);

    localparam int unsigned PRODUCT_W = 2 * COORD_W;
    localparam int unsigned METRIC_W  = PRODUCT_W + 1;

    logic                              input_valid_q;
    logic signed [COORD_W-1:0]         vec_a_x_q;
    logic signed [COORD_W-1:0]         vec_a_y_q;
    logic signed [COORD_W-1:0]         vec_b_x_q;
    logic signed [COORD_W-1:0]         vec_b_y_q;
    logic                              output_ready;

    logic signed [PRODUCT_W-1:0] product_ax_bx;
    logic signed [PRODUCT_W-1:0] product_ay_by;
    logic signed [PRODUCT_W-1:0] product_ax_ax;
    logic signed [PRODUCT_W-1:0] product_ay_ay;
    logic signed [PRODUCT_W-1:0] product_bx_bx;
    logic signed [PRODUCT_W-1:0] product_by_by;

    logic signed [METRIC_W-1:0] dot_product_next;
    logic signed [METRIC_W-1:0] magnitude_sq_a_next;
    logic signed [METRIC_W-1:0] magnitude_sq_b_next;

    assign product_ax_bx = vec_a_x_q * vec_b_x_q;
    assign product_ay_by = vec_a_y_q * vec_b_y_q;
    assign product_ax_ax = vec_a_x_q * vec_a_x_q;
    assign product_ay_ay = vec_a_y_q * vec_a_y_q;
    assign product_bx_bx = vec_b_x_q * vec_b_x_q;
    assign product_by_by = vec_b_y_q * vec_b_y_q;

    assign dot_product_next =
        {product_ax_bx[PRODUCT_W-1], product_ax_bx} +
        {product_ay_by[PRODUCT_W-1], product_ay_by};
    assign magnitude_sq_a_next =
        {1'b0, product_ax_ax} + {1'b0, product_ay_ay};
    assign magnitude_sq_b_next =
        {1'b0, product_bx_bx} + {1'b0, product_by_by};

    assign output_ready = ~out_valid_o | out_ready_i;
    assign in_ready_o = ~input_valid_q | output_ready;

    always_ff @(posedge clk_i) begin
        if (rst_i) begin
            input_valid_q <= 1'b0;
            out_valid_o <= 1'b0;
        end else begin
            if (output_ready) begin
                out_valid_o <= input_valid_q;
                if (input_valid_q) begin
                    dot_product_o    <= dot_product_next;
                    magnitude_sq_a_o <= $unsigned(magnitude_sq_a_next);
                    magnitude_sq_b_o <= $unsigned(magnitude_sq_b_next);
                end
            end

            if (in_ready_o) begin
                input_valid_q <= in_valid_i;
                if (in_valid_i) begin
                    vec_a_x_q <= vec_a_x_i;
                    vec_a_y_q <= vec_a_y_i;
                    vec_b_x_q <= vec_b_x_i;
                    vec_b_y_q <= vec_b_y_i;
                end
            end
        end
    end

endmodule
