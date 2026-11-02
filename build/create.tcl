# Create the TwinCoach BD using its exact collected HDL references.
# Usage: vivado -mode batch -source build/create.tcl -tclargs NEW_PROJECT_DIR DPU_REPO validate PART BOARD_PART TOOL_VERSION
# This creates/generates a project; it does not synthesize, implement or deploy.
set root [file normalize [file join [file dirname [info script]] ..]]
if {[llength $argv] != 6} { error "Pass project directory, DPU IP repo, stage, part, board part and tool version" }
set output [file normalize [lindex $argv 0]]
if {[file exists $output]} { error "Output already exists: $output" }
set expected_version [lindex $argv 5]
if {![string match "$expected_version*" [version -short]]} { error "Platform requires Vivado $expected_version" }
create_project twincoach $output -part [lindex $argv 3]
set_property board_part [lindex $argv 4] [current_project]
set_property ip_repo_paths [list [file normalize [lindex $argv 1]]] [current_project]
update_ip_catalog
source [file join $root build fpga inputs.tcl]
add_files -norecurse $source_files
update_compile_order -fileset sources_1
source [file join $root build fpga bd.tcl]
validate_bd_design
save_bd_design
set bd [get_files */posepriv_dpu_isolation.bd]
if {[lindex $argv 2] eq "generate"} { generate_target all $bd }
set wrappers [make_wrapper -files $bd -top]
add_files -norecurse $wrappers
set_property top posepriv_dpu_isolation_wrapper [get_filesets sources_1]
update_compile_order -fileset sources_1
puts "TWINCOACH_PROJECT_CREATED $output"
close_project
