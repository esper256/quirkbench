# Controller entry point

This branch starts a Python rewrite. No controller is implemented here yet.
See the [setup design](designs/setup-and-media.md) and [mockups](mockups/README.md).

The intended controller runs only in the foreground with
`quirkbench controller run`. Setup saves its configuration. There is no controller
systemd integration or automatic background service.

The previous implementation is preserved at Git commit `b807199` for reference.
