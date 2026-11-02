module tb_vector_metrics_core;

    localparam int unsigned COORD_W = 8;

    logic                      clk_i = 1'b0;
    logic                      rst_i = 1'b1;
    logic                      in_valid_i = 1'b0;
    logic                      in_ready_o;
    logic signed [COORD_W-1:0] vec_a_x_i = '0;
    logic signed [COORD_W-1:0] vec_a_y_i = '0;
    logic signed [COORD_W-1:0] vec_b_x_i = '0;
    logic signed [COORD_W-1:0] vec_b_y_i = '0;
    logic                      out_valid_o;
    logic                      out_ready_i = 1'b1;
    logic signed [2*COORD_W:0] dot_product_o;
    logic        [2*COORD_W:0] magnitude_sq_a_o;
    logic        [2*COORD_W:0] magnitude_sq_b_o;

    always #5 clk_i <= ~clk_i;

    vector_metrics_core #(
        .COORD_W(COORD_W)
    ) dut (
        .clk_i,
        .rst_i,
        .in_valid_i,
        .in_ready_o,
        .vec_a_x_i,
        .vec_a_y_i,
        .vec_b_x_i,
        .vec_b_y_i,
        .out_valid_o,
        .out_ready_i,
        .dot_product_o,
        .magnitude_sq_a_o,
        .magnitude_sq_b_o
    );

    task automatic send_vector(
        input logic signed [COORD_W-1:0] ax,
        input logic signed [COORD_W-1:0] ay,
        input logic signed [COORD_W-1:0] bx,
        input logic signed [COORD_W-1:0] by
    );
        @(negedge clk_i);
        while (!in_ready_o) begin
            @(negedge clk_i);
        end
        vec_a_x_i = ax;
        vec_a_y_i = ay;
        vec_b_x_i = bx;
        vec_b_y_i = by;
        in_valid_i = 1'b1;
        @(negedge clk_i);
        in_valid_i = 1'b0;
    endtask

    task automatic expect_result(
        input logic signed [2*COORD_W:0] expected_dot,
        input logic        [2*COORD_W:0] expected_mag_a,
        input logic        [2*COORD_W:0] expected_mag_b
    );
        while (!out_valid_o) begin
            @(negedge clk_i);
        end
        if ($signed(dot_product_o) != expected_dot) begin
            $fatal(1, "dot product mismatch: got %0d expected %0d", $signed(dot_product_o), expected_dot);
        end
        if (magnitude_sq_a_o != expected_mag_a) begin
            $fatal(1, "magnitude A mismatch: got %0d expected %0d", magnitude_sq_a_o, expected_mag_a);
        end
        if (magnitude_sq_b_o != expected_mag_b) begin
            $fatal(1, "magnitude B mismatch: got %0d expected %0d", magnitude_sq_b_o, expected_mag_b);
        end
        @(negedge clk_i);
    endtask

    initial begin
        repeat (3) @(negedge clk_i);
        rst_i = 1'b0;

        send_vector(3, 4, 4, -3);
        expect_result(0, 25, 25);

        out_ready_i = 1'b0;
        send_vector(2, -1, -3, 5);
        repeat (3) begin
            @(negedge clk_i);
            if (!out_valid_o || $signed(dot_product_o) != -11) begin
                $fatal(1, "output changed while backpressured");
            end
        end
        out_ready_i = 1'b1;
        expect_result(-11, 5, 34);

        $display("TB PASS: vector_metrics_core");
        $finish;
    end

endmodule
