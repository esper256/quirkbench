# Recovery build notes, 2026-10-21

Historical observations moved out of installation guidance. These describe checks
at the time they were recorded, not the current target state or prerequisites for
an investigation. Later checks supersede the unflashed status below.

The previously checked recovery is `a07f65dafb2a15013dd8a199`, with source
`ecd82de0ee7f109cc042d3310a5dbe05b91fce20bd14820c2d9f7f42811af6d2`.
On 2026-10-21 its packaged source was verified and the separate QEMU check passed:
connection, diagnostics, read-only root and writable shared data. The captured TUI
was also inspected. Local check record: `.quirkbench/boot-checks/a9d97f1e08aaf19b6806e599.json`.
The image is 1,621,098,496 bytes and fits the currently inspected Sandisk recovery
partition. Physical boot and the new Bazzite candidate integration still need checks.
The QEMU helper now reads complete saved diagnostics instead of the shortened preview.



The live-debugging update built recovery `8654181680aaae2e4765b345` on 2026-10-21,
with target runtime `5b43fa38267c3f8584fcc6d08be5899d6ac0f2adc7b352485e7845f3bbb758f2`
and retained recovery base `14f0a9cfbe84bb0fd0b37504`. This image has **not** been
boot-checked or flashed. The fast suite passed 81 tests; independent review findings
were corrected. The foreground controller was restarted with the update.

Next physical checks: confirm the external flash device, update recovery and the
candidate target RPM, verify service/child priorities under bounded load, retrieve
live binary evidence, exercise a changed module and one timed suspend/resume, then
confirm return and evidence upload. Successful compilation and software tests do
not establish those hardware results.

## Later physical recovery check

Recovery `a6683117fae16be045e85c1c` was subsequently flashed and inspected on the
physical target. It reported runtime
`69b3861db441a4a274ccdf49e8d26b95c58c5fbc51c624164db5b8bd3ad029e4`,
connected networking/controller, no failed systemd units, read-only recovery and
writable shared data. No disk I/O errors appeared in the collected logs.
The USB enclosure connected at USB 2.0 speed. Recovery lacked SOF firmware/topology
files; that did not establish an audio hardware fault. No physical drive-removal
or forced power test was performed. The retained candidate was left untouched.

Local report: `.quirkbench/reports/target-e438d64a7291/ad1999f8-e782-49e9-a1c8-c266af83ee75/report-2db5d4638be0.txt`.
