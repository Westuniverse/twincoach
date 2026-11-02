// SPDX-License-Identifier: GPL-2.0-only
// Four parallel bias48 + MAC48 -> signed16 channels; preserve raw and tag.
// Bias/shift pins are cold configuration owned by the enclosing head tile.
module head4_requant_s48(
 input logic clk_i,rst_i,
 input logic[191:0]biases_i,input logic[19:0]shifts_i,
 input logic s_valid_i,output logic s_ready_o,
 input logic[191:0]sums_i,input logic[15:0]tag_i,
 output logic m_valid_o,input logic m_ready_i,
 output logic[191:0]raw_o,output logic[63:0]values_o,output logic[15:0]tag_o
);
 logic[2:0]valid_q;
 logic[191:0]raw_q[0:2];logic[15:0]tag_q[0:2];
 (* use_dsp="no" *)logic signed[48:0]bias_sum_q[0:3];
 (* use_dsp="no" *)logic signed[49:0]rounded_q[0:3],shifted_q[0:3];
 logic[4:0]shift_q[0:3][0:1];
 wire advance=~m_valid_o|m_ready_i;
 wire accept=s_valid_i&s_ready_o;
 assign s_ready_o=~rst_i&advance;
 function automatic logic[15:0]clamp16(input logic signed[49:0]value);
  if(value>50'sd32767)clamp16=16'h7fff;
  else if(value < -50'sd32768)clamp16=16'h8000;
  else clamp16=value[15:0];
 endfunction
 for(genvar p=0;p<4;p=p+1)begin: channel
  wire signed[47:0]raw=$signed(sums_i[p*48+:48]);
  wire signed[47:0]bias=$signed(biases_i[p*48+:48]);
  wire signed[49:0]increment=(shift_q[p][0]==0)?50'sd0:$signed(50'd1<<(shift_q[p][0]-5'd1));
  always_ff @(posedge clk_i)if(advance)begin
   if(accept)begin bias_sum_q[p]<={raw[47],raw}+{bias[47],bias};shift_q[p][0]<=shifts_i[p*5+:5];end
   if(valid_q[0])begin rounded_q[p]<={bias_sum_q[p][48],bias_sum_q[p]}+increment;shift_q[p][1]<=shift_q[p][0];end
   if(valid_q[1])shifted_q[p]<=rounded_q[p]>>>shift_q[p][1];
   if(valid_q[2])values_o[p*16+:16]<=clamp16(shifted_q[p]);
  end
 end
 always_ff @(posedge clk_i)begin
  if(rst_i)begin valid_q<=0;m_valid_o<=0;end
  else if(advance)begin
   valid_q<={valid_q[1:0],accept};
   if(accept)begin raw_q[0]<=sums_i;tag_q[0]<=tag_i;end
   for(integer k=1;k<3;k=k+1)if(valid_q[k-1])begin raw_q[k]<=raw_q[k-1];tag_q[k]<=tag_q[k-1];end
   m_valid_o<=valid_q[2];if(valid_q[2])begin raw_o<=raw_q[2];tag_o<=tag_q[2];end
  end
 end
endmodule
