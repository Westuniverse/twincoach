// SPDX-License-Identifier: GPL-2.0-only
// Dedicated tail4 AXI operator; coherent arena and one serialized owner.
#include <linux/completion.h>
#include <linux/dma-mapping.h>
#include <linux/fs.h>
#include <linux/interrupt.h>
#include <linux/io.h>
#include <linux/ioport.h>
#include <linux/jiffies.h>
#include <linux/kernel.h>
#include <linux/kref.h>
#include <linux/miscdevice.h>
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/sched/signal.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/delay.h>
#include "tail4_contract.h"

struct tail4_device {
 struct device *dev;
 struct miscdevice misc;
 struct mutex lock;
 struct kref refs;
 struct completion completion;
 void __iomem *regs;
 resource_size_t reg_start,reg_size;
 void *cpu;
 dma_addr_t dma;
 int irq;
 bool region_claimed,irq_requested,opened,removed,faulted,active;
 u32 hardware_id,hardware_version,completed_status,completed_tag;
};

static void tail4_destroy(struct kref *ref)
{
 struct tail4_device *td=container_of(ref,struct tail4_device,refs);
 /* Active ownership carries an extra kref and module pin. A hard drain
  * failure retains both; never free memory still reachable by AXI masters. */
 if (WARN_ON(td->active))return;
 if(td->irq_requested)free_irq(td->irq,td);
 if(td->cpu)dma_free_coherent(td->dev,TAIL4_ARENA_SIZE,td->cpu,td->dma);
 if(td->regs)iounmap(td->regs);
 if(td->region_claimed)release_mem_region(td->reg_start,td->reg_size);
 put_device(td->dev);kfree(td);
}
static void tail4_retire(struct tail4_device *td)
{
 if(!td->active)return;
 td->active=false;
 kref_put(&td->refs,tail4_destroy);
 module_put(THIS_MODULE);
}
static u32 tail4_observe(struct tail4_device *td)
{
 u32 status=readl(td->regs+TAIL4_OP_REG_STATUS);
 if(status&TAIL4_OP_DONE) {
  WRITE_ONCE(td->completed_tag,readl(td->regs+TAIL4_OP_REG_LAST_TAG));
  WRITE_ONCE(td->completed_status,status);
  writel(TAIL4_OP_ACK,td->regs+TAIL4_OP_REG_CONTROL);
 }
 return READ_ONCE(td->completed_status);
}
static irqreturn_t tail4_irq(int irq,void *arg)
{
 struct tail4_device *td=arg;
 u32 status=readl(td->regs+TAIL4_OP_REG_STATUS);
 if(!(status&TAIL4_OP_IRQ))return IRQ_NONE;
 if(READ_ONCE(td->active)) {
  WRITE_ONCE(td->completed_tag,readl(td->regs+TAIL4_OP_REG_LAST_TAG));
  WRITE_ONCE(td->completed_status,status);
  writel(TAIL4_OP_ACK,td->regs+TAIL4_OP_REG_CONTROL);
  complete(&td->completion);
 } else {
  writel(TAIL4_OP_ACK,td->regs+TAIL4_OP_REG_CONTROL);
 }
 return IRQ_HANDLED;
}
static int tail4_drain(struct tail4_device *td)
{
 unsigned long deadline=jiffies+msecs_to_jiffies(TAIL4_CANCEL_TIMEOUT_MS);
 if(!td->active)return 0;
 writel(TAIL4_OP_CANCEL,td->regs+TAIL4_OP_REG_CONTROL);
 do {
  tail4_observe(td);
  if(!(readl(td->regs+TAIL4_OP_REG_STATUS)&TAIL4_OP_BUSY)) {
   dma_rmb();tail4_retire(td);return 0;
  }
  usleep_range(100,200);
 } while(time_before(jiffies,deadline));
 td->faulted=true;
 dev_err(td->dev,"tail4 cancel did not drain in100ms; coherent arena and active module pin retained\n");
 return -ETIMEDOUT;
}
static int tail4_wait(struct tail4_device *td,u32 timeout_ms)
{
 unsigned long deadline=jiffies+msecs_to_jiffies(timeout_ms);
 if(td->irq_requested) {
  for(;;) {
   unsigned long now=jiffies;
   long waited;
   if(time_after_eq(now,deadline))break;
   waited=wait_for_completion_interruptible_timeout(&td->completion,deadline-now);
   if(waited<0)return -EINTR;
   if(!waited)break;
   if((READ_ONCE(td->completed_status)&TAIL4_OP_DONE)&&!(readl(td->regs+TAIL4_OP_REG_STATUS)&TAIL4_OP_BUSY))return 0;
  }
  /* One deadline fallback handles delivered hardware completion/lost IRQ,
   * without polling/ACK racing the IRQ handler on the normal path. */
  if((tail4_observe(td)&TAIL4_OP_DONE)&&!(readl(td->regs+TAIL4_OP_REG_STATUS)&TAIL4_OP_BUSY))return 0;
  return -ETIMEDOUT;
 }
 for(;;) {
  /* Cached IRQ completion is not enough to retire DMA: the hardware owner
   * must also be idle. A stale/early completion cannot release its memory. */
  if((tail4_observe(td)&TAIL4_OP_DONE)&&!(readl(td->regs+TAIL4_OP_REG_STATUS)&TAIL4_OP_BUSY))return 0;
  if(signal_pending(current))return -EINTR;
  if(time_after_eq(jiffies,deadline))return -ETIMEDOUT;
  usleep_range(100,200);
 }
}
static void tail4_address(struct tail4_device *td,u32 reg,u64 address)
{
 writel(lower_32_bits(address),td->regs+reg);
 writel(upper_32_bits(address),td->regs+reg+4);
}
static int tail4_submit(struct tail4_device *td,struct posepriv_tail4_submit *s)
{
 int ret;
 s->status=0;s->last_tag=0;s->outcome=0;s->reserved_out=0;
 ret=tail4_validate_submit(s,TAIL4_ARENA_SIZE);if(ret)return ret;
 if(readl(td->regs+TAIL4_OP_REG_STATUS)&TAIL4_OP_BUSY)return -EBUSY;
 writel(TAIL4_OP_ACK,td->regs+TAIL4_OP_REG_CONTROL);
 readl(td->regs+TAIL4_OP_REG_STATUS); /* flush ACK before fresh completion */
 reinit_completion(&td->completion);
 WRITE_ONCE(td->completed_status,0);WRITE_ONCE(td->completed_tag,0);
 writel(s->config,td->regs+TAIL4_OP_REG_CONFIG);
 writel(s->qparam,td->regs+TAIL4_OP_REG_QPARAM);
 writel(s->relu_limit,td->regs+TAIL4_OP_REG_RELU_LIMIT);
 writel(s->job_tag,td->regs+TAIL4_OP_REG_JOB_TAG);
 tail4_address(td,TAIL4_OP_REG_A_LO,td->dma+s->a_offset);
 tail4_address(td,TAIL4_OP_REG_WB_LO,s->wb_bytes?td->dma+s->wb_offset:0);
 tail4_address(td,TAIL4_OP_REG_P_LO,s->p_bytes?td->dma+s->p_offset:0);
 tail4_address(td,TAIL4_OP_REG_DST_LO,td->dma+s->dst_offset);
 kref_get(&td->refs);__module_get(THIS_MODULE);td->active=true;
 dma_wmb();writel(TAIL4_OP_START,td->regs+TAIL4_OP_REG_CONTROL);
 s->outcome=TAIL4_OUT_ACCEPTED;
 ret=tail4_wait(td,s->timeout_ms);
 if(ret) {
  if(tail4_drain(td))s->outcome|=TAIL4_OUT_QUARANTINED;
  else s->outcome|=TAIL4_OUT_QUIESCED;
 } else {
  dma_rmb();tail4_retire(td);s->outcome|=TAIL4_OUT_QUIESCED;
 }
 s->status=READ_ONCE(td->completed_status);s->last_tag=READ_ONCE(td->completed_tag);
 if(!ret) {
  if(s->last_tag!=s->job_tag) {td->faulted=true;ret=-EPROTO;}
  else if(s->status&TAIL4_OP_CANCELLED)ret=-ECANCELED;
  else if(!(s->status&TAIL4_OP_SUCCESS)||(s->status&TAIL4_OP_ERROR))ret=-EIO;
 }
 return ret;
}
static int tail4_open(struct inode *inode,struct file *file)
{
 struct tail4_device *td=container_of(file->private_data,struct tail4_device,misc);
 int ret=0;
 mutex_lock(&td->lock);
 if(td->removed)ret=-ENODEV;
 else if(td->faulted)ret=-EIO;
 else if(td->opened)ret=-EBUSY;
 else {kref_get(&td->refs);td->opened=true;file->private_data=td;}
 mutex_unlock(&td->lock);return ret;
}
static int tail4_release(struct inode *inode,struct file *file)
{
 struct tail4_device *td=file->private_data;
 mutex_lock(&td->lock);
 /* A synchronous SUBMIT owns its file until return. Only a quarantined
  * command can remain here; retire only if the hardware is now quiescent. */
 if(td->active&&!(readl(td->regs+TAIL4_OP_REG_STATUS)&TAIL4_OP_BUSY)) {
  tail4_observe(td);dma_rmb();tail4_retire(td);
 }
 td->opened=false;
 mutex_unlock(&td->lock);kref_put(&td->refs,tail4_destroy);return 0;
}
static int tail4_mmap(struct file *file,struct vm_area_struct *vma)
{
 struct tail4_device *td=file->private_data;
 int ret;
 mutex_lock(&td->lock);
 if(td->removed)ret=-ENODEV;
 else if(td->faulted)ret=-EIO;
 else if(vma->vm_pgoff||vma->vm_end-vma->vm_start!=TAIL4_ARENA_SIZE||!(vma->vm_flags&VM_SHARED))ret=-EINVAL;
 else {
  vma->vm_flags|=VM_DONTEXPAND|VM_DONTDUMP;
  ret=dma_mmap_coherent(td->dev,vma,td->cpu,td->dma,TAIL4_ARENA_SIZE);
 }
 /* Linux VMAs hold the file reference: exclusive ownership and its kref
  * persist after close(fd), until the last mapping is closed. */
 mutex_unlock(&td->lock);return ret;
}
static long tail4_ioctl(struct file *file,unsigned int cmd,unsigned long arg)
{
 struct tail4_device *td=file->private_data;
 void __user *user=(void __user *)arg;
 int ret=0;
 if(mutex_lock_interruptible(&td->lock))return -ERESTARTSYS;
 if(cmd==POSEPRIV_TAIL4_CANCEL) {
  struct posepriv_tail4_cancel c={0};
  ret=tail4_drain(td);c.status=READ_ONCE(td->completed_status);c.last_tag=READ_ONCE(td->completed_tag);
  c.outcome=td->active?TAIL4_OUT_QUARANTINED:TAIL4_OUT_QUIESCED;
  if(copy_to_user(user,&c,sizeof(c)))ret=-EFAULT;
 } else if(td->removed)ret=-ENODEV;
 else if(cmd==POSEPRIV_TAIL4_GET_INFO) {
  struct posepriv_tail4_info i={.driver_abi=TAIL4_DRIVER_ABI,.hardware_id=td->hardware_id,
   .hardware_version=td->hardware_version,.dma_base=td->dma,.arena_bytes=TAIL4_ARENA_SIZE,
   .default_timeout_ms=TAIL4_DEFAULT_TIMEOUT_MS,.max_timeout_ms=TAIL4_MAX_TIMEOUT_MS,.cancel_timeout_ms=TAIL4_CANCEL_TIMEOUT_MS};
  i.flags=(td->irq_requested?TAIL4_INFO_IRQ:0)|(td->faulted?TAIL4_INFO_FAULTED:0)|(td->active?TAIL4_INFO_ACTIVE:0);
  if(copy_to_user(user,&i,sizeof(i)))ret=-EFAULT;
 } else if(cmd==POSEPRIV_TAIL4_SUBMIT) {
  struct posepriv_tail4_submit s;
  if(td->faulted)ret=-EIO;
  else if(copy_from_user(&s,user,sizeof(s)))ret=-EFAULT;
  else {ret=tail4_submit(td,&s);if(copy_to_user(user,&s,sizeof(s)))ret=-EFAULT;}
 } else ret=-ENOTTY;
 mutex_unlock(&td->lock);return ret;
}
static const struct file_operations tail4_fops={.owner=THIS_MODULE,.open=tail4_open,
 .release=tail4_release,.mmap=tail4_mmap,.unlocked_ioctl=tail4_ioctl,.llseek=no_llseek};
static int tail4_probe(struct platform_device *pdev)
{
 struct tail4_device *td;
 struct resource *r=platform_get_resource(pdev,IORESOURCE_MEM,0);
 int ret,irq;
 if(!r||resource_size(r)<0x50)return -EINVAL;
 td=kzalloc(sizeof(*td),GFP_KERNEL);if(!td)return -ENOMEM;
 td->dev=get_device(&pdev->dev);kref_init(&td->refs);mutex_init(&td->lock);init_completion(&td->completion);
 td->reg_start=r->start;td->reg_size=resource_size(r);
 if(!request_mem_region(td->reg_start,td->reg_size,dev_name(td->dev))) {ret=-EBUSY;goto fail;}
 td->region_claimed=true;td->regs=ioremap(td->reg_start,td->reg_size);if(!td->regs) {ret=-ENOMEM;goto fail;}
 /* Exactly once, after DT binding and platform resource mapping. No defaults. */
 td->hardware_id=readl(td->regs+TAIL4_OP_REG_ID);td->hardware_version=readl(td->regs+TAIL4_OP_REG_VERSION);
 if(td->hardware_id!=TAIL4_OP_ID||td->hardware_version!=TAIL4_OP_ABI_VERSION) {ret=-ENODEV;goto fail;}
 if(readl(td->regs+TAIL4_OP_REG_STATUS)&TAIL4_OP_BUSY) {ret=-EBUSY;goto fail;}
 /* Existing HP0 DDR-low path. Coherency comes from the DMA API, not ACE or
  * a false DT dma-coherent declaration on the non-snooping HP0 interface. */
 ret=dma_set_mask_and_coherent(td->dev,DMA_BIT_MASK(32));if(ret)goto fail;
 td->cpu=dma_alloc_coherent(td->dev,TAIL4_ARENA_SIZE,&td->dma,GFP_KERNEL);if(!td->cpu) {ret=-ENOMEM;goto fail;}
 writel(TAIL4_OP_ACK,td->regs+TAIL4_OP_REG_CONTROL);
 irq=platform_get_irq_optional(pdev,0);
 if(irq>=0) {td->irq=irq;ret=request_irq(irq,tail4_irq,0,dev_name(td->dev),td);if(ret)goto fail;td->irq_requested=true;}
 else if(irq!=-ENXIO&&irq!=-ENODEV) {ret=irq;goto fail;}
 td->misc.minor=MISC_DYNAMIC_MINOR;td->misc.name="posepriv_tail4";td->misc.mode=0600;td->misc.parent=td->dev;td->misc.fops=&tail4_fops;
 ret=misc_register(&td->misc);if(ret)goto fail;
 platform_set_drvdata(pdev,td);
 dev_info(td->dev,"tail4 ID=%08x ABI=%08x coherent=%pad bytes=%u irq=%d\n",td->hardware_id,td->hardware_version,&td->dma,TAIL4_ARENA_SIZE,td->irq_requested?td->irq:-1);
 return 0;
fail:kref_put(&td->refs,tail4_destroy);return ret;
}
static int tail4_remove(struct platform_device *pdev)
{
 struct tail4_device *td=platform_get_drvdata(pdev);
 misc_deregister(&td->misc);mutex_lock(&td->lock);td->removed=true;
 if(td->active&&!td->faulted)tail4_drain(td);
 if(td->active)dev_err(td->dev,"unbound while DMA quarantined: arena/MMIO/IRQ retained, no active allocation freed\n");
 mutex_unlock(&td->lock);kref_put(&td->refs,tail4_destroy);return 0;
}
static const struct of_device_id tail4_match[]={{.compatible="posepriv,tail4-operator-1.0"},{}};
MODULE_DEVICE_TABLE(of,tail4_match);
static struct platform_driver tail4_driver={.probe=tail4_probe,.remove=tail4_remove,
 .driver={.name="posepriv_tail4",.of_match_table=tail4_match}};
module_platform_driver(tail4_driver);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("PosePriv tail4 coherent arena and serialized AXI operator");
