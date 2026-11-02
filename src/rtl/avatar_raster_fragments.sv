// Project-generated incremental raster command v1; decisions/0070.
(* use_dsp="yes" *) module avatar_raster_fragments (
    input logic clk_i, rst_i,
    input logic cmd_valid_i, output logic cmd_ready_o,
    input logic [47:0] bbox_i,
    input logic [95:0] edge_seed_i, edge_dx_i, edge_dy_i,
    input logic [79:0] uv_seed_i,
    input logic [127:0] uv_steps_i,
    input logic [8:0] shade_i, input logic empty_i,
    output logic out_valid_o, input logic out_ready_i,
    output logic [15:0] u_q8_o,
    output logic [16:0] v_q8_o,
    output logic [8:0] shade_o,
    output logic [31:0] tag_o,
    output logic last_o
);
    typedef enum logic [1:0] {IDLE,SCAN,MARKER} state_t;
    state_t state_q;
    logic [11:0] x_q,y_q,x0_q,x1_q,y1_q;
    logic signed [31:0] edge_q[3], row_edge_q[3], dx_q[3],dy_q[3];
    logic signed [39:0] u_q,v_q,row_u_q,row_v_q;
    logic signed [31:0] uv_steps_q[4];
    logic [8:0] shade_q;
    logic error_q, invalid_cmd, covered;
    wire [11:0] x0=bbox_i[11:0],x1=bbox_i[23:12],y0=bbox_i[35:24],y1=bbox_i[47:36];
    always_comb begin
        invalid_cmd=shade_i>9'd256;
        if (x1>=x0 && ({1'b0,x1}-{1'b0,x0})>13'd31) invalid_cmd=1;
        if (y1>=y0 && ({1'b0,y1}-{1'b0,y0})>13'd31) invalid_cmd=1;
        for (int i=0;i<3;i++) begin
            if ($signed(edge_seed_i[i*32+:32]) < -32'sd1073741824 ||
                $signed(edge_seed_i[i*32+:32]) >= 32'sd1073741824 ||
                $signed(edge_dx_i[i*32+:32]) < -32'sd1048576 ||
                $signed(edge_dx_i[i*32+:32]) >= 32'sd1048576 ||
                $signed(edge_dy_i[i*32+:32]) < -32'sd1048576 ||
                $signed(edge_dy_i[i*32+:32]) >= 32'sd1048576) invalid_cmd=1;
        end
        for (int i=0;i<2;i++)
            if ($signed(uv_seed_i[i*40+:40]) < -40'sd68719476736 ||
                $signed(uv_seed_i[i*40+:40]) >= 40'sd68719476736) invalid_cmd=1;
        for (int i=0;i<4;i++)
            if ($signed(uv_steps_i[i*32+:32]) < -32'sd268435456 ||
                $signed(uv_steps_i[i*32+:32]) >= 32'sd268435456) invalid_cmd=1;
    end
    function automatic logic signed [39:0] extend_step(input logic signed [31:0] value);
        return {{8{value[31]}},value};
    endfunction
    function automatic logic [15:0] clamp_u(input logic signed [39:0] value);
        if (value<=0) return 0;
        if (value>=40'sd16711680) return 16'hff00;
        return value[23:8];
    endfunction
    function automatic logic [16:0] clamp_v(input logic signed [39:0] value);
        if (value<=0) return 0;
        if (value>=40'sd33488896) return 17'h1ff00;
        return value[24:8];
    endfunction
    assign covered=!(edge_q[0][31] || edge_q[1][31] || edge_q[2][31]);
    assign cmd_ready_o=state_q==IDLE && !rst_i;
    assign out_valid_o=!rst_i && (state_q==MARKER || (state_q==SCAN && covered));
    assign last_o=state_q==MARKER;
    assign u_q8_o=last_o ? 16'd0 : clamp_u(u_q);
    assign v_q8_o=last_o ? 17'd0 : clamp_v(v_q);
    assign shade_o=last_o ? (error_q ? 9'd511 : 9'd0) : shade_q;
    assign tag_o=last_o ? 32'd0 : {4'b0,y_q,4'b0,x_q};
    always_ff @(posedge clk_i) begin
        if (rst_i) state_q<=IDLE;
        else case(state_q)
            IDLE: if (cmd_valid_i) begin
                error_q<=invalid_cmd;
                if (invalid_cmd || empty_i || x1<x0 || y1<y0) state_q<=MARKER;
                else begin
                    state_q<=SCAN;x_q<=x0;y_q<=y0;x0_q<=x0;x1_q<=x1;y1_q<=y1;shade_q<=shade_i;
                    for (int i=0;i<3;i++) begin
                        edge_q[i]<=$signed(edge_seed_i[i*32+:32]);
                        row_edge_q[i]<=$signed(edge_seed_i[i*32+:32]);
                        dx_q[i]<=$signed(edge_dx_i[i*32+:32]);
                        dy_q[i]<=$signed(edge_dy_i[i*32+:32]);
                    end
                    u_q<=$signed(uv_seed_i[39:0]);row_u_q<=$signed(uv_seed_i[39:0]);
                    v_q<=$signed(uv_seed_i[79:40]);row_v_q<=$signed(uv_seed_i[79:40]);
                    for (int i=0;i<4;i++) uv_steps_q[i]<=$signed(uv_steps_i[i*32+:32]);
                end
            end
            SCAN: if (!covered || out_ready_i) begin
                if (x_q==x1_q) begin
                    if (y_q==y1_q) state_q<=MARKER;
                    else begin
                        x_q<=x0_q;y_q<=y_q+12'd1;
                        for (int i=0;i<3;i++) begin
                            row_edge_q[i]<=row_edge_q[i]+dy_q[i];
                            edge_q[i]<=row_edge_q[i]+dy_q[i];
                        end
                        row_u_q<=row_u_q+extend_step(uv_steps_q[2]);u_q<=row_u_q+extend_step(uv_steps_q[2]);
                        row_v_q<=row_v_q+extend_step(uv_steps_q[3]);v_q<=row_v_q+extend_step(uv_steps_q[3]);
                    end
                end else begin
                    x_q<=x_q+12'd1;
                    for (int i=0;i<3;i++) edge_q[i]<=edge_q[i]+dx_q[i];
                    u_q<=u_q+extend_step(uv_steps_q[0]);v_q<=v_q+extend_step(uv_steps_q[1]);
                end
            end
            MARKER: if(out_ready_i) state_q<=IDLE;
            default:state_q<=IDLE;
        endcase
    end
endmodule
