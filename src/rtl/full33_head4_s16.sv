// SPDX-License-Identifier: GPL-2.0-only
// A genuine4PE shared-A W16/A16 coordinate-head tile, maximum144 groups.
// Four128-bit SDP weight banks; PE-major/group-minor load, no16PE duplication.
// Raw4xS48 and post4xS16 retire together under one valid/ready owner.
module full33_head4_s16(
 input logic clk_i,rst_i,
 input logic cfg_valid_i,output logic cfg_ready_o,input logic[7:0]cfg_groups_i,
 output logic cfg_error_o,
 input logic w_valid_i,output logic w_ready_o,input logic[127:0]w_data_i,
 input logic p_valid_i,output logic p_ready_o,input logic[1:0]p_pe_i,
 input logic signed[47:0]p_bias_i,input logic[4:0]p_shift_i,
 input logic s_valid_i,output logic s_ready_o,input logic[127:0]features_i,input logic[15:0]tag_i,
 output logic m_valid_o,input logic m_ready_i,
 output logic[191:0]raw_o,output logic[63:0]values_o,output logic[15:0]tag_o
);
 localparam integer MAX_GROUPS=144;
 logic loading_q,loaded_q,stage_valid_q,config_open_q;
 logic[7:0]groups_q,write_group_q,group_q;
 logic[1:0]write_pe_q;logic[3:0]pending_q,post_mask_q;
 logic[127:0]features_q,read_weight_q[0:3];
 logic[15:0]tag_q;
 logic first_q,last_q;
 logic signed[47:0]bias_q[0:3];logic[4:0]shift_q[0:3];
 wire[511:0]weights;wire[191:0]biases,mac_sums;wire[19:0]shifts;
 wire[15:0]mac_tag;
 wire mac_ready,mac_valid,post_ready;
 wire stage_advance=~stage_valid_q|mac_ready;
 wire cfg_accept=cfg_valid_i&cfg_ready_o;
 wire weight_accept=w_valid_i&w_ready_o;
 wire post_accept=p_valid_i&p_ready_o;
 wire data_accept=s_valid_i&s_ready_o;
 wire data_last=(group_q==groups_q-8'd1);
 wire output_accept=m_valid_o&m_ready_i;
 assign cfg_ready_o=~rst_i&~loading_q&(group_q==0)&(pending_q==0)&~stage_valid_q;
 assign w_ready_o=~rst_i&loading_q;
 assign p_ready_o=~rst_i&config_open_q&(group_q==0)&(pending_q==0)&~stage_valid_q&~cfg_valid_i;
 assign s_ready_o=~rst_i&loaded_q&~loading_q&(&post_mask_q)&~cfg_accept&~post_accept&stage_advance;
 for(genvar p=0;p<4;p=p+1)begin: weight_bank
  (* ram_style="block" *)logic[127:0]memory[0:MAX_GROUPS-1];
  always_ff @(posedge clk_i)begin
   if(weight_accept&&write_pe_q==2'(p))memory[write_group_q]<=w_data_i;
   if(data_accept)read_weight_q[p]<=memory[group_q];
  end
  assign weights[p*128+:128]=read_weight_q[p];
  assign biases[p*48+:48]=bias_q[p];assign shifts[p*5+:5]=shift_q[p];
 end
 head4_mac_s16 mac(.clk_i(clk_i),.rst_i(rst_i),
  .s_valid_i(stage_valid_q),.s_ready_o(mac_ready),.features_i(features_q),.weights_i(weights),
  .first_i(first_q),.last_i(last_q),.tag_i(tag_q),
  .m_valid_o(mac_valid),.m_ready_i(post_ready),.sums_o(mac_sums),.tag_o(mac_tag));
 head4_requant_s48 post(.clk_i(clk_i),.rst_i(rst_i),.biases_i(biases),.shifts_i(shifts),
  .s_valid_i(mac_valid),.s_ready_o(post_ready),.sums_i(mac_sums),.tag_i(mac_tag),
  .m_valid_o(m_valid_o),.m_ready_i(m_ready_i),.raw_o(raw_o),.values_o(values_o),.tag_o(tag_o));
 always_ff @(posedge clk_i)begin
  if(rst_i)begin
   loading_q<=0;loaded_q<=0;stage_valid_q<=0;config_open_q<=0;
   cfg_error_o<=0;post_mask_q<=0;group_q<=0;pending_q<=0;write_group_q<=0;write_pe_q<=0;
  end else begin
   cfg_error_o<=0;
   if(cfg_accept)begin
    loaded_q<=0;write_group_q<=0;write_pe_q<=0;post_mask_q<=0;
    if(cfg_groups_i==0||cfg_groups_i>8'd144)begin loading_q<=0;config_open_q<=0;cfg_error_o<=1;end
    else begin groups_q<=cfg_groups_i;loading_q<=1;config_open_q<=1;end
   end
   if(weight_accept)begin
    if(write_group_q==groups_q-8'd1)begin
     write_group_q<=0;
     if(write_pe_q==3)begin loading_q<=0;loaded_q<=1;end
     else write_pe_q<=write_pe_q+2'd1;
    end else write_group_q<=write_group_q+8'd1;
   end
   if(post_accept)begin bias_q[p_pe_i]<=p_bias_i;shift_q[p_pe_i]<=p_shift_i;post_mask_q[p_pe_i]<=1;end
   if(stage_advance)begin
    stage_valid_q<=data_accept;
    if(data_accept)begin features_q<=features_i;tag_q<=tag_i;first_q<=(group_q==0);last_q<=data_last;end
   end
   if(data_accept)begin group_q<=data_last?8'd0:group_q+8'd1;config_open_q<=0;end
   case({data_accept&data_last,output_accept})
    2'b10:pending_q<=pending_q+4'd1;
    2'b01:pending_q<=pending_q-4'd1;
    default:;
   endcase
  end
 end
endmodule
