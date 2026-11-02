// SPDX-License-Identifier: GPL-2.0-only
// Fixed CI288 A16 cache: DDR CHWc4 -> two64bit HWC8 banks. No RAM reset.
// PW/HEAD are elastic synchronous reads at II1. DW gathers9*4 terms then
// emits five shared-A8 words; PE diagonal weights select their own channel.
module tail4_feature_feeder(
 input logic clk_i,rst_i,
 input logic cfg_valid_i,output logic cfg_ready_o,input logic cfg_dim2_i,
 input logic load_valid_i,output logic load_ready_o,input logic[127:0]load_data_i,
 output logic loaded_o,
 input logic run_valid_i,output logic run_ready_o,
 input logic[1:0]run_mode_i,input logic run_stride2_i,
 input logic[6:0]run_channel_quartet_i,input logic[15:0]run_tag_base_i,
 output logic run_error_o,
 output logic m_valid_o,input logic m_ready_i,output logic[127:0]features_o,
 output logic[15:0]tag_o,output logic last_o,done_o
);
 typedef enum logic[2:0]{IDLE,LOAD,READY,STREAM,DW_GATHER,DW_EMIT,DW_DRAIN}state_t;
 state_t state_q;
 logic dim2_q,stride2_q,load_split_q;
 logic[1:0]mode_q;
 logic[6:0]load_cg_q,cg_q;
 logic[3:0]load_pixel_q,pixel_q;
 logic[15:0]base_tag_q;
 logic[63:0]load_high_q,read_q[0:1];
 logic[9:0]load_addr,write_addr,read_addr_q,dw_read_addr;
 logic[3:0]dw_pixel_index;
 logic write_enable,read_enable,read_bank_select,read_both;
 logic[9:0]ram_read_addr;
 logic[63:0]write_data;
 logic read_pending_q,issued_done_q,pending_last_q;
 logic[15:0]pending_tag_q;
 logic[5:0]group_q;
 logic[3:0]tap_issue_q,pending_tap_q;
 logic tap_pending_q,pending_pad_q;
 logic[575:0]window_q;
 logic[2:0]emit_q;
 logic[1:0]tap_y,tap_x,out_y,out_x;
 logic signed[4:0]source_y,source_x;
 logic dw_inside;
 wire cfg_fire=cfg_valid_i&&cfg_ready_o;
 wire load_fire=load_valid_i&&load_ready_o;
 wire run_fire=run_valid_i&&run_ready_o;
 wire advance=!m_valid_o||m_ready_i;
 wire stream_issue=!rst_i&&state_q==STREAM&&advance&&!issued_done_q;
 wire gather_issue=!rst_i&&state_q==DW_GATHER&&tap_issue_q<9;
 wire[3:0]last_input_pixel=dim2_q?4'd3:4'd15;
 wire[3:0]last_output_pixel=stride2_q?(dim2_q?4'd0:4'd3):last_input_pixel;
 wire[9:0]last_stream_address=dim2_q?10'd143:10'd575;
 wire run_legal=(run_mode_i<3)&&!(run_mode_i==2&&!dim2_q)&&!(run_mode_i==1&&run_channel_quartet_i>71);
 assign cfg_ready_o=!rst_i&&(state_q==IDLE||state_q==READY);
 assign run_ready_o=!rst_i&&state_q==READY&&!cfg_valid_i;
 assign load_ready_o=!rst_i&&state_q==LOAD&&!load_split_q;
 assign loaded_o=!rst_i&&(state_q==READY||state_q==STREAM||state_q==DW_GATHER||state_q==DW_EMIT||state_q==DW_DRAIN);
 // Load half0 and half1 are different pixels, not channel halves. Each cg4
 // selects one bank, so the other quartet at this HWC8 address is preserved.
 // Exact finite CI288 address bases:36 HWC8 records per input pixel.
 // A four-bit index covers every valid2x2/4x4 pixel, so no multiplier is needed.
 function automatic logic[9:0]pixel_base36(input logic[3:0]pixel);
  case(pixel)
   0:return 10'd0;1:return 10'd36;2:return 10'd72;3:return 10'd108;
   4:return 10'd144;5:return 10'd180;6:return 10'd216;7:return 10'd252;
   8:return 10'd288;9:return 10'd324;10:return 10'd360;11:return 10'd396;
   12:return 10'd432;13:return 10'd468;14:return 10'd504;default:return 10'd540;
  endcase
 endfunction
 assign load_addr=pixel_base36(load_pixel_q)+{4'd0,load_cg_q[6:1]};
 assign write_addr=load_addr+(load_split_q?10'd36:10'd0);
 assign write_data=load_split_q?load_high_q:load_data_i[63:0];
 assign write_enable=!rst_i&&state_q==LOAD&&(load_fire||load_split_q);
 always_comb begin
  case(tap_issue_q)
   0:begin tap_y=0;tap_x=0;end 1:begin tap_y=0;tap_x=1;end 2:begin tap_y=0;tap_x=2;end
   3:begin tap_y=1;tap_x=0;end 4:begin tap_y=1;tap_x=1;end 5:begin tap_y=1;tap_x=2;end
   6:begin tap_y=2;tap_x=0;end 7:begin tap_y=2;tap_x=1;end default:begin tap_y=2;tap_x=2;end
  endcase
  if(dim2_q&&stride2_q)begin out_y=0;out_x=0;end
  else if(dim2_q||stride2_q)begin out_y={1'b0,pixel_q[1]};out_x={1'b0,pixel_q[0]};end
  else begin out_y=pixel_q[3:2];out_x=pixel_q[1:0];end
  // Even input sizes: SAME s2 has pad_before0, pad_after1; s1 pad_before1.
  source_y=(stride2_q?$signed({2'd0,out_y,1'b0}):$signed({3'd0,out_y}))+$signed({3'd0,tap_y})-(stride2_q?5'sd0:5'sd1);
  source_x=(stride2_q?$signed({2'd0,out_x,1'b0}):$signed({3'd0,out_x}))+$signed({3'd0,tap_x})-(stride2_q?5'sd0:5'sd1);
  dw_inside=source_y>=0&&source_x>=0&&source_y<(dim2_q?5'sd2:5'sd4)&&source_x<(dim2_q?5'sd2:5'sd4);
  // When dw_inside is true these bit packs equal y*dim+x exactly. Padding
  // never enables a RAM read; its wrapped address is deliberately irrelevant.
  dw_pixel_index=dim2_q?{2'd0,source_y[0],source_x[0]}:{source_y[1:0],source_x[1:0]};
  dw_read_addr=pixel_base36(dw_pixel_index)+{4'd0,cg_q[6:1]};
 end
 assign read_enable=stream_issue||(gather_issue&&dw_inside);
 assign read_both=state_q==STREAM;
 assign read_bank_select=cg_q[0];
 assign ram_read_addr=read_both?read_addr_q:dw_read_addr;
 for(genvar bank=0;bank<2;bank=bank+1)begin: banks
  (* ram_style="block" *)logic[63:0]ram[0:575];
  always_ff @(posedge clk_i)begin
   if(write_enable&&load_cg_q[0]==1'(bank))ram[write_addr]<=write_data;
   if(read_enable&&(read_both||read_bank_select==1'(bank)))read_q[bank]<=ram[ram_read_addr];
  end
 end
 always_ff @(posedge clk_i)begin
  run_error_o<=0;done_o<=0;
  if(rst_i)begin
   state_q<=IDLE;load_split_q<=0;m_valid_o<=0;read_pending_q<=0;issued_done_q<=0;tap_pending_q<=0;
  end else begin
   if(cfg_fire)begin state_q<=LOAD;dim2_q<=cfg_dim2_i;load_cg_q<=0;load_pixel_q<=0;load_split_q<=0;end
   if(load_fire)begin load_high_q<=load_data_i[127:64];load_split_q<=1;end
   if(state_q==LOAD&&load_split_q)begin
    load_split_q<=0;
    if(load_pixel_q==last_input_pixel-1'b1)begin
     load_pixel_q<=0;
     if(load_cg_q==71)state_q<=READY;else load_cg_q<=load_cg_q+1'b1;
    end else load_pixel_q<=load_pixel_q+4'd2;
   end
   if(run_fire)begin
    if(!run_legal)run_error_o<=1;
    else begin
     mode_q<=run_mode_i;stride2_q<=run_stride2_i;cg_q<=run_channel_quartet_i;base_tag_q<=run_tag_base_i;
     pixel_q<=0;group_q<=0;read_addr_q<=0;read_pending_q<=0;issued_done_q<=0;
     tap_issue_q<=0;tap_pending_q<=0;emit_q<=0;
     state_q<=run_mode_i==1?DW_GATHER:STREAM;
    end
   end
   if(state_q==STREAM&&advance)begin
    m_valid_o<=read_pending_q;read_pending_q<=stream_issue;
    if(read_pending_q)begin features_o<={read_q[1],read_q[0]};tag_o<=pending_tag_q;last_o<=pending_last_q;end
   end
   if(stream_issue)begin
    pending_tag_q<=mode_q==2?base_tag_q:base_tag_q+{12'd0,pixel_q};pending_last_q<=read_addr_q==last_stream_address;
    if(read_addr_q==last_stream_address)issued_done_q<=1;
    read_addr_q<=read_addr_q+1'b1;
    if(group_q==35)begin group_q<=0;pixel_q<=pixel_q+1'b1;end else group_q<=group_q+1'b1;
   end
   if(state_q==DW_GATHER)begin
    tap_pending_q<=gather_issue;
    if(gather_issue)begin pending_tap_q<=tap_issue_q;pending_pad_q<=!dw_inside;tap_issue_q<=tap_issue_q+1'b1;end
    if(tap_pending_q)begin
     window_q[pending_tap_q*64+:64]<=pending_pad_q?64'd0:read_q[cg_q[0]];
     if(pending_tap_q==8)begin state_q<=DW_EMIT;emit_q<=0;end
    end
   end
   if(state_q==DW_EMIT&&advance)begin
    m_valid_o<=1;features_o<=emit_q==4?{64'd0,window_q[512+:64]}:window_q[emit_q*128+:128];
    tag_o<=base_tag_q+{12'd0,pixel_q};last_o<=pixel_q==last_output_pixel&&emit_q==4;
    if(emit_q==4)state_q<=DW_DRAIN;else emit_q<=emit_q+1'b1;
   end
   if(state_q==DW_DRAIN&&m_valid_o&&m_ready_i)begin
    m_valid_o<=0;
    if(pixel_q!=last_output_pixel)begin pixel_q<=pixel_q+1'b1;tap_issue_q<=0;tap_pending_q<=0;state_q<=DW_GATHER;end
   end
   if(m_valid_o&&m_ready_i&&last_o)begin state_q<=READY;done_o<=1;end
  end
 end
endmodule
