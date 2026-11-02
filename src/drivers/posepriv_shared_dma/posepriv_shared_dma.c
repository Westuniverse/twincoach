// SPDX-License-Identifier: GPL-2.0-only
// One kernel-serialized operation: copy -> config -> RX -> TX -> wait -> copy.
// This candidate does not bind the stable xlnx,dma_proxy endpoints.
#include <linux/completion.h>
#include <linux/dmaengine.h>
#include <linux/dma-mapping.h>
#include <linux/fs.h>
#include <linux/io.h>
#include <linux/jiffies.h>
#include <linux/kref.h>
#include <linux/miscdevice.h>
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/of_address.h>
#include <linux/iopoll.h>
#include <linux/platform_device.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include "posepriv_shared_dma_contract.h"
#include "posepriv_roi_uapi.h"
#include "../posepriv_dma_proxy/posepriv_dma_bounds.h"

struct ps_endpoint {
	struct dma_chan *chan;
	void *cpu;
	void *cached; /* CPU-only packet snapshot; never passed to the DMA engine. */
	dma_addr_t dma;
	struct completion done;
	dma_cookie_t cookie;
	unsigned int limit, capacity;
};
struct ps_device {
	struct device *dev;
	struct miscdevice misc;
	struct ps_endpoint end[2]; /* TX, legacy RX */
	struct ps_endpoint whole_rx; /* Same RX channel, separate optional 256 KiB buffers. */
	u32 static_mesh_bytes; /* Zero: no immutable mesh for this exclusive open. */
	void __iomem *regs;
	void __iomem *roi_regs;
	void *roi_cpu;
	dma_addr_t roi_dma;
	bool roi_profile, roi_retain_dma;
	u32 hardware_id;
	struct mutex lock;
	struct kref refs;
	bool opened, faulted, removed;
};

static void ps_destroy(struct kref *ref)
{
	struct ps_device *ps = container_of(ref, struct ps_device, refs);
	put_device(ps->dev);
	kfree(ps);
}
static void ps_complete(void *arg)
{
	struct ps_endpoint *ep = arg;
	complete(&ep->done);
}
static int ps_open(struct inode *inode, struct file *file)
{
	struct ps_device *ps = container_of(file->private_data, struct ps_device, misc);
	int ret = 0;
	mutex_lock(&ps->lock);
	if (ps->removed)
		ret = -ENODEV;
	else if (ps->faulted)
		ret = -EIO;
	else if (ps->opened)
		ret = -EBUSY;
	else {
		kref_get(&ps->refs);
		ps->opened = true;
		ps->static_mesh_bytes = 0;
		file->private_data = ps;
	}
	mutex_unlock(&ps->lock);
	return ret;
}
static int ps_close(struct inode *inode, struct file *file)
{
	struct ps_device *ps = file->private_data;
	mutex_lock(&ps->lock);
	ps->opened = false;
	ps->static_mesh_bytes = 0;
	mutex_unlock(&ps->lock);
	kref_put(&ps->refs, ps_destroy);
	return 0;
}
static void ps_stop(struct ps_device *ps)
{
	int i;
	for (i = 0; i < 2; i++)
		if (ps->end[i].chan)
			dmaengine_terminate_sync(ps->end[i].chan);
}
static int ps_start(struct ps_endpoint *ep, unsigned int bytes,
		    enum dma_transfer_direction direction)
{
	struct dma_async_tx_descriptor *desc;
	reinit_completion(&ep->done);
	desc = dmaengine_prep_slave_single(ep->chan, ep->dma, bytes, direction,
		DMA_PREP_INTERRUPT | DMA_CTRL_ACK);
	if (!desc)
		return -EIO;
	desc->callback = ps_complete;
	desc->callback_param = ep;
	ep->cookie = dmaengine_submit(desc);
	if (dma_submit_error(ep->cookie))
		return -EIO;
	dma_async_issue_pending(ep->chan);
	return 0;
}
static int ps_wait(struct ps_endpoint *ep, unsigned long deadline)
{
	unsigned long remaining;
	/* A completion already delivered remains valid even if the deadline passed
	 * while waiting for the other channel. Check it before computing remaining.
	 */
	if (!try_wait_for_completion(&ep->done)) {
		if (time_after_eq(jiffies, deadline))
			return -ETIMEDOUT;
		remaining = deadline - jiffies;
		if (!wait_for_completion_timeout(&ep->done, remaining))
			return -ETIMEDOUT;
	}
	return dmaengine_tx_status(ep->chan, ep->cookie, NULL) == DMA_COMPLETE ? 0 : -EIO;
}
#include "posepriv_roi_transaction.inc"

static long ps_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
	struct ps_device *ps = file->private_data;
	struct posepriv_shared_xfer xfer;
	unsigned int output, tx_bytes;
	struct ps_endpoint *rx;
	bool whole = cmd == POSEPRIV_SKINNING_WHOLE_XFER;
	bool upload = cmd == POSEPRIV_SKINNING_STATIC_UPLOAD;
	bool static_run = cmd == POSEPRIV_SKINNING_STATIC_RUN;
	unsigned long deadline;
	int ret;
	if (cmd == POSEPRIV_ROI_XFER || cmd == POSEPRIV_ROI_STAGE_BENCH)
		return ps_roi_ioctl(ps, cmd, arg);
	if (cmd != POSEPRIV_SHARED_XFER && !whole && !upload && !static_run)
		return -ENOTTY;
	if (mutex_lock_interruptible(&ps->lock))
		return -ERESTARTSYS;
	if (ps->removed) { ret = -ENODEV; goto out; }
	if (ps->faulted) { ret = -EIO; goto out; }
	if (copy_from_user(&xfer, (void __user *)arg, sizeof(xfer))) { ret = -EFAULT; goto out; }
	if (upload) {
		if (!posepriv_skinning_static_upload_shape(&xfer)) { ret = -EINVAL; goto out; }
		if (ps->hardware_id != POSEPRIV_SHARED_ID_V4) { ret = -EOPNOTSUPP; goto out; }
		if (ps->static_mesh_bytes) { ret = -EBUSY; goto out; }
		if (!ps->whole_rx.cpu || !ps->whole_rx.cached) { ret = -ENOMEM; goto out; }
		if (xfer.input_bytes + POSEPRIV_SKINNING_HEADER_BYTES > ps->end[0].limit ||
		    xfer.input_bytes > ps->whole_rx.limit) { ret = -EINVAL; goto out; }
		if (copy_from_user((unsigned char *)ps->end[0].cached + POSEPRIV_SKINNING_HEADER_BYTES,
				u64_to_user_ptr(xfer.tx_user), xfer.input_bytes)) { ret = -EFAULT; goto out; }
		if (!posepriv_skinning_vertices_valid((unsigned char *)ps->end[0].cached +
				POSEPRIV_SKINNING_HEADER_BYTES, xfer.input_bytes)) { ret = -EINVAL; goto out; }
		memcpy((unsigned char *)ps->end[0].cpu + POSEPRIV_SKINNING_HEADER_BYTES,
		       (unsigned char *)ps->end[0].cached + POSEPRIV_SKINNING_HEADER_BYTES, xfer.input_bytes);
		ps->static_mesh_bytes = xfer.input_bytes; /* Publish only after the cold trusted copy. */
		ret = 0;
		goto out;
	}
	/* A supported ioctl on an old image is not a hardware transaction: reject
	 * new mode before touching config/DMA, without poisoning the old device. */
	if ((ps->roi_profile && xfer.operator_id == 4) ||
	    !posepriv_shared_operator_supported(ps->hardware_id, xfer.operator_id)) {
		ret = -EOPNOTSUPP; goto out;
	}
	rx = whole || static_run ? &ps->whole_rx : &ps->end[1];
	if (static_run) {
		if (!posepriv_skinning_static_run_shape(&xfer)) { ret = -EINVAL; goto out; }
		if (!ps->static_mesh_bytes) { ret = -ENODATA; goto out; }
		output = ps->static_mesh_bytes;
		tx_bytes = POSEPRIV_SKINNING_HEADER_BYTES + output;
	} else {
		output = whole ? posepriv_skinning_whole_output_bytes(xfer.operator_id, xfer.input_bytes) :
			posepriv_shared_output_bytes(xfer.operator_id, xfer.input_bytes);
		tx_bytes = xfer.input_bytes;
		if (xfer.reserved || !xfer.timeout_ms || xfer.timeout_ms > 2000 ||
		    !xfer.tx_user || !xfer.rx_user) { ret = -EINVAL; goto out; }
	}
	if (!output || tx_bytes > ps->end[0].limit) { ret = -EINVAL; goto out; }
	/* Only this pre-DMA allocation failure permits explicit v1 fallback. */
	if ((whole || static_run) && (!rx->cpu || !rx->cached)) { ret = -ENOMEM; goto out; }
	if (output > rx->limit) { ret = -EINVAL; goto out; }
	if (!access_ok(u64_to_user_ptr(xfer.rx_user), output) ||
	    copy_from_user(ps->end[0].cached, u64_to_user_ptr(xfer.tx_user), xfer.input_bytes)) {
		ret = -EFAULT; goto out;
	}
	if (static_run ? !posepriv_skinning_header_valid(ps->end[0].cached) :
	    (whole ? !posepriv_skinning_whole_payload_valid(ps->end[0].cached, xfer.input_bytes) :
	    !posepriv_shared_payload_valid(ps->end[0].cached, xfer.operator_id, xfer.input_bytes))) {
		ret = -EINVAL; goto out;
	}
	if (!static_run) ps->static_mesh_bytes = 0; /* Legacy TX may overwrite fixed vertices. */
	memcpy(ps->end[0].cpu, ps->end[0].cached, xfer.input_bytes);
	if (readl(ps->regs) != ps->hardware_id || (readl(ps->regs + 4) & 1)) {
		ret = -EIO; goto fault;
	}
	if (xfer.operator_id == 4 && ((unsigned char *)ps->end[0].cached)[4] == 2 &&
	    !(readl(ps->regs + 4) & 4)) { ret = -ENODATA; goto out; }
	writel(xfer.operator_id, ps->regs + 8);
	/* Readback flushes the posted MMIO write; do not arm MM2S before accepted config. */
	if (readl(ps->regs + 8) != xfer.operator_id || !posepriv_shared_configured_idle(ps->hardware_id, readl(ps->regs + 4))) {
		ret = -EIO; goto fault;
	}
	memset(rx->cpu, 0xa5, output);
	dma_wmb();
	deadline = jiffies + msecs_to_jiffies(xfer.timeout_ms);
	ret = ps_start(rx, output, DMA_DEV_TO_MEM); /* S2MM FIRST */
	if (ret) goto fault;
	ret = ps_start(&ps->end[0], tx_bytes, DMA_MEM_TO_DEV);
	if (ret) goto fault;
	ret = ps_wait(&ps->end[0], deadline);
	if (ret) goto fault;
	ret = ps_wait(rx, deadline);
	if (ret) goto fault;
	dma_rmb();
	if (readl(ps->regs) != ps->hardware_id || !posepriv_shared_configured_idle(ps->hardware_id, readl(ps->regs + 4)) ||
	    readl(ps->regs + 8) != xfer.operator_id) {
		ret = -EIO; goto fault;
	}
	memcpy(rx->cached, rx->cpu, output);
	if (xfer.operator_id == 3 && !posepriv_shared_lighting_output_valid(
			ps->end[0].cached, rx->cached, xfer.input_bytes)) {
		ret = -EPROTO; goto fault;
	}
	if (xfer.operator_id == 4 && (!posepriv_raster_output_valid(
	        ps->end[0].cached, rx->cached, xfer.input_bytes) || !(readl(ps->regs + 4) & 4))) {
		ret = -EPROTO; goto fault;
	}
	if (xfer.operator_id == 5 && (whole || static_run ?
	    !posepriv_skinning_whole_output_valid(rx->cached, tx_bytes) :
	    !posepriv_skinning_output_valid(rx->cached, xfer.input_bytes))) {
		ret = -EPROTO; goto fault;
	}
	ret = copy_to_user(u64_to_user_ptr(xfer.rx_user), rx->cached, output) ? -EFAULT : 0;
	goto out;
fault:
	ps->faulted = true;
	ps->static_mesh_bytes = 0;
	ps_stop(ps);
	dev_err(ps->dev, "shared DMA fault=%d op=%u input=%u output=%u status=%08x; explicit PL reset required\n",
		ret, xfer.operator_id, xfer.input_bytes, output, readl(ps->regs + 4));
out:
	mutex_unlock(&ps->lock);
	return ret;
}
static const struct file_operations ps_fops = {
	.owner = THIS_MODULE, .open = ps_open, .release = ps_close,
	.unlocked_ioctl = ps_ioctl, .llseek = no_llseek,
};
static void ps_free_channels(struct ps_device *ps)
{
	int i;
	ps_stop(ps);
	if (ps->roi_cpu && !ps->roi_retain_dma)
		dma_free_coherent(ps->dev, POSEPRIV_ROI_CAPACITY, ps->roi_cpu, ps->roi_dma);
	if (ps->whole_rx.cpu)
		dma_free_coherent(ps->dev, ps->whole_rx.capacity, ps->whole_rx.cpu, ps->whole_rx.dma);
	kvfree(ps->whole_rx.cached);
	/* whole_rx.chan aliases end[1].chan; release that channel only once below. */
	for (i = 0; i < 2; i++) {
		struct ps_endpoint *ep = &ps->end[i];
		if (ep->cpu)
			dma_free_coherent(ps->dev, ep->capacity, ep->cpu, ep->dma);
		kvfree(ep->cached);
		if (ep->chan)
			dma_release_channel(ep->chan);
	}
}
static void ps_alloc_whole_rx(struct ps_device *ps, u32 bits)
{
	struct ps_endpoint *ep = &ps->whole_rx;
	if (ps->hardware_id != POSEPRIV_SHARED_ID_V4)
		return;
	ep->capacity = POSEPRIV_SKINNING_WHOLE_CAPACITY;
	ep->limit = pp_dma_packet_limit(bits, ep->capacity);
	if (ep->limit < ep->capacity)
		return;
	ep->chan = ps->end[1].chan;
	init_completion(&ep->done);
	ep->cpu = dma_alloc_coherent(ps->dev, ep->capacity, &ep->dma, GFP_KERNEL);
	if (ep->cpu && pp_dma_region_valid(ep->dma, ep->capacity))
		ep->cached = kvzalloc(ep->capacity, GFP_KERNEL);
	if (!ep->cached && ep->cpu) {
		dma_free_coherent(ps->dev, ep->capacity, ep->cpu, ep->dma);
		ep->cpu = NULL;
	}
}
static int ps_probe(struct platform_device *pdev)
{
	struct ps_device *ps;
	struct resource *resource;
	int i, ret;
	u32 rx_length_bits = 0;
	/* Exact candidate node + separate compatible; no wildcard legacy binding. */
	if (strcmp(pdev->dev.of_node->name, "posepriv_shared_pose_dma"))
		return -ENODEV;
	resource = platform_get_resource(pdev, IORESOURCE_MEM, 0);
	if (!resource || resource->start != 0xa0030000 || resource_size(resource) != 0x1000)
		return -EINVAL;
	ps = kzalloc(sizeof(*ps), GFP_KERNEL);
	if (!ps) return -ENOMEM;
	ps->dev = get_device(&pdev->dev);
	kref_init(&ps->refs);
	mutex_init(&ps->lock);
	ps->regs = devm_platform_ioremap_resource(pdev, 0);
	if (IS_ERR(ps->regs)) { ret = PTR_ERR(ps->regs); goto fail; }
	ps->hardware_id = readl(ps->regs);
	ps->roi_profile = of_device_is_compatible(pdev->dev.of_node, "posepriv,shared-pose-dma-roi-v1");
	if (!posepriv_shared_id_valid(ps->hardware_id) || readl(ps->regs + 4) & 1) {
		ret = -ENODEV; goto fail;
	}
	if (ps->roi_profile ?
	    (ps->hardware_id != POSEPRIV_SHARED_ID_V4 || readl(ps->regs + 12) != 0x2f) :
	    ((ps->hardware_id == POSEPRIV_SHARED_ID_V3 || ps->hardware_id == POSEPRIV_SHARED_ID_V4) &&
	    !posepriv_shared_capabilities_valid(ps->hardware_id, readl(ps->regs + 12)))) {
		ret = -ENODEV; goto fail;
	}
	ret = dma_set_mask_and_coherent(ps->dev, DMA_BIT_MASK(32));
	if (ret) goto fail;
	for (i = 0; i < 2; i++) {
		struct ps_endpoint *ep = &ps->end[i];
		u32 bits;
		init_completion(&ep->done);
		ep->chan = dma_request_chan(ps->dev, i ? "rx" : "tx");
		if (IS_ERR(ep->chan)) { ret = PTR_ERR(ep->chan); ep->chan = NULL; goto fail; }
		ret = of_property_read_u32(ep->chan->device->dev->of_node, "xlnx,sg-length-width", &bits);
		if (ret) goto fail;
		ep->capacity = i == 0 ? posepriv_shared_tx_capacity(ps->hardware_id) : POSEPRIV_SHARED_CAPACITY;
		ep->limit = pp_dma_packet_limit(bits, ep->capacity);
		if (i == 1) rx_length_bits = bits;
		if (ep->limit < ep->capacity) { ret = -EINVAL; goto fail; }
		ep->cpu = dma_alloc_coherent(ps->dev, ep->capacity, &ep->dma, GFP_KERNEL);
		if (!ep->cpu) { ret = -ENOMEM; goto fail; }
		if (!pp_dma_region_valid(ep->dma, ep->capacity)) { ret = -ERANGE; goto fail; }
		ep->cached = kvzalloc(ep->capacity, GFP_KERNEL);
		if (!ep->cached) { ret = -ENOMEM; goto fail; }
		dev_info(ps->dev, "%s coherent=%pad bytes=%u align=16 validated_end=0x80000000 limit=%u\n",
			i ? "rx" : "tx", &ep->dma, ep->capacity, ep->limit);
	}
	ps_alloc_whole_rx(ps, rx_length_bits);
	ret = ps_roi_probe(pdev, ps);
	if (ret) goto fail;
	ps->misc.minor = MISC_DYNAMIC_MINOR;
	ps->misc.name = "posepriv_shared_pose_dma";
	ps->misc.parent = ps->dev;
	ps->misc.fops = &ps_fops;
	ret = misc_register(&ps->misc);
	if (ret) goto fail;
	platform_set_drvdata(pdev, ps);
	dev_info(ps->dev, "shared pose ioctl ready ID=%08x (one atomic RX-first transaction)\n", readl(ps->regs));
	return 0;
fail:
	ps_free_channels(ps);
	kref_put(&ps->refs, ps_destroy);
	return ret;
}
static int ps_remove(struct platform_device *pdev)
{
	struct ps_device *ps = platform_get_drvdata(pdev);
	misc_deregister(&ps->misc);
	mutex_lock(&ps->lock);
	ps->removed = true;
	ps->static_mesh_bytes = 0;
	ps_free_channels(ps);
	mutex_unlock(&ps->lock);
	/* Existing open fds retain only the state; future ioctls return ENODEV. */
	kref_put(&ps->refs, ps_destroy);
	return 0;
}
static const struct of_device_id ps_match[] = {
	{ .compatible = "posepriv,shared-pose-dma-roi-v1" },
	{ .compatible = "posepriv,shared-pose-dma-v1" }, { }
};
MODULE_DEVICE_TABLE(of, ps_match);
static struct platform_driver ps_driver = {
	.probe = ps_probe, .remove = ps_remove,
	.driver = { .name = "posepriv_shared_dma", .of_match_table = ps_match },
};
module_platform_driver(ps_driver);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("PosePriv single atomic shared pose DMA transaction candidate");
