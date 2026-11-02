# SPDX-License-Identifier: GPL-2.0-only
ifneq ($(KERNELRELEASE),)
obj-m += posepriv_tail4.o
else
.PHONY: module
module:
	@test -n "$(KDIR)" || { echo "Set KDIR to matching ARM64 kernel headers" >&2; exit 2; }
	$(MAKE) -C "$(KDIR)" M="$(CURDIR)" ARCH=arm64 CROSS_COMPILE="$(CROSS_COMPILE)" modules
endif
