// SPDX-License-Identifier: GPL-2.0-only
// A controlled debugging module: reports its revision and touches no hardware.
#include <linux/init.h>
#include <linux/module.h>
#include <linux/moduleparam.h>
#ifndef QB_REVISION
#define QB_REVISION 0
#endif
static int revision = QB_REVISION;
module_param(revision, int, 0444);
static int __init qb_probe_init(void)
{
    pr_info("quirkbench comparison module revision %d\n", revision);
    return 0;
}
static void __exit qb_probe_exit(void) {}
module_init(qb_probe_init);
module_exit(qb_probe_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Quirkbench controlled deployment comparison module");
