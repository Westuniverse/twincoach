// SPDX-License-Identifier: GPL-2.0-only
// Explicit AABB-extent derivative; frozen original tile module is unchanged.
// One bounded BGR8 tile. Loading and querying have exclusive ownership.
// Four local XY-parity banks supply4 neighbors, including repeated border taps.
// Ordered response status:0 HIT,1 MISS,2 INVALID(unloaded). pixel_valid only HIT.
module crop_tile128_q7_extent_axis (
    input  logic clk_i, rst_i,
    input  logic cfg_valid_i,
    output logic cfg_ready_o,
    input  logic [11:0] source_width_i, source_height_i,
    input  logic [11:0] origin_x_i, origin_y_i,
    input  logic [7:0] extent_width_i, extent_height_i,
    output logic cfg_error_o,
    input  logic load_valid_i,
    output logic load_ready_o,
    input  logic [23:0] load_bgr_i,
    input  logic load_eol_i, load_eof_i,
    output logic load_error_o, load_complete_o,
    input  logic query_valid_i,
    output logic query_ready_o,
    input  logic signed [31:0] query_x_q5_i, query_y_q5_i,
    input  logic [31:0] query_tag_i,
    input  logic query_eol_i, query_eof_i,
    output logic response_valid_o,
    input  logic response_ready_i,
    output logic pixel_valid_o,
    output logic [1:0] response_status_o,
    output logic [23:0] rgb_u8_o, rgb_q7_o,
    output logic [31:0] response_tag_o,
    output logic response_eol_o, response_eof_o,
    output logic resident_valid_o, busy_o
);
    localparam logic [1:0] HIT=2'd0, MISS=2'd1, INVALID=2'd2;
    typedef enum logic [1:0] {IDLE, LOAD, DRAIN} state_t;
    state_t state_q;
    logic [11:0] source_width_q, source_height_q, origin_x_q, origin_y_q;
    logic [7:0] width_q, height_q, column_q, row_q;
    logic resident_q;
    logic cfg_legal, load_fire, load_bad, write_fire, query_fire, response_fire;
    logic expected_eol, expected_eof;
    logic [12:0] remaining_width, remaining_height;
    logic [1:0] write_bank;
    logic [11:0] write_address;

    logic [15:0] clamped_x, clamped_y, maximum_x_q5, maximum_y_q5;
    logic [11:0] source_last_x,source_last_y;
    logic [11:0] x0,y0,x1,y1;
    logic [12:0] end_x,end_y;
    logic [6:0] local_x0,local_x1,local_y0,local_y1;
    logic [1:0] selectors[4], select_q[4];
    logic query_hit;
    logic [1:0] query_status;
    logic [23:0] bank_pixels[4];
    logic gather_valid_q, gather_hit_q, gather_advance, sample_ready;
    logic [4:0] fx_q,fy_q;
    logic [31:0] tag_q;
    logic eol_q,eof_q;
    logic [23:0] sampled_u8,sampled_q7;
    logic sample_error;

    // One status per accepted query; a maximum6 accepted records can occupy the
    // one BRAM-read stage plus the unchanged five-stage sample pipeline.
    logic [1:0] status_fifo[8];
    logic [2:0] status_write_q,status_read_q;
    logic [3:0] outstanding_q;

    assign cfg_legal=source_width_i>=12'd1 && source_width_i<=12'd2048 &&
        source_height_i>=12'd1 && source_height_i<=12'd2048 &&
        origin_x_i<source_width_i && origin_y_i<source_height_i &&
        extent_width_i>=1 && extent_width_i<=128 && extent_height_i>=1 && extent_height_i<=128 &&
        {1'b0,origin_x_i}+{5'd0,extent_width_i}<={1'b0,source_width_i} &&
        {1'b0,origin_y_i}+{5'd0,extent_height_i}<={1'b0,source_height_i};
    assign remaining_width={1'b0,source_width_i}-{1'b0,origin_x_i};
    assign remaining_height={1'b0,source_height_i}-{1'b0,origin_y_i};
    assign cfg_ready_o=!rst_i && state_q==IDLE && outstanding_q==0;
    assign load_ready_o=!rst_i && (state_q==LOAD || state_q==DRAIN);
    assign load_fire=load_valid_i && load_ready_o;
    assign expected_eol=column_q==width_q-8'd1;
    assign expected_eof=expected_eol && row_q==height_q-8'd1;
    assign load_bad=load_eol_i!=expected_eol || load_eof_i!=expected_eof;
    assign write_fire=load_fire && state_q==LOAD && !load_bad;
    assign write_bank={row_q[0],column_q[0]};
    assign write_address={row_q[6:1],column_q[6:1]};

    assign source_last_x=source_width_q-12'd1;
    assign source_last_y=source_height_q-12'd1;
    assign maximum_x_q5={source_last_x[10:0],5'b0};
    assign maximum_y_q5={source_last_y[10:0],5'b0};
    always_comb begin
        if(query_x_q5_i<0)clamped_x=16'd0;
        else if($unsigned(query_x_q5_i)>{16'd0,maximum_x_q5})clamped_x=maximum_x_q5;
        else clamped_x=query_x_q5_i[15:0];
        if(query_y_q5_i<0)clamped_y=16'd0;
        else if($unsigned(query_y_q5_i)>{16'd0,maximum_y_q5})clamped_y=maximum_y_q5;
        else clamped_y=query_y_q5_i[15:0];
    end
    assign x0={1'b0,clamped_x[15:5]};
    assign y0={1'b0,clamped_y[15:5]};
    assign x1=x0+12'd1<source_width_q ? x0+12'd1 : x0;
    assign y1=y0+12'd1<source_height_q ? y0+12'd1 : y0;
    assign end_x={1'b0,origin_x_q}+{5'd0,width_q};
    assign end_y={1'b0,origin_y_q}+{5'd0,height_q};
    assign query_hit=resident_q && x0>=origin_x_q && y0>=origin_y_q &&
        {1'b0,x1}<end_x && {1'b0,y1}<end_y;
    assign query_status=!resident_q ? INVALID : query_hit ? HIT : MISS;
    assign local_x0=7'(x0-origin_x_q);
    assign local_x1=7'(x1-origin_x_q);
    assign local_y0=7'(y0-origin_y_q);
    assign local_y1=7'(y1-origin_y_q);
    assign selectors[0]={local_y0[0],local_x0[0]};
    assign selectors[1]={local_y0[0],local_x1[0]};
    assign selectors[2]={local_y1[0],local_x0[0]};
    assign selectors[3]={local_y1[0],local_x1[0]};

    assign gather_advance=!gather_valid_q || sample_ready;
    assign query_ready_o=!rst_i && state_q==IDLE && !cfg_valid_i &&
        gather_advance && outstanding_q<4'd8;
    assign query_fire=query_valid_i && query_ready_o;
    assign response_fire=response_valid_o && response_ready_i;
    assign response_status_o=response_valid_o ? status_fifo[status_read_q] : INVALID;
    assign pixel_valid_o=response_valid_o && response_status_o==HIT;
    assign rgb_u8_o=pixel_valid_o ? sampled_u8 : 24'd0;
    assign rgb_q7_o=pixel_valid_o ? sampled_q7 : 24'd0;
    assign resident_valid_o=resident_q;
    assign busy_o=state_q!=IDLE || outstanding_q!=0;

    for(genvar b=0;b<4;b=b+1)begin:g_bank
        (* ram_style="block" *) logic [7:0] blue_ram[0:4095];
        (* ram_style="block" *) logic [7:0] green_ram[0:4095];
        (* ram_style="block" *) logic [7:0] red_ram[0:4095];
        logic [7:0] blue_q,green_q,red_q;
        logic [6:0] read_x,read_y;
        logic [11:0] read_address;
        logic selected;
        assign read_x=local_x0[0]==1'(b%2) ? local_x0 : local_x1;
        assign read_y=local_y0[0]==1'(b/2) ? local_y0 : local_y1;
        assign read_address={read_y[6:1],read_x[6:1]};
        assign selected=(selectors[0]==2'(b) || selectors[1]==2'(b) ||
                         selectors[2]==2'(b) || selectors[3]==2'(b));
        assign bank_pixels[b]={red_q,green_q,blue_q};
        always_ff @(posedge clk_i)begin
            if(write_fire && write_bank==2'(b))begin
                blue_ram[write_address]<=load_bgr_i[7:0];
                green_ram[write_address]<=load_bgr_i[15:8];
                red_ram[write_address]<=load_bgr_i[23:16];
            end
            if(query_fire && query_hit && selected)begin
                blue_q<=blue_ram[read_address];
                green_q<=green_ram[read_address];
                red_q<=red_ram[read_address];
            end
        end
    end

    crop_bilinear_q7_axis sampler(
        .clk_i,.rst_i,.s_valid_i(gather_valid_q),.s_ready_o(sample_ready),
        .p00_bgr_i(gather_hit_q ? bank_pixels[select_q[0]] : 24'd0),
        .p10_bgr_i(gather_hit_q ? bank_pixels[select_q[1]] : 24'd0),
        .p01_bgr_i(gather_hit_q ? bank_pixels[select_q[2]] : 24'd0),
        .p11_bgr_i(gather_hit_q ? bank_pixels[select_q[3]] : 24'd0),
        .fx_q5_i(fx_q),.fy_q5_i(fy_q),.tag_i(tag_q),.eol_i(eol_q),.eof_i(eof_q),
        .m_valid_o(response_valid_o),.m_ready_i(response_ready_i),
        .rgb_u8_o(sampled_u8),.rgb_q7_o(sampled_q7),
        .tag_o(response_tag_o),.eol_o(response_eol_o),.eof_o(response_eof_o),.error_o(sample_error)
    );

    always_ff @(posedge clk_i)begin
        if(rst_i)begin
            state_q<=IDLE;resident_q<=1'b0;
            source_width_q<=12'd1;source_height_q<=12'd1;
            origin_x_q<='0;origin_y_q<='0;width_q<='0;height_q<='0;
            column_q<='0;row_q<='0;cfg_error_o<=1'b0;
            load_error_o<=1'b0;load_complete_o<=1'b0;
            gather_valid_q<=1'b0;status_write_q<='0;status_read_q<='0;outstanding_q<='0;
        end else begin
            cfg_error_o<=1'b0;load_complete_o<=1'b0;
            if(gather_advance)begin
                gather_valid_q<=query_fire;
                if(query_fire)begin
                    gather_hit_q<=query_hit;fx_q<=clamped_x[4:0];fy_q<=clamped_y[4:0];
                    tag_q<=query_tag_i;eol_q<=query_eol_i;eof_q<=query_eof_i;
                    for(integer t=0;t<4;t=t+1)select_q[t]<=selectors[t];
                end
            end
            if(query_fire)begin
                status_fifo[status_write_q]<=query_status;
                status_write_q<=status_write_q+3'd1;
            end
            if(response_fire)status_read_q<=status_read_q+3'd1;
            case({query_fire,response_fire})
                2'b10:outstanding_q<=outstanding_q+4'd1;
                2'b01:outstanding_q<=outstanding_q-4'd1;
                default:begin end
            endcase
            if(cfg_valid_i && cfg_ready_o)begin
                resident_q<=1'b0;load_error_o<=1'b0;
                if(cfg_legal)begin
                    source_width_q<=source_width_i;source_height_q<=source_height_i;
                    origin_x_q<=origin_x_i;origin_y_q<=origin_y_i;
                    width_q<=extent_width_i;
                    height_q<=extent_height_i;
                    column_q<='0;row_q<='0;state_q<=LOAD;
                end else cfg_error_o<=1'b1;
            end
            if(load_fire)begin
                if(state_q==DRAIN)begin
                    if(load_eof_i)state_q<=IDLE;
                end else if(load_bad)begin
                    resident_q<=1'b0;load_error_o<=1'b1;
                    state_q<=load_eof_i ? IDLE : DRAIN;
                end else if(expected_eof)begin
                    resident_q<=1'b1;load_complete_o<=1'b1;state_q<=IDLE;
                end else if(expected_eol)begin
                    column_q<='0;row_q<=row_q+8'd1;
                end else column_q<=column_q+8'd1;
            end
        end
    end
endmodule
