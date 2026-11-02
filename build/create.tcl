# Recreate the measured di46 BD using its exact collected HDL references.
# Usage: vivado -mode batch -source build/create.tcl -tclargs NEW_PROJECT_DIR DPU_REPO validate
# This creates/generates a project; it does not synthesize, implement or deploy.
set root [file normalize [file join [file dirname [info script]] ..]]
if {![string match "2026.1*" [version -short]]} { error "Vivado 2026.1 required" }
if {[llength $argv] != 3} { error "Pass a fresh project directory, DPU IP repo and validate/generate" }
set output [file normalize [lindex $argv 0]]
if {[file exists $output]} { error "Output already exists: $output" }
create_project posepriv_di46 $output -part xck26-sfvc784-2LV-c
set_property board_part xilinx.com:kv260_som:part0:1.4 [current_project]
set_property ip_repo_paths [list [file normalize [lindex $argv 1]]] [current_project]
update_ip_catalog
source [file join $root build di46 inputs.tcl]
add_files -norecurse $source_files
update_compile_order -fileset sources_1
source [file join $root build di46 bd.tcl]
validate_bd_design
save_bd_design
set bd [get_files */posepriv_dpu_isolation.bd]
if {[lindex $argv 2] eq "generate"} { generate_target all $bd }
set wrappers [make_wrapper -files $bd -top]
add_files -norecurse $wrappers
set_property top posepriv_dpu_isolation_wrapper [get_filesets sources_1]
update_compile_order -fileset sources_1
puts "POSEPRIV_DI46_PROJECT_CREATED $output"
close_project
