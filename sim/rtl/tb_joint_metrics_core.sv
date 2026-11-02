module tb_joint_metrics_core;

    localparam int unsigned COORD_W = 8;
    localparam int unsigned METRIC_W = 2 * (COORD_W + 1) + 1;

    logic                          clk_i = 1'b0;
    logic                          rst_i = 1'b1;
    logic                          in_valid_i = 1'b0;
    logic                          in_ready_o;
    logic signed [COORD_W-1:0]     point_a_x_i = '0;
    logic signed [COORD_W-1:0]     point_a_y_i = '0;
    logic signed [COORD_W-1:0]     point_b_x_i = '0;
    logic signed [COORD_W-1:0]     point_b_y_i = '0;
    logic signed [COORD_W-1:0]     point_c_x_i = '0;
    logic signed [COORD_W-1:0]     point_c_y_i = '0;
    logic                          out_valid_o;
    logic                          out_ready_i = 1'b1;
    logic signed [METRIC_W-1:0]    dot_product_o;
    logic        [METRIC_W-1:0]    magnitude_sq_ab_o;
    logic        [METRIC_W-1:0]    magnitude_sq_cb_o;

    always #5 clk_i <= ~clk_i;

    joint_metrics_core #(
        .COORD_W(COORD_W)
    ) dut (
        .clk_i,
        .rst_i,
        .in_valid_i,
        .in_ready_o,
        .point_a_x_i,
        .point_a_y_i,
        .point_b_x_i,
        .point_b_y_i,
        .point_c_x_i,
        .point_c_y_i,
        .out_valid_o,
        .out_ready_i,
        .dot_product_o,
        .magnitude_sq_ab_o,
        .magnitude_sq_cb_o
    );

    task automatic drive_joint(
        input logic signed [COORD_W-1:0] ax,
        input logic signed [COORD_W-1:0] ay,
        input logic signed [COORD_W-1:0] bx,
        input logic signed [COORD_W-1:0] by,
        input logic signed [COORD_W-1:0] cx,
        input logic signed [COORD_W-1:0] cy
    );
        point_a_x_i = ax;
        point_a_y_i = ay;
        point_b_x_i = bx;
        point_b_y_i = by;
        point_c_x_i = cx;
        point_c_y_i = cy;
    endtask

    task automatic send_joint(
        input logic signed [COORD_W-1:0] ax,
        input logic signed [COORD_W-1:0] ay,
        input logic signed [COORD_W-1:0] bx,
        input logic signed [COORD_W-1:0] by,
        input logic signed [COORD_W-1:0] cx,
        input logic signed [COORD_W-1:0] cy
    );
        @(negedge clk_i);
        while (!in_ready_o) begin
            @(negedge clk_i);
        end
        drive_joint(ax, ay, bx, by, cx, cy);
        in_valid_i = 1'b1;
        @(negedge clk_i);
        in_valid_i = 1'b0;
    endtask

    task automatic expect_result(
        input logic signed [METRIC_W-1:0] expected_dot,
        input logic        [METRIC_W-1:0] expected_mag_ab,
        input logic        [METRIC_W-1:0] expected_mag_cb
    );
        while (!out_valid_o) begin
            @(negedge clk_i);
        end
        if ($signed(dot_product_o) != expected_dot) begin
            $fatal(1, "dot mismatch: got %0d expected %0d", $signed(dot_product_o), expected_dot);
        end
        if (magnitude_sq_ab_o != expected_mag_ab) begin
            $fatal(1, "AB magnitude mismatch: got %0d expected %0d", magnitude_sq_ab_o, expected_mag_ab);
        end
        if (magnitude_sq_cb_o != expected_mag_cb) begin
            $fatal(1, "CB magnitude mismatch: got %0d expected %0d", magnitude_sq_cb_o, expected_mag_cb);
        end
        @(negedge clk_i);
    endtask

    initial begin
        repeat (3) @(negedge clk_i);
        rst_i = 1'b0;

        send_joint(5, 7, 2, 3, 6, 0);
        expect_result(0, 25, 25);

        send_joint(-2, 5, 1, 1, 4, -1);
        expect_result(-17, 25, 13);

        send_joint(127, 0, -128, 0, -128, 0);
        expect_result(0, 65025, 0);

        out_ready_i = 1'b0;
        @(negedge clk_i);
        if (!in_ready_o) begin
            $fatal(1, "pipeline did not accept first continuous input");
        end
        drive_joint(5, 7, 2, 3, 6, 0);
        in_valid_i = 1'b1;
        @(negedge clk_i);
        if (!in_ready_o) begin
            $fatal(1, "pipeline did not accept second continuous input");
        end
        drive_joint(-2, 5, 1, 1, 4, -1);
        @(negedge clk_i);
        in_valid_i = 1'b0;

        repeat (3) begin
            if (!out_valid_o || $signed(dot_product_o) != 0 ||
                magnitude_sq_ab_o != 25 || magnitude_sq_cb_o != 25) begin
                $fatal(1, "output changed while backpressured");
            end
            @(negedge clk_i);
        end

        out_ready_i = 1'b1;
        expect_result(0, 25, 25);
        expect_result(-17, 25, 13);

        $display("TB PASS: joint_metrics_core");
        $finish;
    end

endmodule
