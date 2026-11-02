// SPDX-License-Identifier: GPL-2.0-only
// Private exact DDR BGR+Q5 map sampler -> owned row-major RGBQ7 AXIS.
// No public MMIO/UAPI change. Ticket/source/map owner lasts to final output/drain.
module roi_dma_sampler_axis128(
 input logic clk_i,rst_i,cmd_valid_i,output logic cmd_ready_o,
 input logic[63:0]source_addr_i,map_addr_i,ticket_i,
 input logic[11:0]source_width_i,source_height_i,input logic[31:0]source_stride_i,
 input logic cancel_i,
 output logic busy_o,done_o,success_o,error_o,cancelled_o,
 output logic read_quiesced_o, // cancel held: no present/future DDR read ownership
 output logic[63:0]ticket_o,output logic[3:0]error_code_o,
 output logic[63:0]m_axi_araddr_o,output logic[7:0]m_axi_arlen_o,
 output logic[2:0]m_axi_arsize_o,m_axi_arprot_o,output logic[1:0]m_axi_arburst_o,
 output logic[3:0]m_axi_arcache_o,m_axi_arqos_o,output logic m_axi_arid_o,m_axi_arlock_o,m_axi_arvalid_o,input logic m_axi_arready_i,
 input logic[127:0]m_axi_rdata_i,input logic[1:0]m_axi_rresp_i,input logic m_axi_rid_i,m_axi_rlast_i,m_axi_rvalid_i,output logic m_axi_rready_o,
 output logic[127:0]m_axis_tdata_o,output logic[15:0]m_axis_tkeep_o,
 output logic[63:0]m_axis_tuser_o,output logic m_axis_tvalid_o,m_axis_tlast_o,input logic m_axis_tready_i
);
 typedef enum logic[4:0]{IDLE,DESC_MUL,DESC_CHECK,MAP_REQ,MAP_WAIT,ROOT_START,
  SCAN_INIT,SCAN,BOX_EVAL,CACHE_CFG,SRC_MUL,SRC_PLAN,SRC_REQ,SRC_RUN,CACHE_READY,
  ROW_PLAN,ROW_RUN,RECT_DONE,OUTPUT_INIT,OUTPUT_RUN,ABORT,FLUSH,FINISH}state_t;
 state_t state_q;
 localparam logic[15:0]BUS_TAG=16'hc701;
 logic[63:0]source_addr_q,map_addr_q;
 logic[11:0]source_width_q,source_height_q;
 logic[31:0]stride_q;
 logic[63:0]mul_acc_q,mul_value_q;
 logic[11:0]mul_bits_q;logic[3:0]mul_step_q;
 logic[64:0]source_end,map_end;
 logic[63:0]source_alloc_bytes;
 logic header_bad,ranges_bad;

 logic rd_req_valid,rd_req_ready,rd_busy,rd_done,rd_success,rd_error,rd_cancelled;
 logic[63:0]rd_addr;logic[31:0]rd_words;
 logic[127:0]rd_data;logic rd_valid,rd_ready,rd_fire;

 logic[13:0]map_word_q;
 logic map_receive,map_write,bad_map_beat,map_read_enable;
 logic[15:0]map_read_index;
 logic[1:0]map_bank_q;
 logic[31:0]map_values[4];
 logic[31:0]map_value;
 logic[15:0]max_x_q5,max_y_q5;
 logic[11:0]last_source_x,last_source_y,map_x0,map_y0,map_x1,map_y1;

 logic[5:0]root_q;
 // Cached64 initial32x32 root bounds, accumulated during existing map writes.
 logic[63:0]root_valid_q;
 logic[5:0]map_load_root,root_cache_read_index;
 (* ram_style="distributed" *)logic[11:0]root_min_x[0:63],root_min_y[0:63],root_max_x[0:63],root_max_y[0:63];
 logic[11:0]load_x0[4],load_y0[4],load_x1[4],load_y1[4];
 logic[11:0]beat_min_x,beat_min_y,beat_max_x,beat_max_y;
 function automatic logic[11:0]minimum12(input logic[11:0]a,b);return a<b?a:b;endfunction
 function automatic logic[11:0]maximum12(input logic[11:0]a,b);return a>b?a:b;endfunction
 assign map_load_root={map_word_q[13:11],map_word_q[5:3]};
 assign root_cache_read_index=state_q==ROOT_START ? root_q:map_load_root;
 for(genvar lane=0;lane<4;lane=lane+1)begin:g_load_bounds
  assign load_x0[lane]={1'b0,rd_data[lane*32+5+:11]};
  assign load_y0[lane]={1'b0,rd_data[lane*32+21+:11]};
  assign load_x1[lane]=load_x0[lane]<last_source_x ? load_x0[lane]+12'd1:load_x0[lane];
  assign load_y1[lane]=load_y0[lane]<last_source_y ? load_y0[lane]+12'd1:load_y0[lane];
 end
 assign beat_min_x=minimum12(minimum12(load_x0[0],load_x0[1]),minimum12(load_x0[2],load_x0[3]));
 assign beat_min_y=minimum12(minimum12(load_y0[0],load_y0[1]),minimum12(load_y0[2],load_y0[3]));
 assign beat_max_x=maximum12(maximum12(load_x1[0],load_x1[1]),maximum12(load_x1[2],load_x1[3]));
 assign beat_max_y=maximum12(maximum12(load_y1[0],load_y1[1]),maximum12(load_y1[2],load_y1[3]));
 always_ff@(posedge clk_i)begin
  if(rst_i)root_valid_q<=0;
  else if(cmd_valid_i&&cmd_ready_o)root_valid_q<=0;
  else if(map_write)begin
   root_valid_q[map_load_root]<=1;
   root_min_x[map_load_root]<=root_valid_q[map_load_root] ? minimum12(root_min_x[root_cache_read_index],beat_min_x):beat_min_x;
   root_min_y[map_load_root]<=root_valid_q[map_load_root] ? minimum12(root_min_y[root_cache_read_index],beat_min_y):beat_min_y;
   root_max_x[map_load_root]<=root_valid_q[map_load_root] ? maximum12(root_max_x[root_cache_read_index],beat_max_x):beat_max_x;
   root_max_y[map_load_root]<=root_valid_q[map_load_root] ? maximum12(root_max_y[root_cache_read_index],beat_max_y):beat_max_y;
  end
 end

 logic[7:0]rect_x_q,rect_y_q;
 logic[5:0]rect_w_q,rect_h_q;
 logic[7:0]stack_x[10],stack_y[10];
 logic[5:0]stack_w[10],stack_h[10];logic[3:0]stack_count_q;
 logic[5:0]scan_x_q,scan_y_q;
 logic scan_all_q,scan_pending_q;
 logic[7:0]scan_abs_x,scan_abs_y,query_abs_x,query_abs_y;
 logic[11:0]box_min_x_q,box_min_y_q,box_max_x_q,box_max_y_q;
 logic[12:0]box_w,box_h;

 logic cache_cfg_valid,cache_cfg_ready,cache_cfg_error,cache_load_ready,cache_load_error,cache_load_complete;
 logic cache_load_valid,cache_load_fire,cache_resident,cache_busy;
 logic cache_query_valid,cache_query_ready,cache_query_fire;
 logic cache_response_valid,cache_response_ready,cache_pixel_valid;
 logic[1:0]cache_status;logic[23:0]cache_rgb,cache_q7;
 logic[31:0]cache_tag;logic cache_eol,cache_eof;
 logic[7:0]tile_w_q,tile_h_q,source_row_q,source_pixel_q;
 logic[63:0]source_row_addr_q,source_read_addr_q;
 logic[31:0]source_read_words_q;
 logic[3:0]source_skip_q;
 logic source_first_q,source_done_q;
 logic[255:0]byte_buffer_q,byte_buffer_d;
 logic[5:0]byte_count_q,byte_count_d;
 logic[255:0]append_data;
 logic[5:0]append_bytes;
 logic[9:0]source_row_span;

 logic[5:0]output_row_q,query_column_q,response_column_q;
 logic queries_all_q,query_payload_valid_q;
 logic[15:0]query_pixel_q;
 logic query_payload_eol_q,query_payload_eof_q;
 logic query_map_advance;
 logic[7:0]output_y;
 logic[15:0]row_start_pixel,expected_response_pixel;
 logic row_response_bad,cache_fault,abort_now;
 wire local_rst=rst_i||(state_q==FLUSH);

 function automatic logic[63:0]triple(input logic[63:0]v);
  return v+(v<<1);
 endfunction
 assign cmd_ready_o=state_q==IDLE&&!rst_i&&!cancel_i;
 assign busy_o=state_q!=IDLE;
 // No new request is issued while cancel_i; reader must also release pending AR/R.
 // Normal output uses only internal BRAM, so it is also read-quiescent.
 assign read_quiesced_o=!rd_busy&&!m_axi_arvalid_o&&!rd_req_valid&&
  (cancel_i||state_q==OUTPUT_INIT||state_q==OUTPUT_RUN||state_q==FINISH||state_q==IDLE);
 assign header_bad=source_width_i==0||source_width_i>12'd2048||source_height_i==0||source_height_i>12'd2048||
  64'(source_stride_i)<triple(64'(source_width_i))||source_addr_i[3:0]!=0||map_addr_i[3:0]!=0;
 assign source_alloc_bytes=((mul_acc_q+64'd15)&64'hfffffffffffffff0)+64'd16;
 assign source_end={1'b0,source_addr_q}+{1'b0,source_alloc_bytes}-65'd1;
 assign map_end={1'b0,map_addr_q}+65'd262143;
 assign ranges_bad=source_end[64]||map_end[64]||
  !((source_end<{1'b0,map_addr_q})||(map_end<{1'b0,source_addr_q}));
 assign last_source_x=source_width_q-12'd1;
 assign last_source_y=source_height_q-12'd1;
 assign max_x_q5={last_source_x[10:0],5'd0};
 assign max_y_q5={last_source_y[10:0],5'd0};
 assign rd_addr=state_q==MAP_REQ ? map_addr_q : source_read_addr_q;
 assign rd_words=state_q==MAP_REQ ? 32'd16384 : source_read_words_q;
 assign rd_req_valid=(state_q==MAP_REQ||state_q==SRC_REQ)&&!abort_now;
 assign rd_ready=(state_q==MAP_WAIT)||(state_q==SRC_RUN&&byte_count_q<=6'd16)||(state_q==ABORT);
 assign rd_fire=rd_valid&&rd_ready;
 assign map_receive=state_q==MAP_WAIT&&rd_fire;
 always_comb begin
  bad_map_beat=1'b0;
  for(integer k=0;k<4;k=k+1)begin
   if(rd_data[k*32+:16]>max_x_q5||rd_data[k*32+16+:16]>max_y_q5)bad_map_beat=1'b1;
  end
  bad_map_beat=bad_map_beat&&map_receive;
 end
 assign map_write=map_receive&&!bad_map_beat&&!abort_now;
 assign scan_abs_x=rect_x_q+8'(scan_x_q);
 assign scan_abs_y=rect_y_q+8'(scan_y_q);
 assign query_abs_x=rect_x_q+8'(query_column_q);
 assign output_y=rect_y_q+8'(output_row_q);
 assign query_abs_y=output_y;
 assign map_read_index=state_q==SCAN ? {scan_abs_y,scan_abs_x}:{query_abs_y,query_abs_x};
 assign query_map_advance=!query_payload_valid_q||cache_query_ready;
 assign map_read_enable=!abort_now&&((state_q==SCAN&&!scan_all_q)||
  (state_q==ROW_RUN&&!queries_all_q&&query_map_advance));
 assign map_value=map_values[map_bank_q];
 for(genvar b=0;b<4;b=b+1)begin:g_map
  (* ram_style="block" *)logic[31:0]memory[0:16383];
  always_ff@(posedge clk_i)begin
   if(map_write)memory[map_word_q]<=rd_data[b*32+:32];
   if(map_read_enable)map_values[b]<=memory[map_read_index[15:2]];
  end
 end
 always_ff@(posedge clk_i)if(map_read_enable)map_bank_q<=map_read_index[1:0];
 assign map_x0={1'b0,map_value[15:5]};
 assign map_y0={1'b0,map_value[31:21]};
 assign map_x1=map_x0<last_source_x ? map_x0+12'd1:map_x0;
 assign map_y1=map_y0<last_source_y ? map_y0+12'd1:map_y0;
 assign box_w={1'b0,box_max_x_q}-{1'b0,box_min_x_q}+13'd1;
 assign box_h={1'b0,box_max_y_q}-{1'b0,box_min_y_q}+13'd1;

 assign cache_cfg_valid=state_q==CACHE_CFG&&!abort_now;
 assign cache_load_valid=state_q==SRC_RUN&&source_pixel_q<tile_w_q&&byte_count_q>=6'd3&&!abort_now;
 assign cache_load_fire=cache_load_valid&&cache_load_ready;
 assign source_row_span={6'd0,source_row_addr_q[3:0]}+10'(triple(64'(tile_w_q)));
 always_comb begin
  byte_buffer_d=byte_buffer_q;byte_count_d=byte_count_q;
  append_data={128'd0,rd_data};append_bytes=6'd16;
  if(source_first_q)begin
   append_data=append_data>>(source_skip_q*8);
   append_bytes=6'd16-{2'd0,source_skip_q};
  end
  if(cache_load_fire)begin byte_buffer_d=byte_buffer_d>>24;byte_count_d=byte_count_d-6'd3;end
  if(state_q==SRC_RUN&&rd_fire&&!abort_now)begin
   byte_buffer_d=byte_buffer_d|(append_data<<(byte_count_d*8));
   byte_count_d=byte_count_d+append_bytes;
  end
 end
 assign cache_query_valid=state_q==ROW_RUN&&query_payload_valid_q&&!abort_now;
 assign cache_query_fire=cache_query_valid&&cache_query_ready;
 assign cache_response_ready=state_q==ROW_RUN||state_q==ABORT;
 assign row_start_pixel={output_y,rect_x_q};
 assign expected_response_pixel={output_y,rect_x_q+8'(response_column_q)};

 assign row_response_bad=state_q==ROW_RUN&&cache_response_valid&&(
  !cache_pixel_valid||cache_status!=0||cache_tag!={16'd0,expected_response_pixel}||
  cache_eol!=(response_column_q==rect_w_q-6'd1)||
  cache_eof!=((response_column_q==rect_w_q-6'd1)&&(output_row_q==rect_h_q-6'd1)));
 assign cache_fault=cache_cfg_error||cache_load_error||row_response_bad;
 assign abort_now=state_q==ABORT||state_q==FLUSH||
  (state_q!=IDLE&&state_q!=FINISH&&cancel_i)||cache_fault||bad_map_beat||
  (rd_done&&!rd_success);

 crop_tile128_q7_extent_axis cache(
  .clk_i,.rst_i(local_rst),.cfg_valid_i(cache_cfg_valid),.cfg_ready_o(cache_cfg_ready),
  .source_width_i(source_width_q),.source_height_i(source_height_q),.origin_x_i(box_min_x_q),.origin_y_i(box_min_y_q),
  .extent_width_i(box_w[7:0]),.extent_height_i(box_h[7:0]),
  .cfg_error_o(cache_cfg_error),.load_valid_i(cache_load_valid),.load_ready_o(cache_load_ready),
  .load_bgr_i(byte_buffer_q[23:0]),.load_eol_i(source_pixel_q==tile_w_q-8'd1),
  .load_eof_i(source_pixel_q==tile_w_q-8'd1&&source_row_q==tile_h_q-8'd1),
  .load_error_o(cache_load_error),.load_complete_o(cache_load_complete),
  .query_valid_i(cache_query_valid),.query_ready_o(cache_query_ready),
  .query_x_q5_i({16'd0,map_value[15:0]}),.query_y_q5_i({16'd0,map_value[31:16]}),
  .query_tag_i({16'd0,query_pixel_q}),.query_eol_i(query_payload_eol_q),.query_eof_i(query_payload_eof_q),
  .response_valid_o(cache_response_valid),.response_ready_i(cache_response_ready),
  .pixel_valid_o(cache_pixel_valid),.response_status_o(cache_status),.rgb_u8_o(cache_rgb),.rgb_q7_o(cache_q7),
  .response_tag_o(cache_tag),.response_eol_o(cache_eol),.response_eof_o(cache_eof),
  .resident_valid_o(cache_resident),.busy_o(cache_busy)
 );
 tensor_axi128_read reader(.clk_i,.rst_i,
  .req_valid_i(rd_req_valid),.req_ready_o(rd_req_ready),.req_addr_i(rd_addr),.req_words_i(rd_words),.req_tag_i(BUS_TAG),
  .cancel_i(abort_now),.busy_o(rd_busy),.done_o(rd_done),.success_o(rd_success),.error_o(rd_error),.cancelled_o(rd_cancelled),.status_tag_o(),
  .m_data_o(rd_data),.m_keep_o(),.m_tag_o(),.m_valid_o(rd_valid),.m_ready_i(rd_ready),.m_last_o(),
  .m_axi_araddr_o(m_axi_araddr_o),
  .m_axi_arlen_o(m_axi_arlen_o),
  .m_axi_arsize_o(m_axi_arsize_o),
  .m_axi_arprot_o(m_axi_arprot_o),
  .m_axi_arburst_o(m_axi_arburst_o),
  .m_axi_arcache_o(m_axi_arcache_o),
  .m_axi_arqos_o(m_axi_arqos_o),
  .m_axi_arid_o(m_axi_arid_o),
  .m_axi_arlock_o(m_axi_arlock_o),
  .m_axi_arvalid_o(m_axi_arvalid_o),
  .m_axi_arready_i(m_axi_arready_i),
  .m_axi_rdata_i(m_axi_rdata_i),
  .m_axi_rresp_i(m_axi_rresp_i),
  .m_axi_rid_i(m_axi_rid_i),
  .m_axi_rlast_i(m_axi_rlast_i),
  .m_axi_rvalid_i(m_axi_rvalid_i),
  .m_axi_rready_o(m_axi_rready_o));

 // True row-major output storage: one byte/channel plane, no external gather.
 logic[7:0]output_r_q,output_g_q,output_b_q;
 logic[16:0]output_pixel_q;
 logic[13:0]output_word_q;
 logic output_read_enable,output_read_pending_q,output_pop,axis_fire;
 logic[255:0]output_buffer_q,output_buffer_d;
 logic[5:0]output_count_q,output_count_d;
 assign m_axis_tkeep_o=16'hffff;
 assign m_axis_tuser_o=ticket_o;
 assign axis_fire=m_axis_tvalid_o&&m_axis_tready_i;
 assign output_pop=state_q==OUTPUT_RUN&&(!m_axis_tvalid_o||m_axis_tready_i)&&
  output_count_q>=6'd16&&!abort_now;
 // Room for current pending pixel and the next synchronous RAM result.
 assign output_read_enable=state_q==OUTPUT_RUN&&output_pixel_q<17'd65536&&
  (output_count_q<=6'd25||output_pop)&&!abort_now;
 (* ram_style="block" *)logic[7:0]output_r[0:65535],output_g[0:65535],output_b[0:65535];
 always_ff@(posedge clk_i)begin
  if(state_q==ROW_RUN&&cache_response_valid&&cache_response_ready&&!abort_now)begin
   output_r[expected_response_pixel]<=cache_q7[7:0];
   output_g[expected_response_pixel]<=cache_q7[15:8];
   output_b[expected_response_pixel]<=cache_q7[23:16];
  end
  if(output_read_enable)begin
   output_r_q<=output_r[output_pixel_q[15:0]];
   output_g_q<=output_g[output_pixel_q[15:0]];
   output_b_q<=output_b[output_pixel_q[15:0]];
  end
 end
 always_comb begin
  output_buffer_d=output_buffer_q;output_count_d=output_count_q;
  if(output_pop)begin output_buffer_d=output_buffer_d>>128;output_count_d=output_count_d-6'd16;end
  if(state_q==OUTPUT_RUN&&output_read_pending_q&&!abort_now)begin
   output_buffer_d=output_buffer_d|({232'd0,output_b_q,output_g_q,output_r_q}<<(output_count_d*8));
   output_count_d=output_count_d+6'd3;
  end
 end
 always_ff@(posedge clk_i)begin
  if(rst_i)begin
   m_axis_tvalid_o<=0;m_axis_tlast_o<=0;output_read_pending_q<=0;
   output_pixel_q<=0;output_word_q<=0;output_count_q<=0;
  end else begin
   if(axis_fire)m_axis_tvalid_o<=0;
   output_read_pending_q<=output_read_enable;
   if(state_q==OUTPUT_INIT||state_q==FLUSH)begin
    output_pixel_q<=0;output_word_q<=0;output_count_q<=0;output_buffer_q<=0;
    output_read_pending_q<=0;m_axis_tvalid_o<=0;
   end else if(state_q==OUTPUT_RUN&&!abort_now)begin
    output_buffer_q<=output_buffer_d;output_count_q<=output_count_d;
    if(output_read_enable)output_pixel_q<=output_pixel_q+17'd1;
    if(output_pop)begin
     m_axis_tdata_o<=output_buffer_q[127:0];m_axis_tvalid_o<=1;
     m_axis_tlast_o<=output_word_q==14'd12287;output_word_q<=output_word_q+14'd1;
    end
   end
  end
 end


 always_ff@(posedge clk_i)begin
  if(rst_i)begin
   state_q<=IDLE;done_o<=0;success_o<=0;error_o<=0;cancelled_o<=0;error_code_o<=0;ticket_o<=0;
   query_payload_valid_q<=0;scan_pending_q<=0;stack_count_q<=0;
  end else begin
   done_o<=0;success_o<=0;
   if(state_q!=IDLE&&cancel_i)cancelled_o<=1;
   if(map_write)map_word_q<=map_word_q+14'd1;
   if(state_q==SRC_RUN)begin
    byte_buffer_q<=byte_buffer_d;byte_count_q<=byte_count_d;
    if(rd_fire)source_first_q<=0;
    if(cache_load_fire)source_pixel_q<=source_pixel_q+8'd1;
    if(rd_done&&rd_success)source_done_q<=1;
   end
   if(state_q==ROW_RUN&&query_map_advance)begin
    query_payload_valid_q<=map_read_enable;
    if(map_read_enable)begin
     query_pixel_q<=map_read_index;
     query_payload_eol_q<=query_column_q==rect_w_q-6'd1;
     query_payload_eof_q<=query_column_q==rect_w_q-6'd1&&output_row_q==rect_h_q-6'd1;
     if(query_column_q==rect_w_q-6'd1)queries_all_q<=1;
     else query_column_q<=query_column_q+6'd1;
    end
   end
   if(state_q==ROW_RUN&&cache_response_valid&&cache_response_ready&&!abort_now)begin
    response_column_q<=response_column_q+6'd1;
    if(response_column_q==rect_w_q-6'd1)begin
     if(output_row_q==rect_h_q-6'd1)state_q<=RECT_DONE;
     else begin output_row_q<=output_row_q+6'd1;state_q<=ROW_PLAN;end
    end
   end
   case(state_q)
    IDLE:if(cmd_valid_i&&cmd_ready_o)begin
     ticket_o<=ticket_i;error_o<=0;cancelled_o<=0;error_code_o<=0;
     source_addr_q<=source_addr_i;map_addr_q<=map_addr_i;
     source_width_q<=source_width_i;source_height_q<=source_height_i;stride_q<=source_stride_i;
     stack_count_q<=0;query_payload_valid_q<=0;
     if(header_bad)begin error_o<=1;error_code_o<=1;state_q<=FINISH;end
     else begin mul_acc_q<=0;mul_value_q<={32'd0,source_stride_i};mul_bits_q<=source_height_i;mul_step_q<=0;state_q<=DESC_MUL;end
    end
    DESC_MUL:begin
     if(mul_bits_q[0])mul_acc_q<=mul_acc_q+mul_value_q;
     mul_value_q<=mul_value_q<<1;mul_bits_q<=mul_bits_q>>1;
     if(mul_step_q==11)state_q<=DESC_CHECK;else mul_step_q<=mul_step_q+4'd1;
    end
    DESC_CHECK:if(ranges_bad)begin error_o<=1;error_code_o<=1;state_q<=FINISH;end
     else begin map_word_q<=0;state_q<=MAP_REQ;end
    MAP_REQ:if(rd_req_valid&&rd_req_ready)state_q<=MAP_WAIT;
    MAP_WAIT:if(rd_done&&rd_success)begin root_q<=0;state_q<=ROOT_START;end
    ROOT_START:begin
     rect_x_q<={root_q[2:0],5'd0};rect_y_q<={root_q[5:3],5'd0};rect_w_q<=32;rect_h_q<=32;
     box_min_x_q<=root_min_x[root_cache_read_index];box_min_y_q<=root_min_y[root_cache_read_index];
     box_max_x_q<=root_max_x[root_cache_read_index];box_max_y_q<=root_max_y[root_cache_read_index];
     state_q<=BOX_EVAL;
    end
    SCAN_INIT:begin
     scan_x_q<=0;scan_y_q<=0;scan_all_q<=0;scan_pending_q<=0;
     box_min_x_q<=last_source_x;box_min_y_q<=last_source_y;box_max_x_q<=0;box_max_y_q<=0;
     state_q<=SCAN;
    end
    SCAN:begin
     scan_pending_q<=map_read_enable;
     if(map_read_enable)begin
      if(scan_x_q==rect_w_q-6'd1)begin
       scan_x_q<=0;if(scan_y_q==rect_h_q-6'd1)scan_all_q<=1;else scan_y_q<=scan_y_q+6'd1;
      end else scan_x_q<=scan_x_q+6'd1;
     end
     if(scan_pending_q)begin
      if(map_x0<box_min_x_q)box_min_x_q<=map_x0;
      if(map_y0<box_min_y_q)box_min_y_q<=map_y0;
      if(map_x1>box_max_x_q)box_max_x_q<=map_x1;
      if(map_y1>box_max_y_q)box_max_y_q<=map_y1;
      if(scan_all_q)state_q<=BOX_EVAL;
     end
    end
    BOX_EVAL:begin
     if(box_w<=128&&box_h<=128)state_q<=CACHE_CFG;
     else if(stack_count_q==10||(rect_w_q==1&&rect_h_q==1))begin error_o<=1;error_code_o<=6;state_q<=ABORT;end
     else begin
      if(rect_w_q>=rect_h_q&&rect_w_q>1)begin
       stack_x[stack_count_q]<=rect_x_q+(8'(rect_w_q)>>1);stack_y[stack_count_q]<=rect_y_q;
       stack_w[stack_count_q]<=rect_w_q-(rect_w_q>>1);stack_h[stack_count_q]<=rect_h_q;
       rect_w_q<=rect_w_q>>1;
      end else begin
       stack_x[stack_count_q]<=rect_x_q;stack_y[stack_count_q]<=rect_y_q+(8'(rect_h_q)>>1);
       stack_w[stack_count_q]<=rect_w_q;stack_h[stack_count_q]<=rect_h_q-(rect_h_q>>1);
       rect_h_q<=rect_h_q>>1;
      end
      stack_count_q<=stack_count_q+4'd1;state_q<=SCAN_INIT;
     end
    end
    CACHE_CFG:if(cache_cfg_valid&&cache_cfg_ready)begin
     tile_w_q<=box_w[7:0];
     tile_h_q<=box_h[7:0];
     source_row_q<=0;mul_acc_q<=source_addr_q+triple(64'(box_min_x_q));
     mul_value_q<={32'd0,stride_q};mul_bits_q<=box_min_y_q;mul_step_q<=0;state_q<=SRC_MUL;
    end
    SRC_MUL:begin
     if(mul_bits_q[0])mul_acc_q<=mul_acc_q+mul_value_q;
     mul_value_q<=mul_value_q<<1;mul_bits_q<=mul_bits_q>>1;
     if(mul_step_q==11)begin source_row_addr_q<=mul_acc_q+(mul_bits_q[0]?mul_value_q:64'd0);state_q<=SRC_PLAN;end
     else mul_step_q<=mul_step_q+4'd1;
    end
    SRC_PLAN:begin
     source_read_addr_q<={source_row_addr_q[63:4],4'd0};
     source_read_words_q<=({22'd0,source_row_span}+32'd15)>>4;
     source_skip_q<=source_row_addr_q[3:0];source_first_q<=1;source_done_q<=0;
     source_pixel_q<=0;byte_buffer_q<=0;byte_count_q<=0;state_q<=SRC_REQ;
    end
    SRC_REQ:if(rd_req_valid&&rd_req_ready)state_q<=SRC_RUN;
    SRC_RUN:if(source_pixel_q==tile_w_q&&(source_done_q||(rd_done&&rd_success)))begin
     if(source_row_q==tile_h_q-8'd1)state_q<=CACHE_READY;
     else begin source_row_q<=source_row_q+8'd1;source_row_addr_q<=source_row_addr_q+{32'd0,stride_q};state_q<=SRC_PLAN;end
    end
    CACHE_READY:if(cache_resident&&!cache_busy)begin output_row_q<=0;state_q<=ROW_PLAN;end
    ROW_PLAN:begin
     query_column_q<=0;queries_all_q<=0;query_payload_valid_q<=0;response_column_q<=0;
     state_q<=ROW_RUN;
    end
    ROW_RUN:begin end
    RECT_DONE:begin
     if(stack_count_q!=0)begin
      rect_x_q<=stack_x[stack_count_q-4'd1];rect_y_q<=stack_y[stack_count_q-4'd1];
      rect_w_q<=stack_w[stack_count_q-4'd1];rect_h_q<=stack_h[stack_count_q-4'd1];
      stack_count_q<=stack_count_q-4'd1;state_q<=SCAN_INIT;
     end else if(root_q==63)state_q<=OUTPUT_INIT;
     else begin root_q<=root_q+6'd1;state_q<=ROOT_START;end
    end
    OUTPUT_INIT:state_q<=OUTPUT_RUN;
    OUTPUT_RUN:if(axis_fire&&m_axis_tlast_o)state_q<=FINISH;
    ABORT:if(!rd_busy&&!m_axis_tvalid_o)state_q<=FLUSH;
    FLUSH:begin query_payload_valid_q<=0;state_q<=FINISH;end
    FINISH:begin done_o<=1;success_o<=!error_o&&!cancelled_o&&!cancel_i;state_q<=IDLE;end
    default:state_q<=IDLE;
   endcase
   if(state_q!=IDLE&&state_q!=ABORT&&state_q!=FLUSH&&state_q!=FINISH&&abort_now)begin
    if(bad_map_beat)begin error_o<=1;error_code_o<=2;end
    else if(cache_fault)begin error_o<=1;error_code_o<=5;end
    else if(rd_done&&rd_error)begin error_o<=1;error_code_o<=3;end
    state_q<=ABORT;
   end
  end
 end
endmodule
