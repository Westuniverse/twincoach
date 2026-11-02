// Four XY-parity banks, each with separate RGB8 planes. No RAM data reset.
module avatar_texture_cache (
    input logic clk_i,rst_i,idle_i,
    input logic upload_begin_valid_i,output logic upload_begin_ready_o,
    input logic tex_valid_i,output logic tex_ready_o,
    input logic [23:0] tex_rgb_i,input logic tex_last_i,
    output logic texture_valid_o,upload_active_o,upload_error_o,
    input logic in_valid_i,output logic in_ready_o,
    input logic [15:0] u_q8_i,input logic [16:0] v_q8_i,
    input logic [8:0] shade_i,input logic [31:0] tag_i,input logic last_i,
    output logic out_valid_o,input logic out_ready_i,
    output logic [23:0] p00_o,p10_o,p01_o,p11_o,
    output logic [7:0] u_fraction_o,v_fraction_o,
    output logic [8:0] shade_o,output logic [31:0] tag_o,output logic last_o
);
    logic [16:0] upload_count_q;
    logic sample_valid_q;
    logic [1:0] select_q[4];
    logic [23:0] samples[4];
    logic advance,read_enable,write_enable;
    wire [7:0] x0=u_q8_i[15:8];
    wire [8:0] y0=v_q8_i[16:8];
    wire [7:0] x1=x0==8'hff ? x0 : x0+8'd1;
    wire [8:0] y1=y0==9'h1ff ? y0 : y0+9'd1;
    wire [1:0] write_bank={upload_count_q[8],upload_count_q[0]};
    wire [14:0] write_address={upload_count_q[16:9],upload_count_q[7:1]};
    assign upload_begin_ready_o=idle_i && !upload_active_o && !sample_valid_q && !rst_i;
    assign tex_ready_o=upload_active_o && !rst_i;
    assign write_enable=tex_valid_i && tex_ready_o;
    assign advance=!sample_valid_q || out_ready_i;
    assign in_ready_o=texture_valid_o && !upload_active_o && advance && !rst_i;
    assign read_enable=in_valid_i && in_ready_o;
    assign out_valid_o=sample_valid_q && !rst_i;
    assign p00_o=samples[select_q[0]];
    assign p10_o=samples[select_q[1]];
    assign p01_o=samples[select_q[2]];
    assign p11_o=samples[select_q[3]];
    for (genvar bank=0;bank<4;bank++) begin:g_bank
        (* ram_style="block" *) logic [7:0] red_ram[0:32767];
        (* ram_style="block" *) logic [7:0] green_ram[0:32767];
        (* ram_style="block" *) logic [7:0] blue_ram[0:32767];
        logic [7:0] red_q,green_q,blue_q;
        wire [7:0] read_x=x0[0]==1'(bank%2) ? x0 : x1;
        wire [8:0] read_y=y0[0]==1'(bank/2) ? y0 : y1;
        wire [14:0] read_address={read_y[8:1],read_x[7:1]};
        assign samples[bank]={blue_q,green_q,red_q};
        always_ff @(posedge clk_i) begin
            if(write_enable && write_bank==2'(bank)) begin
                red_ram[write_address]<=tex_rgb_i[7:0];
                green_ram[write_address]<=tex_rgb_i[15:8];
                blue_ram[write_address]<=tex_rgb_i[23:16];
            end
            if(read_enable) begin
                red_q<=red_ram[read_address];green_q<=green_ram[read_address];blue_q<=blue_ram[read_address];
            end
        end
    end
    always_ff @(posedge clk_i) begin
        if(rst_i) begin
            texture_valid_o<=0;upload_active_o<=0;upload_error_o<=0;sample_valid_q<=0;
        end else begin
            upload_error_o<=0;
            if(upload_begin_valid_i && upload_begin_ready_o) begin
                texture_valid_o<=0;upload_active_o<=1;upload_count_q<=0;
            end
            if(write_enable) begin
                if(tex_last_i!=(upload_count_q==17'h1ffff)) begin
                    upload_error_o<=1;upload_active_o<=0;texture_valid_o<=0;
                end else if(tex_last_i) begin
                    texture_valid_o<=1;upload_active_o<=0;
                end else upload_count_q<=upload_count_q+17'd1;
            end
            if(advance) begin
                sample_valid_q<=read_enable;
                if(read_enable) begin
                    select_q[0]<={y0[0],x0[0]};select_q[1]<={y0[0],x1[0]};
                    select_q[2]<={y1[0],x0[0]};select_q[3]<={y1[0],x1[0]};
                    u_fraction_o<=u_q8_i[7:0];v_fraction_o<=v_q8_i[7:0];
                    shade_o<=shade_i;tag_o<=tag_i;last_o<=last_i;
                end
            end
        end
    end
endmodule
