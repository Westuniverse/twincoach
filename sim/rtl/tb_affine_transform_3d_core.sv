module tb_affine_transform_3d_core;

    localparam logic signed [15:0] Q_ONE = 16'sd16384;

    logic               clk_i = 1'b0;
    logic               rst_i = 1'b1;
    logic               in_valid_i = 1'b0;
    logic               in_ready_o;
    logic signed [15:0] point_x_i = '0;
    logic signed [15:0] point_y_i = '0;
    logic signed [15:0] point_z_i = '0;
    logic signed [15:0] matrix_00_i = '0;
    logic signed [15:0] matrix_01_i = '0;
    logic signed [15:0] matrix_02_i = '0;
    logic signed [15:0] matrix_03_i = '0;
    logic signed [15:0] matrix_10_i = '0;
    logic signed [15:0] matrix_11_i = '0;
    logic signed [15:0] matrix_12_i = '0;
    logic signed [15:0] matrix_13_i = '0;
    logic signed [15:0] matrix_20_i = '0;
    logic signed [15:0] matrix_21_i = '0;
    logic signed [15:0] matrix_22_i = '0;
    logic signed [15:0] matrix_23_i = '0;
    logic               out_valid_o;
    logic               out_ready_i = 1'b1;
    logic signed [33:0] point_x_o;
    logic signed [33:0] point_y_o;
    logic signed [33:0] point_z_o;

    always #5 clk_i <= ~clk_i;

    affine_transform_3d_core dut (.*);

    task automatic set_identity;
        matrix_00_i = Q_ONE;
        matrix_01_i = '0;
        matrix_02_i = '0;
        matrix_03_i = '0;
        matrix_10_i = '0;
        matrix_11_i = Q_ONE;
        matrix_12_i = '0;
        matrix_13_i = '0;
        matrix_20_i = '0;
        matrix_21_i = '0;
        matrix_22_i = Q_ONE;
        matrix_23_i = '0;
    endtask

    task automatic send_point(
        input logic signed [15:0] x,
        input logic signed [15:0] y,
        input logic signed [15:0] z
    );
        @(negedge clk_i);
        while (!in_ready_o) begin
            @(negedge clk_i);
        end
        point_x_i = x;
        point_y_i = y;
        point_z_i = z;
        in_valid_i = 1'b1;
        @(negedge clk_i);
        in_valid_i = 1'b0;
    endtask

    task automatic expect_point(
        input logic signed [33:0] x,
        input logic signed [33:0] y,
        input logic signed [33:0] z
    );
        while (!out_valid_o) begin
            @(negedge clk_i);
        end
        if (point_x_o !== x || point_y_o !== y || point_z_o !== z) begin
            $fatal(
                1,
                "point mismatch: got (%0d,%0d,%0d), expected (%0d,%0d,%0d)",
                point_x_o,
                point_y_o,
                point_z_o,
                x,
                y,
                z
            );
        end
        @(negedge clk_i);
    endtask

    initial begin
        repeat (3) @(negedge clk_i);
        rst_i = 1'b0;

        // Identity: Q1.14 multiplied by Q1.14 produces Q2.28.
        set_identity();
        send_point(16'sd8192, -16'sd4096, 16'sd2048);
        expect_point(34'sd134217728, -34'sd67108864, 34'sd33554432);

        // Add (+0.25, -0.5, +1.0) in normalized model coordinates.
        set_identity();
        matrix_03_i = 16'sd4096;
        matrix_13_i = -16'sd8192;
        matrix_23_i = Q_ONE;
        send_point(16'sd8192, -16'sd4096, 16'sd2048);
        expect_point(34'sd201326592, -34'sd201326592, 34'sd301989888);

        // Rotate +90 degrees around Z: (x,y,z) -> (-y,x,z).
        set_identity();
        matrix_00_i = '0;
        matrix_01_i = -Q_ONE;
        matrix_10_i = Q_ONE;
        matrix_11_i = '0;
        send_point(16'sd4096, 16'sd8192, -16'sd2048);
        expect_point(-34'sd134217728, 34'sd67108864, -34'sd33554432);

        // A completed result must remain stable throughout downstream backpressure.
        set_identity();
        out_ready_i = 1'b0;
        send_point(-16'sd16384, 16'sd12288, 16'sd4096);
        repeat (3) begin
            @(negedge clk_i);
            if (!out_valid_o || point_x_o !== -34'sd268435456 ||
                point_y_o !== 34'sd201326592 || point_z_o !== 34'sd67108864)
                $fatal(1, "output changed while backpressured");
        end
        out_ready_i = 1'b1;
        expect_point(-34'sd268435456, 34'sd201326592, 34'sd67108864);

        $display("TB PASS: affine_transform_3d_core");
        $finish;
    end

endmodule
