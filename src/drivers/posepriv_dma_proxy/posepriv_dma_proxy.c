// SPDX-License-Identifier: GPL-2.0-only
/* Temporary DMAengine bridge for PosePriv PL AXI DMA golden comparisons. */
#include <linux/completion.h>
#include <linux/dmaengine.h>
#include <linux/dma-mapping.h>
#include <linux/fs.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include "posepriv_dma_bounds.h"
#include "posepriv_dma_binding.h"

#define PP_BYTES (128 * 1024)
#define PP_COUNT 32
#define PP_STRIDE (PP_BYTES + 16)
#define PP_TOTAL (PP_COUNT * PP_STRIDE)
#define PP_START _IOW('a', 'b', int *)
#define PP_FINISH _IOW('a', 'a', int *)
#define PP_TIMEOUT_MS 2000

struct pp_slot {
	u8 data[PP_BYTES];
	u32 status;
	u32 length;
	u32 reserved[2];
};

struct pp_endpoint {
	struct miscdevice misc;
	struct device *parent;
	struct dma_chan *chan;
	void *cpu;
	dma_addr_t dma;
	struct completion done;
	struct mutex lock;
	bool opened;
	bool faulted;
	bool pending;
	int active_index;
	u32 max_transfer_bytes;
	enum dma_transfer_direction direction;
	char name[48];
};

struct pp_pair {
	struct pp_endpoint end[2];
};

/* Default remains the proven single joint channel; extra pairs are opt-in. */
static char *node_name = "posepriv_unified_joint_proxy";
module_param(node_name, charp, 0444);
MODULE_PARM_DESC(node_name, "Exact DT node name to bind");
static bool enable_motion;
module_param(enable_motion, bool, 0444);
MODULE_PARM_DESC(enable_motion, "Also bind the exact PosePriv motion proxy (default off)");
static bool enable_rectified;
module_param(enable_rectified, bool, 0444);
MODULE_PARM_DESC(enable_rectified, "Also bind the exact PosePriv rectified proxy (default off)");

static void pp_complete(void *arg)
{
	complete(&((struct pp_endpoint *)arg)->done);
}

static int pp_open(struct inode *inode, struct file *file)
{
	struct miscdevice *misc = file->private_data;
	struct pp_endpoint *ep = container_of(misc, struct pp_endpoint, misc);
	int ret = 0;

	/* ioctl locking cannot protect userspace stores into the shared coherent
	 * mapping. One independent file owns an endpoint, including its VMAs.
	 * dup/fork shares that file; its callers still must serialize transactions.
	 */
	mutex_lock(&ep->lock);
	if (ep->opened)
		ret = -EBUSY;
	else if (ep->faulted)
		ret = -EIO;
	else {
		ep->opened = true;
		file->private_data = ep;
	}
	mutex_unlock(&ep->lock);
	return ret;
}

static int pp_close(struct inode *inode, struct file *file)
{
	struct pp_endpoint *ep = file->private_data;

	mutex_lock(&ep->lock);
	if (ep->pending) {
		/* Do not leave DMA writing a region that the next client can modify.
		 * Abandonment is not recovery: the PL packet may be incomplete. Keep
		 * this endpoint failed until explicit module/hardware recovery.
		 */
		dmaengine_terminate_sync(ep->chan);
		ep->pending = false;
		ep->faulted = true;
	}
	ep->opened = false;
	mutex_unlock(&ep->lock);
	return 0;
}

static int pp_mmap(struct file *file, struct vm_area_struct *vma)
{
	struct pp_endpoint *ep = file->private_data;

	/* mmap rounds the requested byte length up to a whole page. */
	if (vma->vm_pgoff || (vma->vm_end - vma->vm_start) != PAGE_ALIGN(PP_TOTAL))
		return -EINVAL;
	return dma_mmap_coherent(ep->parent, vma, ep->cpu, ep->dma, PP_TOTAL);
}

static long pp_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
	struct pp_endpoint *ep = file->private_data;
	struct pp_slot *slot;
	struct dma_async_tx_descriptor *desc;
	dma_cookie_t cookie;
	int index, ret = 0;
	u32 length;

	if (cmd != PP_START && cmd != PP_FINISH)
		return -ENOTTY;
	if (copy_from_user(&index, (void __user *)arg, sizeof(index)))
		return -EFAULT;
	if (index < 0 || index >= PP_COUNT)
		return -EINVAL;
	mutex_lock(&ep->lock);
	if (ep->faulted) {
		ret = -EIO;
		goto out;
	}
	slot = (struct pp_slot *)((u8 *)ep->cpu + index * PP_STRIDE);
	if (cmd == PP_START) {
		if (ep->pending) {
			ret = -EBUSY;
			goto out;
		}
		length = READ_ONCE(slot->length);
		if (!pp_dma_length_valid(length, ep->max_transfer_bytes)) {
			ret = -EINVAL;
			goto out;
		}
		reinit_completion(&ep->done);
		WRITE_ONCE(slot->status, 1);
		/* Coherent allocation handles cache visibility; order payload stores
		 * before the DMAengine starts the device, not userspace cache guesses.
		 */
		dma_wmb();
		desc = dmaengine_prep_slave_single(ep->chan,
			ep->dma + index * PP_STRIDE, length,
			ep->direction, DMA_CTRL_ACK | DMA_PREP_INTERRUPT);
		if (!desc) {
			WRITE_ONCE(slot->status, 3);
			ret = -EIO;
			goto out;
		}
		desc->callback = pp_complete;
		desc->callback_param = ep;
		cookie = dmaengine_submit(desc);
		ret = dma_submit_error(cookie);
		if (ret) {
			WRITE_ONCE(slot->status, 3);
			goto out;
		}
		ep->pending = true;
		ep->active_index = index;
		dma_async_issue_pending(ep->chan);
	} else {
		if (!ep->pending || ep->active_index != index) {
			ret = -EINVAL;
			goto out;
		}
		if (!wait_for_completion_timeout(&ep->done,
			msecs_to_jiffies(PP_TIMEOUT_MS))) {
			dmaengine_terminate_sync(ep->chan);
			/* Termination cannot establish that the PL packet is clean.
			 * A later START/open must not silently reuse this endpoint.
			 */
			ep->faulted = true;
			WRITE_ONCE(slot->status, 2);
			ret = -ETIMEDOUT;
		} else {
			dma_rmb();
			WRITE_ONCE(slot->status, 0);
		}
		ep->pending = false;
	}
out:
	mutex_unlock(&ep->lock);
	return ret;
}

static const struct file_operations pp_fops = {
	.owner = THIS_MODULE,
	.open = pp_open,
	.release = pp_close,
	.mmap = pp_mmap,
	.unlocked_ioctl = pp_ioctl,
	.llseek = no_llseek,
};

static void pp_release_endpoint(struct pp_endpoint *ep)
{
	if (ep->misc.this_device)
		misc_deregister(&ep->misc);
	if (ep->chan) {
		if (ep->pending)
			dmaengine_terminate_sync(ep->chan);
		dma_release_channel(ep->chan);
	}
	if (ep->cpu)
		dma_free_coherent(ep->parent, PP_TOTAL, ep->cpu, ep->dma);
}

static int pp_probe(struct platform_device *pdev)
{
	struct pp_pair *pair;
	struct pp_endpoint *ep;
	const char *channel;
	int i, ret;
	u32 length_bits;

	if (!pp_dma_node_allowed(pdev->dev.of_node->name, node_name,
				 enable_motion, enable_rectified))
		return -ENODEV;
	/* arm64 dma-direct may reject a sub-32-bit mask when its DMA zone
	 * spans 4 GiB, even if the actual low RAM bank ends below 2 GiB.
	 * Use the supported allocation API mask, then enforce the *hardware*
	 * HP0 DDR_LOW window on every returned complete buffer below.
	 * The mask is not proof that a buffer is reachable by this bitstream.
	 */
	ret = dma_set_mask_and_coherent(&pdev->dev, DMA_BIT_MASK(32));
	if (ret) {
		dev_err(&pdev->dev, "32-bit DMA allocation API unavailable: %d\n", ret);
		return ret;
	}
	pair = devm_kzalloc(&pdev->dev, sizeof(*pair), GFP_KERNEL);
	if (!pair)
		return -ENOMEM;
	for (i = 0; i < 2; i++) {
		ep = &pair->end[i];
		ep->parent = &pdev->dev;
		ep->direction = i ? DMA_DEV_TO_MEM : DMA_MEM_TO_DEV;
		ret = of_property_read_string_index(pdev->dev.of_node, "dma-names", i,
			&channel);
		if (ret)
			goto fail;
		strscpy(ep->name, channel, sizeof(ep->name));
		ep->chan = dma_request_chan(&pdev->dev, channel);
		if (IS_ERR(ep->chan)) {
			ret = PTR_ERR(ep->chan);
			ep->chan = NULL;
			goto fail;
		}
		ret = of_property_read_u32(ep->chan->device->dev->of_node,
			"xlnx,sg-length-width", &length_bits);
		if (ret)
			goto fail;
		ep->max_transfer_bytes = pp_dma_packet_limit(length_bits, PP_BYTES);
		if (!ep->max_transfer_bytes) {
			ret = -EINVAL;
			goto fail;
		}
		ep->cpu = dma_alloc_coherent(&pdev->dev, PP_TOTAL, &ep->dma,
			GFP_KERNEL);
		if (!ep->cpu) {
			ret = -ENOMEM;
			goto fail;
		}
		if (!pp_dma_region_valid(ep->dma, PP_TOTAL)) {
			dev_err(&pdev->dev, "%s invalid DMA address=%pad bytes=%u\n",
				ep->name, &ep->dma, PP_TOTAL);
			ret = -ERANGE;
			goto fail;
		}
		init_completion(&ep->done);
		mutex_init(&ep->lock);
		ep->misc.minor = MISC_DYNAMIC_MINOR;
		ep->misc.name = ep->name;
		ep->misc.fops = &pp_fops;
		ret = misc_register(&ep->misc);
		if (ret)
			goto fail;
		dev_info(&pdev->dev,
			"%s coherent DMA address=%pad bytes=%u stride=%u alignment=16 mask=32 validated_limit=0x80000000 length_bits=%u max_packet=%u\n",
			ep->name, &ep->dma, PP_TOTAL, PP_STRIDE, length_bits,
			ep->max_transfer_bytes);
	}
	platform_set_drvdata(pdev, pair);
	dev_info(&pdev->dev, "PosePriv DMA proxy ready: %s %s\n",
		pair->end[0].name, pair->end[1].name);
	return 0;
fail:
	for (; i >= 0; i--)
		pp_release_endpoint(&pair->end[i]);
	return ret;
}

static int pp_remove(struct platform_device *pdev)
{
	struct pp_pair *pair = platform_get_drvdata(pdev);
	int i;

	for (i = 1; i >= 0; i--)
		pp_release_endpoint(&pair->end[i]);
	return 0;
}

static const struct of_device_id pp_match[] = {
	{ .compatible = "xlnx,dma_proxy" },
	{ }
};
MODULE_DEVICE_TABLE(of, pp_match);

static struct platform_driver pp_driver = {
	.probe = pp_probe,
	.remove = pp_remove,
	.driver = {
		.name = "posepriv_dma_proxy",
		.of_match_table = pp_match,
	},
};
module_platform_driver(pp_driver);

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("PosePriv temporary coherent DMAengine proxy");
