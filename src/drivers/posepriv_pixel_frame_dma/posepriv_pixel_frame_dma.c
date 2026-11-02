// SPDX-License-Identifier: GPL-2.0-only
// Standalone PixelShuffle whole-frame candidate; NEVER binds di38s endpoints.
#include <linux/completion.h>
#include <linux/dmaengine.h>
#include <linux/dma-mapping.h>
#include <linux/fs.h>
#include <linux/interrupt.h>
#include <linux/io.h>
#include <linux/irq.h>
#include <linux/jiffies.h>
#include <linux/kref.h>
#include <linux/ktime.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/of_address.h>
#include <linux/platform_device.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include "posepriv_pixel_frame_contract.h"
#include "../posepriv_dma_proxy/posepriv_dma_bounds.h"

static bool copy_timing;
module_param(copy_timing, bool, 0444);
MODULE_PARM_DESC(copy_timing, "Opt-in copy-path wall timing; excludes log/return, NOT pure PL cycles");

struct pf_copy_timing {
	u64 stamp[8];
	unsigned int stages;
	bool enabled;
};
static void pf_timing_mark(struct pf_copy_timing *t, unsigned int stage)
{
	if (t->enabled) {
		t->stamp[stage] = ktime_get_ns();
		t->stages = stage;
	}
}
static u64 pf_timing_span(const struct pf_copy_timing *t, unsigned int stage)
{
	/* A failed/unfinished stage is absent, NOT a measured zero-time success. */
	return stage <= t->stages ? t->stamp[stage] - t->stamp[stage - 1] : 0;
}

struct pf_endpoint {
	struct dma_chan *chan;
	struct device *dma_dev;
	void *cpu;
	dma_addr_t dma;
	void __iomem *regs; /* read-only DMASR; DMAengine owns control writes */
	unsigned int bytes;
	dma_cookie_t cookie;
	struct completion done;
};
struct pf_device {
	struct device *dev;
	struct miscdevice misc;
	struct pf_endpoint end[2];
	void *tx_frozen; /* CPU-only kvmalloc, NEVER supplied to a DMA API */
	struct posepriv_pixel_frame_status status;
	struct mutex lock;
	struct kref refs;
	unsigned long deadline;
	atomic_t protocol_error;
	int error_irq;
	bool irq_owned, opened, removed;
};
static void pf_timing_log(struct pf_device *pf, const struct pf_copy_timing *t, int result)
{
	if (!t->enabled || !t->stages)
		return;
	dev_info(pf->dev, "pixel_frame_copy_timing abi=1 stages=%u lock_header_ns=%llu copy_tx_ns=%llu padding_ns=%llu poison_ns=%llu core_ns=%llu post_ns=%llu copy_rx_ns=%llu ioctl_prelog_ns=%llu result=%d\n",
		t->stages, pf_timing_span(t, 1), pf_timing_span(t, 2),
		pf_timing_span(t, 3), pf_timing_span(t, 4), pf_timing_span(t, 5),
		pf_timing_span(t, 6), pf_timing_span(t, 7),
		t->stamp[t->stages] - t->stamp[0], result);
}
static void pf_destroy(struct kref *ref)
{
	struct pf_device *pf = container_of(ref, struct pf_device, refs);
	put_device(pf->dev);
	kfree(pf);
}
static void pf_complete(void *arg)
{
	struct pf_endpoint *ep = arg;
	complete(&ep->done);
}
static void pf_latch_error(struct pf_device *pf)
{
	/* Sticky RTL line: mask once, wake waiters, NEVER automatically re-enable. */
	if (atomic_cmpxchg(&pf->protocol_error, 0, 1) == 0)
		disable_irq_nosync(pf->error_irq);
	complete_all(&pf->end[0].done);
	complete_all(&pf->end[1].done);
}
static irqreturn_t pf_error_irq(int irq, void *arg)
{
	pf_latch_error(arg);
	return IRQ_HANDLED;
}
static int pf_check_error(void *ctx)
{
	struct pf_device *pf = ctx;
	bool pending, active;
	int ret;
	if (atomic_read(&pf->protocol_error)) goto error;
	ret = irq_get_irqchip_state(pf->error_irq, IRQCHIP_STATE_PENDING, &pending);
	if (ret) return ret; /* Unsupported observation is NOT a clear error flag. */
	ret = irq_get_irqchip_state(pf->error_irq, IRQCHIP_STATE_ACTIVE, &active);
	if (ret) return ret;
	if (pending || active || atomic_read(&pf->protocol_error)) goto error;
	if (pixel_frame_dma_status_error(readl(pf->end[0].regs + 0x04),
					 readl(pf->end[1].regs + 0x34))) return -EIO;
	return 0;
error:
	pf_latch_error(pf);
	pf->status.protocol_error = 1;
	return -EPROTO;
}
static void pf_snapshot(struct pf_device *pf)
{
	pf->status.mm2s_status = readl(pf->end[0].regs + 0x04);
	pf->status.s2mm_status = readl(pf->end[1].regs + 0x34);
	pf->status.protocol_error = atomic_read(&pf->protocol_error);
}
static void pf_terminate(struct pf_device *pf)
{
	int i;
	for (i = 0; i < 2; i++)
		if (pf->end[i].chan)
			dmaengine_terminate_sync(pf->end[i].chan);
}
static void pf_stop(void *ctx)
{
	struct pf_device *pf = ctx;
	pf_snapshot(pf); /* Preserve pre-terminate status; don't erase fault evidence. */
	pf_terminate(pf);
}
static int pf_start(void *ctx, unsigned int index)
{
	struct pf_device *pf = ctx;
	struct pf_endpoint *ep = &pf->end[index];
	struct dma_async_tx_descriptor *desc;
	int ret = pf_check_error(pf);
	if (ret) return ret;
	reinit_completion(&ep->done);
	desc = dmaengine_prep_slave_single(ep->chan, ep->dma, ep->bytes,
		index ? DMA_DEV_TO_MEM : DMA_MEM_TO_DEV,
		DMA_PREP_INTERRUPT | DMA_CTRL_ACK);
	if (!desc) return -EIO;
	desc->callback = pf_complete;
	desc->callback_param = ep;
	ep->cookie = dmaengine_submit(desc);
	if (dma_submit_error(ep->cookie)) return -EIO;
	dma_async_issue_pending(ep->chan);
	return 0;
}
static int pf_wait(void *ctx, unsigned int index)
{
	struct pf_device *pf = ctx;
	struct pf_endpoint *ep = &pf->end[index];
	unsigned long remaining;
	int ret;
	/* Check delivered completion first: both channels share ONE deadline. */
	if (!try_wait_for_completion(&ep->done)) {
		if (time_after_eq(jiffies, pf->deadline)) return -ETIMEDOUT;
		remaining = pf->deadline - jiffies;
		if (!wait_for_completion_timeout(&ep->done, remaining)) return -ETIMEDOUT;
	}
	ret = pf_check_error(pf);
	if (ret) return ret;
	return dmaengine_tx_status(ep->chan, ep->cookie, NULL) == DMA_COMPLETE ? 0 : -EIO;
}
static const struct pixel_frame_ops pf_ops = {
	.check_error = pf_check_error, .start = pf_start, .wait = pf_wait, .stop = pf_stop,
};
static int pf_open(struct inode *inode, struct file *file)
{
	struct pf_device *pf = container_of(file->private_data, struct pf_device, misc);
	int ret = 0;
	mutex_lock(&pf->lock);
	if (pf->removed) ret = -ENODEV;
	else if (pf->opened) ret = -EBUSY;
	else {
		kref_get(&pf->refs);
		pf->opened = true;
		file->private_data = pf;
	}
	mutex_unlock(&pf->lock);
	return ret; /* Faulted devices can reopen for cached diagnostics, NOT retry. */
}
static int pf_close(struct inode *inode, struct file *file)
{
	struct pf_device *pf = file->private_data;
	mutex_lock(&pf->lock);
	pf->opened = false;
	mutex_unlock(&pf->lock);
	kref_put(&pf->refs, pf_destroy);
	return 0;
}
static long pf_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
	struct pf_device *pf = file->private_data;
	struct posepriv_pixel_frame_xfer xfer;
	struct pf_copy_timing timing = { .enabled = copy_timing && cmd == POSEPRIV_PIXEL_FRAME_XFER };
	u64 begin;
	int ret;
	pf_timing_mark(&timing, 0);
	if (cmd != POSEPRIV_PIXEL_FRAME_XFER && cmd != POSEPRIV_PIXEL_FRAME_STATUS)
		return -ENOTTY;
	if (mutex_lock_interruptible(&pf->lock)) return -ERESTARTSYS;
	if (pf->removed) { ret = -ENODEV; goto out; }
	if (cmd == POSEPRIV_PIXEL_FRAME_STATUS) {
		ret = copy_to_user((void __user *)arg, &pf->status, sizeof(pf->status)) ? -EFAULT : 0;
		goto out;
	}
	if (pf->status.faulted) { ret = -EIO; goto out; }
	if (copy_from_user(&xfer, (void __user *)arg, sizeof(xfer))) { ret = -EFAULT; goto out; }
	if (!pixel_frame_header_valid(&xfer)) { ret = -EINVAL; goto out; }
	if (!access_ok(u64_to_user_ptr(xfer.rx_user), xfer.output_bytes)) { ret = -EFAULT; goto out; }
	pf_timing_mark(&timing, 1);
	ret = copy_from_user(pf->tx_frozen, u64_to_user_ptr(xfer.tx_user), xfer.input_bytes) ? -EFAULT : 0;
	pf_timing_mark(&timing, 2);
	if (ret) goto out;
	/* Validate the private frozen copy, not memory a userspace thread can change. */
	ret = pixel_frame_payload_valid(pf->tx_frozen, xfer.input_bytes) ? 0 : -EINVAL;
	pf_timing_mark(&timing, 3);
	if (ret) goto out;
	/* Only the validated private snapshot is published; coherent API unchanged.
	 * Legacy timing field poison_ns also includes this TX publication + barrier.
	 */
	memcpy(pf->end[0].cpu, pf->tx_frozen, xfer.input_bytes);
	memset(pf->end[1].cpu, 0xa5, xfer.output_bytes);
	dma_wmb(); /* coherent DMA API allocation; no streaming-buffer sync misuse */
	begin = ktime_get_ns();
	if (timing.enabled) { timing.stamp[4] = begin; timing.stages = 4; }
	pf->deadline = jiffies + msecs_to_jiffies(xfer.timeout_ms);
	ret = pixel_frame_run(pf, &pf_ops, &pf->status);
	pf->status.elapsed_ns = ktime_get_ns() - begin;
	if (timing.enabled) { timing.stamp[5] = begin + pf->status.elapsed_ns; timing.stages = 5; }
	if (ret) {
		dev_err(pf->dev, "whole-frame fault=%d mm2s=%08x s2mm=%08x protocol_error=%u; no automatic retry\n",
			ret, pf->status.mm2s_status, pf->status.s2mm_status, pf->status.protocol_error);
		goto out;
	}
	dma_rmb();
	ret = pf_check_error(pf);
	if (ret) {
		pf->status.faulted = 1;
		pf->status.last_errno = ret;
		pf_stop(pf);
		goto out;
	}
	pf_snapshot(pf);
	pf_timing_mark(&timing, 6);
	ret = copy_to_user(u64_to_user_ptr(xfer.rx_user), pf->end[1].cpu, xfer.output_bytes) ? -EFAULT : 0;
	pf_timing_mark(&timing, 7);
	pf->status.last_errno = ret;
out:
	/* Opt-in printk is AFTER every measured span; default has no diagnostic clock reads/log. */
	pf_timing_log(pf, &timing, ret);
	mutex_unlock(&pf->lock);
	return ret;
}
static const struct file_operations pf_fops = {
	.owner = THIS_MODULE, .open = pf_open, .release = pf_close,
	.unlocked_ioctl = pf_ioctl, .llseek = no_llseek,
};
static void pf_free(struct pf_device *pf)
{
	int i;
	/* Non-devm IRQ: stop handler BEFORE state/buffers/channels can be freed. */
	if (pf->irq_owned) {
		free_irq(pf->error_irq, pf);
		pf->irq_owned = false;
	}
	pf_terminate(pf);
	kvfree(pf->tx_frozen);
	pf->tx_frozen = NULL;
	for (i = 0; i < 2; i++) {
		struct pf_endpoint *ep = &pf->end[i];
		if (ep->cpu) dma_free_coherent(ep->dma_dev, ep->bytes, ep->cpu, ep->dma);
		if (ep->chan) dma_release_channel(ep->chan);
	}
}
static int pf_endpoint_init(struct pf_device *pf, unsigned int index)
{
	struct pf_endpoint *ep = &pf->end[index];
	struct of_phandle_args args;
	struct resource res;
	struct device_node *child;
	const char *name = index ? "rx" : "tx";
	u32 bits, addrwidth, datawidth;
	int slot, ret;
	slot = of_property_match_string(pf->dev->of_node, "dma-names", name);
	if (slot < 0) return slot;
	ret = of_parse_phandle_with_args(pf->dev->of_node, "dmas", "#dma-cells", slot, &args);
	if (ret) return ret;
	ret = -EINVAL;
	if (!of_device_is_compatible(args.np, "xlnx,axi-dma-1.00.a") ||
	    args.args_count != 1 || args.args[0] != index ||
	    of_address_to_resource(args.np, 0, &res) ||
	    res.start != (index ? 0xa0010000 : 0xa0000000) || resource_size(&res) != 0x10000 ||
	    of_property_read_u32(args.np, "xlnx,sg-length-width", &bits) || bits != 24 ||
	    of_property_read_u32(args.np, "xlnx,addrwidth", &addrwidth) || addrwidth != 32 ||
	    of_property_read_bool(args.np, "xlnx,include-sg") ||
	    of_get_available_child_count(args.np) != 1) goto put;
	child = of_get_next_available_child(args.np, NULL);
	if (!child) goto put;
	ret = of_device_is_compatible(child, index ? "xlnx,axi-dma-s2mm-channel" : "xlnx,axi-dma-mm2s-channel") &&
		!of_property_read_u32(child, "xlnx,datawidth", &datawidth) && datawidth == 128 &&
		!of_property_read_bool(child, "xlnx,include-dre") ? 0 : -EINVAL;
	of_node_put(child);
	if (ret) goto put;
	ep->bytes = index ? POSEPRIV_PIXEL_OUTPUT_BYTES : POSEPRIV_PIXEL_INPUT_BYTES;
	if (pp_dma_packet_limit(bits, ep->bytes) != ep->bytes) { ret = -EINVAL; goto put; }
	ep->chan = dma_request_chan(pf->dev, name);
	if (IS_ERR(ep->chan)) { ret = PTR_ERR(ep->chan); ep->chan = NULL; goto put; }
	ep->dma_dev = ep->chan->device->dev;
	if (ep->dma_dev->of_node != args.np) { ret = -EINVAL; goto put; }
	/* Map for READS only; don't claim/request the DMAengine driver's resource. */
	ep->regs = devm_ioremap(pf->dev, res.start, resource_size(&res));
	if (!ep->regs) { ret = -ENOMEM; goto put; }
	ret = dma_set_mask_and_coherent(ep->dma_dev, DMA_BIT_MASK(32));
	if (ret) goto put;
	ep->cpu = dma_alloc_coherent(ep->dma_dev, ep->bytes, &ep->dma, GFP_KERNEL);
	if (!ep->cpu) { ret = -ENOMEM; goto put; }
	if (!pp_dma_region_valid(ep->dma, ep->bytes)) { ret = -ERANGE; goto put; }
	dev_info(pf->dev, "%s coherent=%pad bytes=%u HP0_DDR_LOW align=16\n", name, &ep->dma, ep->bytes);
	ret = 0;
put:
	of_node_put(args.np);
	return ret;
}
static int pf_probe(struct platform_device *pdev)
{
	struct pf_device *pf;
	struct irq_data *irq_data;
	u32 width, height;
	int ret, i;
	if (strcmp(pdev->dev.of_node->name, "posepriv_pixel_shuffle_frame_dma") ||
	    of_property_read_u32(pdev->dev.of_node, "posepriv,input-width", &width) || width != 640 ||
	    of_property_read_u32(pdev->dev.of_node, "posepriv,input-height", &height) || height != 360 ||
	    of_property_count_strings(pdev->dev.of_node, "dma-names") != 2) return -EINVAL;
	pf = kzalloc(sizeof(*pf), GFP_KERNEL);
	if (!pf) return -ENOMEM;
	pf->dev = get_device(&pdev->dev);
	kref_init(&pf->refs);
	mutex_init(&pf->lock);
	atomic_set(&pf->protocol_error, 0);
	for (i = 0; i < 2; i++) init_completion(&pf->end[i].done);
	/* One reusable ordinary-memory snapshot; physical continuity is not needed. */
	pf->tx_frozen = kvmalloc(POSEPRIV_PIXEL_INPUT_BYTES, GFP_KERNEL);
	if (!pf->tx_frozen) { ret = -ENOMEM; goto fail; }
	pf->error_irq = platform_get_irq_byname(pdev, "protocol_error");
	if (pf->error_irq < 0) { ret = pf->error_irq; goto fail; }
	irq_data = irq_get_irq_data(pf->error_irq);
	/* HWH concat In2 -> PL0[2]: GIC SPI91 / hardware INTID123, NOT Linux IRQ number. */
	if (!irq_data || irq_data->hwirq != 123 ||
	    irq_get_trigger_type(pf->error_irq) != IRQ_TYPE_LEVEL_HIGH) { ret = -EINVAL; goto fail; }
	for (i = 0; i < 2; i++) {
		ret = pf_endpoint_init(pf, i);
		if (ret) goto fail;
	}
	ret = request_irq(pf->error_irq, pf_error_irq, 0, "posepriv_pixel_protocol_error", pf);
	if (ret) goto fail;
	pf->irq_owned = true;
	ret = pf_check_error(pf);
	if (ret) goto fail;
	pf->status.tx_dma = pf->end[0].dma;
	pf->status.rx_dma = pf->end[1].dma;
	pf->status.input_bytes = POSEPRIV_PIXEL_INPUT_BYTES;
	pf->status.output_bytes = POSEPRIV_PIXEL_OUTPUT_BYTES;
	pf_snapshot(pf);
	pf->misc.minor = MISC_DYNAMIC_MINOR;
	pf->misc.name = "posepriv_pixel_shuffle_frame_dma";
	pf->misc.parent = pf->dev;
	pf->misc.fops = &pf_fops;
	ret = misc_register(&pf->misc);
	if (ret) goto fail;
	platform_set_drvdata(pdev, pf);
	return 0;
fail:
	pf_free(pf);
	kref_put(&pf->refs, pf_destroy);
	return ret;
}
static int pf_remove(struct platform_device *pdev)
{
	struct pf_device *pf = platform_get_drvdata(pdev);
	misc_deregister(&pf->misc);
	mutex_lock(&pf->lock);
	pf->removed = true;
	pf_free(pf);
	mutex_unlock(&pf->lock);
	kref_put(&pf->refs, pf_destroy); /* Surviving fds return ENODEV, retain no resources. */
	return 0;
}
static const struct of_device_id pf_match[] = {
	{ .compatible = "posepriv,pixel-shuffle-frame-dma-v1" }, { }
};
MODULE_DEVICE_TABLE(of, pf_match);
static struct platform_driver pf_driver = {
	.probe = pf_probe, .remove = pf_remove,
	.driver = { .name = "posepriv_pixel_frame_dma", .of_match_table = pf_match },
};
module_platform_driver(pf_driver);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("PosePriv standalone PixelShuffle RX-first whole-frame DMA candidate");
