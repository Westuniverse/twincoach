/* SPDX-License-Identifier: GPL-2.0-only */
/* Included after ps_start/ps_wait. All ROI and legacy ioctls share ps->lock. */

static int ps_roi_release(struct ps_device *ps)
{
	u32 status;
	writel(ROI1_CMD_ACK, ps->roi_regs + ROI1_REG_COMMAND);
	writel(ROI1_CMD_RELEASE_LEGACY, ps->roi_regs + ROI1_REG_COMMAND);
	status = readl(ps->roi_regs + ROI1_REG_STATUS);
	return status & (ROI1_STATUS_BUSY | ROI1_STATUS_DONE | ROI1_STATUS_ROUTE_ROI |
			 ROI1_STATUS_ABORT_EMIT) ? -EIO : 0;
}

static void ps_roi_fault(struct ps_device *ps, bool started)
{
	u32 status;
	int drain = 0;
	ps->faulted = true;
	if (started) {
		status = readl(ps->roi_regs + ROI1_REG_STATUS);
		if (!(status & ROI1_STATUS_DONE)) {
			writel(ROI1_CMD_CANCEL, ps->roi_regs + ROI1_REG_COMMAND);
			/* Cancellation gates new AR; accepted AR/R must really drain.
			 * This remains observable if a broken RX holds AXIS not-ready.
			 */
			drain = readl_poll_timeout(ps->roi_regs + ROI1_REG_STATUS, status,
				status & (ROI1_STATUS_DONE | ROI1_STATUS_READ_QUIESCED), 10, 1000000);
		}
	}
	if (drain) {
		/* A fabric that never returns accepted reads cannot be repaired by
			 * resetting S2MM. Retain the source allocation until board reboot.
			 */
		ps->roi_retain_dma = true;
		dev_err(ps->dev, "ROI AXI read drain failed: coherent source retained; board reboot required\n");
	}
	ps_stop(ps);
	/* No ACK/route reuse after a DMA fault, even if source reads drained. */
}

static long ps_roi_ioctl(struct ps_device *ps, unsigned int cmd, unsigned long arg)
{
	struct posepriv_roi_xfer x;
	struct ps_endpoint *rx = &ps->whole_rx;
	u32 map_offset, status = 0;
	dma_addr_t map_dma;
	unsigned long deadline;
	int ret;
	bool started = false, accepted = false;
	bool bench = cmd == POSEPRIV_ROI_STAGE_BENCH;
	if (mutex_lock_interruptible(&ps->lock)) return -ERESTARTSYS;
	if (ps->removed) { ret = -ENODEV; goto out; }
	if (ps->faulted) { ret = -EIO; goto out; }
#ifndef POSEPRIV_ROI_STAGE_BENCH_ENABLED
	if (bench) { ret = -ENOTTY; goto out; }
#endif
	if (!bench && !ps->roi_regs) { ret = -EOPNOTSUPP; goto out; }
	if (!ps->roi_cpu || !rx->cpu) { ret = -ENOMEM; goto out; }
	if (copy_from_user(&x, (void __user *)arg, sizeof(x))) { ret = -EFAULT; goto out; }
	if (!posepriv_roi_shape(&x)) { ret = -EINVAL; goto out; }
	if (!access_ok(u64_to_user_ptr(x.rx_user), ROI1_OUTPUT_BYTES)) { ret = -EFAULT; goto out; }
	map_offset = ALIGN(x.source_bytes, 16) + 16;
	map_dma = ps->roi_dma + map_offset;
	if (copy_from_user(ps->roi_cpu, u64_to_user_ptr(x.source_user), x.source_bytes) ||
	    copy_from_user((unsigned char *)ps->roi_cpu + map_offset,
			u64_to_user_ptr(x.map_user), ROI1_MAP_BYTES)) { ret = -EFAULT; goto out; }
	/* Separate ROI allocation deliberately preserves the immutable static mesh. */
	x.status = 0;
	x.stream_bytes = 0;
	if (bench) {
		/* Explicit cost-only build: cold initialized RX, never a crop result. */
		ret = copy_to_user(u64_to_user_ptr(x.rx_user), rx->cpu, ROI1_OUTPUT_BYTES) ? -EFAULT : 0;
		x.stream_bytes = ROI1_OUTPUT_BYTES;
		accepted = true;
		goto reply;
	}
	if (readl(ps->regs) != ps->hardware_id || readl(ps->regs + 4) & 1) { ret = -EIO; goto fault; }
	status = readl(ps->roi_regs + ROI1_REG_STATUS);
	if (status & (ROI1_STATUS_BUSY | ROI1_STATUS_DONE | ROI1_STATUS_ROUTE_ROI |
		ROI1_STATUS_ABORT_EMIT | ROI1_STATUS_LEGACY_PACKET)) { ret = -EIO; goto fault; }
	writel(lower_32_bits(ps->roi_dma), ps->roi_regs + ROI1_REG_SOURCE_LO);
	writel(upper_32_bits(ps->roi_dma), ps->roi_regs + ROI1_REG_SOURCE_HI);
	writel(lower_32_bits(map_dma), ps->roi_regs + ROI1_REG_MAP_LO);
	writel(upper_32_bits(map_dma), ps->roi_regs + ROI1_REG_MAP_HI);
	writel(x.width | (x.height << 16), ps->roi_regs + ROI1_REG_DIMS);
	writel(x.stride, ps->roi_regs + ROI1_REG_STRIDE);
	writel(lower_32_bits(x.ticket), ps->roi_regs + ROI1_REG_CONFIG_TICKET_LO);
	writel(upper_32_bits(x.ticket), ps->roi_regs + ROI1_REG_CONFIG_TICKET_HI);
	writel(ROI1_CMD_ARM_ROI, ps->roi_regs + ROI1_REG_COMMAND);
	status = readl(ps->roi_regs + ROI1_REG_STATUS);
	if (status != ROI1_STATUS_ROUTE_ROI) { ret = -EIO; goto fault; }
	dma_wmb();
	deadline = jiffies + msecs_to_jiffies(x.timeout_ms);
	ret = ps_start(rx, ROI1_OUTPUT_BYTES, DMA_DEV_TO_MEM); /* S2MM FIRST; no MM2S. */
	if (ret) goto fault;
	started = true;
	writel(ROI1_CMD_START, ps->roi_regs + ROI1_REG_COMMAND);
	status = readl(ps->roi_regs + ROI1_REG_STATUS); /* Flush accepted control. */
	accepted = true;
	ret = ps_wait(rx, deadline);
	if (ret) goto fault;
	ret = readl_poll_timeout(ps->roi_regs + ROI1_REG_STATUS, status,
		status & ROI1_STATUS_DONE, 1, 1000);
	if (ret) goto fault;
	x.status = status;
	x.stream_bytes = readl(ps->roi_regs + ROI1_REG_STREAM_BYTES);
	if (!(status & ROI1_STATUS_CORE_QUIESCED) || !(status & ROI1_STATUS_FRAME_ENDED) ||
	    readl(ps->roi_regs + ROI1_REG_TICKET_LO) != lower_32_bits(x.ticket) ||
	    readl(ps->roi_regs + ROI1_REG_TICKET_HI) != upper_32_bits(x.ticket)) { ret = -EIO; goto fault; }
	if ((status & (ROI1_STATUS_SUCCESS | ROI1_STATUS_ERROR | ROI1_STATUS_CANCELLED)) != ROI1_STATUS_SUCCESS ||
	    x.stream_bytes != ROI1_OUTPUT_BYTES) {
		ret = -EPROTO; /* Completed short/error frame is discarded, then ACKed. */
	} else {
		dma_rmb();
		ret = copy_to_user(u64_to_user_ptr(x.rx_user), rx->cpu, ROI1_OUTPUT_BYTES) ? -EFAULT : 0;
	}
	if (ps_roi_release(ps)) { ret = -EIO; goto fault; }
	goto reply;
fault:
	if (ps->roi_regs) {
		x.status = readl(ps->roi_regs + ROI1_REG_STATUS);
		x.stream_bytes = readl(ps->roi_regs + ROI1_REG_STREAM_BYTES);
	}
	ps_roi_fault(ps, started);
	dev_err(ps->dev, "ROI transaction fault=%d status=%08x; explicit board reset required\n", ret, x.status);
reply:
	if (accepted && copy_to_user((void __user *)arg, &x, sizeof(x))) ret = -EFAULT;
out:
	mutex_unlock(&ps->lock);
	return ret;
}

static int ps_roi_probe(struct platform_device *pdev, struct ps_device *ps)
{
	struct device_node *node;
	struct resource resource;
	int ret;
	if (ps->roi_profile) {
		node = of_parse_phandle(pdev->dev.of_node, "posepriv,roi-sampler", 0);
		if (!node) return -EINVAL;
		ret = of_address_to_resource(node, 0, &resource);
		if (!of_device_is_compatible(node, "posepriv,roi-sampler-v1")) ret = -EINVAL;
		of_node_put(node);
		if (ret) return ret;
		if (resource.start != 0xa2040000 || resource_size(&resource) != 0x1000) return -EINVAL;
		ps->roi_regs = devm_ioremap_resource(ps->dev, &resource);
		if (IS_ERR(ps->roi_regs)) return PTR_ERR(ps->roi_regs);
		if (readl(ps->roi_regs + ROI1_REG_ID) != ROI1_ID ||
		    readl(ps->roi_regs + ROI1_REG_VERSION) != ROI1_VERSION ||
		    readl(ps->roi_regs + ROI1_REG_STATUS) ||
		    readl(ps->roi_regs + ROI1_REG_OUTPUT_BYTES) != ROI1_OUTPUT_BYTES ||
		    readl(ps->roi_regs + ROI1_REG_MAP_BYTES) != ROI1_MAP_BYTES) return -ENODEV;
	}
#ifndef POSEPRIV_ROI_STAGE_BENCH_ENABLED
	if (!ps->roi_profile) return 0;
#endif
	ps->roi_cpu = dma_alloc_coherent(ps->dev, POSEPRIV_ROI_CAPACITY, &ps->roi_dma, GFP_KERNEL);
	if (!ps->roi_cpu) return -ENOMEM;
	if (!pp_dma_region_valid(ps->roi_dma, POSEPRIV_ROI_CAPACITY)) return -ERANGE;
#ifdef POSEPRIV_ROI_STAGE_BENCH_ENABLED
	if (ps->whole_rx.cpu) memset(ps->whole_rx.cpu, 0xa5, ROI1_OUTPUT_BYTES);
#endif
	return 0;
}
